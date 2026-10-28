#!/usr/bin/env python3
"""
Asbest-Radar Vorarlberg
Lokaler Server: Ort suchen, Gebäude laden, Dachausschnitte aus dem
Orthofoto des Landes Vorarlberg schneiden und von Claude bewerten lassen.

Start:  python app.py      dann im Browser  http://localhost:8765
Voraussetzung: Umgebungsvariable ANTHROPIC_API_KEY, Paket Pillow.
"""

import base64
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from PIL import Image, ImageDraw
except ImportError:
    import subprocess
    print("Installiere Pillow (einmalig, ca. 30 Sekunden) …")
    for extra in ([], ["--user"], ["--break-system-packages"]):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pillow"] + extra)
        if r.returncode == 0:
            break
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit("Pillow konnte nicht installiert werden. Bitte im Terminal ausführen:  "
                 f"{sys.executable} -m pip install pillow")


def load_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    path = os.path.join(HERE, "api-key.txt")
    if not key and os.path.exists(path):
        with open(path, encoding="utf8") as f:
            key = f.read().strip()
    if not key and os.environ.get("GEMINI_API_KEY"):
        return ""
    if not key and sys.stdin and sys.stdin.isatty():
        print("\nEinmalig: Anthropic API-Schlüssel einfügen (beginnt mit sk-ant-).")
        print("Zu holen unter console.anthropic.com, Bereich API Keys.")
        key = input("Schlüssel: ").strip()
        if key:
            with open(path, "w", encoding="utf8") as f:
                f.write(key)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            print("Gespeichert in api-key.txt. Beim nächsten Start nicht mehr nötig.\n")
    return key


# ----------------------------------------------------------------- Einstellungen
PORT = int(os.environ.get("PORT", "8765"))
ONLINE = bool(os.environ.get("RENDER") or os.environ.get("ONLINE"))
HOST = "0.0.0.0" if ONLINE else "127.0.0.1"
APP_USER = os.environ.get("APP_USER", "asbest")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
API_KEY = load_api_key()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_ACTIVE = [GEMINI_MODEL]   # wird bei "Modell nicht mehr verfügbar" automatisch ersetzt
PROVIDER = "gemini" if GEMINI_KEY else "claude"
MODEL = GEMINI_MODEL if PROVIDER == "gemini" else os.environ.get("ASBEST_MODEL", "claude-sonnet-5-5")
GEMINI_LOCK = threading.Lock()
GEMINI_LAST = [0.0]
BATCH = 10           # Dächer pro KI-Anfrage
CROP_MAX_PX = 768    # max. Kantenlänge eines Dachausschnitts
UA = "Asbest-Radar-Vorarlberg/1.0 (private Nutzung)"
OUT_DIR = os.path.join(HERE, "ergebnisse")

# Bildquellen (in dieser Reihenfolge versucht)
VOGIS_WMS = "https://vogis.cnv.at/geoserver/vogis/ef2022_10cm/wms"
VOGIS_LAYER = "ef2022_10cm"
BASEMAP_HOSTS = ["maps.wien.gv.at", "mapsneu.wien.gv.at", "maps1.wien.gv.at", "maps2.wien.gv.at"]
BASEMAP_PATH = "/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg"
BASEMAP_OK = [0]   # Index des zuletzt funktionierenden Hosts
VOGIS_SEM = threading.Semaphore(2)   # Landesserver schonen

# Grobe Begrenzung Vorarlberg (WGS84) für die Ortssuche
VBG_VIEWBOX = "9.50,47.62,10.25,46.82"   # lon_min,lat_max,lon_max,lat_min

R_EARTH = 6378137.0
JOBS = {}
JOBS_LOCK = threading.Lock()


# ------------------------------------------------------------------- Geometrie
def to_3857(lon, lat):
    x = math.radians(lon) * R_EARTH
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R_EARTH
    return x, y


def from_3857(x, y):
    lon = math.degrees(x / R_EARTH)
    lat = math.degrees(2 * math.atan(math.exp(y / R_EARTH)) - math.pi / 2)
    return lon, lat


def meters_per_unit(lat):
    """Web-Mercator streckt mit 1/cos(lat). Wahre Meter = Einheit * cos(lat)."""
    return math.cos(math.radians(lat))


def bbox_for_polygon(coords_3857, lat):
    xs = [p[0] for p in coords_3857]
    ys = [p[1] for p in coords_3857]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = maxx - minx, maxy - miny
    side = max(w, h)
    # Rand: 35 % der Gebäudegröße, mindestens 6 m echte Distanz
    pad = max(side * 0.35, 6.0 / meters_per_unit(lat))
    side += 2 * pad
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    return cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2


def polygon_area_m2(coords_3857, lat):
    a = 0.0
    n = len(coords_3857)
    for i in range(n):
        x1, y1 = coords_3857[i]
        x2, y2 = coords_3857[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2 * meters_per_unit(lat) ** 2


# ------------------------------------------------------------------- HTTP-Hilfe
def http_get(url, timeout=40, data=None, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


# ------------------------------------------------------------------ Ortssuche
VBG_BOUNDS = (9.50, 46.82, 10.25, 47.62)   # lon_min, lat_min, lon_max, lat_max


def in_vbg(lat, lon):
    return VBG_BOUNDS[1] <= lat <= VBG_BOUNDS[3] and VBG_BOUNDS[0] <= lon <= VBG_BOUNDS[2]


def normalize_query(q):
    q = " ".join(q.replace(";", ",").split())
    q = re.sub(r"(?i)str\.(?=\s|,|\d|$)", "straße", q)
    q = re.sub(r"(?i)(?<=\w)str(?=\s+\d)", "straße", q)
    q = re.sub(r"(?i)strasse\b", "straße", q)
    q = re.sub(r"(?i)\bg\.(?=\s|,|$)", "gasse", q)
    return q


def geo_nominatim(q):
    params = urllib.parse.urlencode({
        "q": q, "format": "jsonv2", "limit": 8, "countrycodes": "at", "addressdetails": 1,
        "viewbox": VBG_VIEWBOX, "bounded": 1, "accept-language": "de",
    })
    raw, _ = http_get("https://nominatim.openstreetmap.org/search?" + params, timeout=12)
    out = []
    for r in json.loads(raw):
        a = r.get("address", {})
        street = " ".join(x for x in (a.get("road"), a.get("house_number")) if x)
        town = a.get("village") or a.get("town") or a.get("city") or a.get("municipality") or ""
        short = ", ".join(x for x in (r.get("name") if not street else "", street, town) if x)
        bb = [float(v) for v in r.get("boundingbox", [])]   # lat_min, lat_max, lon_min, lon_max
        out.append({"name": short or r.get("display_name", ""), "full": r.get("display_name", ""),
                    "lat": float(r["lat"]), "lon": float(r["lon"]),
                    "bbox": bb, "adresse": bool(a.get("house_number")), "quelle": "nominatim"})
    return out


def geo_photon(q):
    params = urllib.parse.urlencode({
        "q": q, "limit": 8, "lang": "de",
        "bbox": ",".join(str(v) for v in VBG_BOUNDS),
    })
    raw, _ = http_get("https://photon.komoot.io/api/?" + params, timeout=12)
    out = []
    for f in json.loads(raw).get("features", []):
        p = f.get("properties", {})
        lon, lat = f["geometry"]["coordinates"][:2]
        if p.get("state") and "Vorarlberg" not in p.get("state", ""):
            continue
        street = " ".join(str(x) for x in (p.get("street"), p.get("housenumber")) if x)
        town = p.get("city") or p.get("district") or p.get("county") or ""
        name = p.get("name") if p.get("name") and p.get("name") != street else ""
        short = ", ".join(x for x in (name, street, town) if x)
        ext = p.get("extent")          # lon_min, lat_max, lon_max, lat_min
        bb = [ext[3], ext[1], ext[0], ext[2]] if ext and len(ext) == 4 else []
        out.append({"name": short or name or "Treffer", "full": short,
                    "lat": float(lat), "lon": float(lon), "bbox": bb,
                    "adresse": bool(p.get("housenumber")), "quelle": "photon"})
    return out


def geocode(q):
    q = normalize_query(q)
    # Koordinaten direkt, z.B. "47.4935, 9.6880"
    m = re.match(r"^\s*(4[67]\.\d+)\s*[,; ]\s*(9\.\d+|10\.\d+)\s*$", q)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return [{"name": f"{lat:.5f}, {lon:.5f}", "full": "Koordinate", "lat": lat, "lon": lon,
                 "bbox": [], "adresse": True, "quelle": "eingabe"}]

    results, errors = [], []

    def run(fn):
        try:
            results.extend(fn(q))
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")

    ts = [threading.Thread(target=run, args=(fn,)) for fn in (geo_nominatim, geo_photon)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    if not results and errors:
        raise RuntimeError(" | ".join(errors))

    # Doppelte (unter 40 m Abstand, gleicher Name) zusammenführen, Adressen mit Hausnummer zuerst
    out = []
    for r in results:
        if not in_vbg(r["lat"], r["lon"]):
            continue
        dup = next((o for o in out if abs(o["lat"] - r["lat"]) < 0.0004 and abs(o["lon"] - r["lon"]) < 0.0006), None)
        if dup:
            continue
        out.append(r)
    want_nr = bool(re.search(r"\d", q))
    out.sort(key=lambda r: (not (r["adresse"] and want_nr), r["quelle"] != "nominatim"))
    return out[:8]


# ---------------------------------------------------------------- Gebäude (OSM)
OVERPASS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]


def load_buildings(lat, lon, radius, limit):
    q = f'[out:json][timeout:25];way["building"](around:{int(radius)},{lat},{lon});out tags geom;'
    data = urllib.parse.urlencode({"data": q}).encode()
    errors = []
    for url in OVERPASS:
        try:
            raw, _ = http_get(url, timeout=20, data=data)
            js = json.loads(raw)
            print(f"Gebäude von {url.split('/')[2]}", flush=True)
            break
        except Exception as e:  # nächster Server
            errors.append(f"{url.split('/')[2]}: {e}")
            print("Overpass-Fehler", errors[-1], flush=True)
    else:
        raise RuntimeError("Gebäudedaten nicht erreichbar: " + " | ".join(errors))

    cx, cy = to_3857(lon, lat)
    items = []
    for el in js.get("elements", []):
        geom = el.get("geometry") or []
        if len(geom) < 4:
            continue
        ll = [(g["lon"], g["lat"]) for g in geom]
        if ll[0] == ll[-1]:
            ll = ll[:-1]
        merc = [to_3857(x, y) for x, y in ll]
        area = polygon_area_m2(merc, lat)
        if area < 12:          # Kleinstobjekte (Mülltonnenhäuschen etc.) weglassen
            continue
        mx = sum(p[0] for p in merc) / len(merc)
        my = sum(p[1] for p in merc) / len(merc)
        dist = math.hypot(mx - cx, my - cy) * meters_per_unit(lat)
        tags = el.get("tags", {})
        items.append({
            "id": f"w{el['id']}",
            "osm_id": el["id"],
            "ring": ll,
            "merc": merc,
            "area": round(area),
            "dist": round(dist),
            "tags": {k: v for k, v in tags.items() if k in (
                "building", "start_date", "building:levels", "roof:material",
                "roof:shape", "addr:street", "addr:housenumber", "name")},
        })
    items.sort(key=lambda b: b["dist"])
    return items[:limit], len(items)


# ------------------------------------------------------------- Luftbild-Ausschnitt
def crop_from_wms(bbox):
    minx, miny, maxx, maxy = bbox
    side = maxx - minx
    # Ziel ca. 10 cm/px, gedeckelt
    px = max(256, min(CROP_MAX_PX, int(side / 0.10)))
    params = urllib.parse.urlencode({
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": VOGIS_LAYER, "STYLES": "", "SRS": "EPSG:3857",
        "BBOX": f"{minx},{miny},{maxx},{maxy}",
        "WIDTH": px, "HEIGHT": px, "FORMAT": "image/jpeg",
    })
    last = None
    for attempt in range(3):
        try:
            with VOGIS_SEM:
                raw, ctype = http_get(VOGIS_WMS + "?" + params, timeout=40)
            break
        except Exception as e:
            last = e
            time.sleep(2 + attempt * 3)
    else:
        raise RuntimeError(f"Landesserver nicht erreichbar ({last})")
    if "image" not in ctype:
        raise RuntimeError("WMS lieferte kein Bild: " + raw[:200].decode("utf8", "replace"))
    return Image.open(io.BytesIO(raw)).convert("RGB")


def crop_from_basemap(bbox, z=19):
    minx, miny, maxx, maxy = bbox
    world = 2 * math.pi * R_EARTH
    res = world / (256 * 2 ** z)

    def px(x, y):
        return (x + world / 2) / res, (world / 2 - y) / res

    x0, y0 = px(minx, maxy)
    x1, y1 = px(maxx, miny)
    tx0, ty0, tx1, ty1 = int(x0 // 256), int(y0 // 256), int(x1 // 256), int(y1 // 256)
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            raw, last = None, None
            for k in range(len(BASEMAP_HOSTS)):
                host = BASEMAP_HOSTS[(BASEMAP_OK[0] + k) % len(BASEMAP_HOSTS)]
                try:
                    raw, _ = http_get("https://" + host + BASEMAP_PATH.format(z=z, y=ty, x=tx), timeout=30)
                    BASEMAP_OK[0] = BASEMAP_HOSTS.index(host)
                    break
                except Exception as e:
                    last = e
            if raw is None:
                raise RuntimeError(f"basemap.at nicht erreichbar ({last})")
            canvas.paste(Image.open(io.BytesIO(raw)).convert("RGB"),
                         ((tx - tx0) * 256, (ty - ty0) * 256))
    left, top = x0 - tx0 * 256, y0 - ty0 * 256
    img = canvas.crop((int(left), int(top), int(left + (x1 - x0)), int(top + (y1 - y0))))
    return img.resize((512, 512), Image.LANCZOS)


def make_crop(b, lat, source_pref):
    bbox = bbox_for_polygon(b["merc"], lat)
    img, source = None, None
    errors = []
    order = ["vogis", "basemap"] if source_pref != "basemap" else ["basemap", "vogis"]
    for s in order:
        try:
            img = crop_from_wms(bbox) if s == "vogis" else crop_from_basemap(bbox)
            source = s
            break
        except Exception as e:
            errors.append(f"{s}: {e}")
    if img is None:
        raise RuntimeError(" | ".join(errors))

    # Gebäudeumriss einzeichnen, damit die KI weiß, welches Dach gemeint ist
    minx, miny, maxx, maxy = bbox
    w, h = img.size
    pts = [((x - minx) / (maxx - minx) * w, (maxy - y) / (maxy - miny) * h) for x, y in b["merc"]]
    d = ImageDraw.Draw(img)
    d.line(pts + [pts[0]], fill=(255, 40, 40), width=max(2, w // 220))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue(), source, bbox


# ------------------------------------------------------------------- KI-Analyse
PROMPT = """Du bewertest Luftbild-Ausschnitte (Orthofotos, senkrecht von oben, Vorarlberg, Österreich) auf einen möglichen Verdacht auf asbesthaltige Dacheindeckung (Asbestzement, umgangssprachlich "Eternit").

Pro Bild ist genau EIN Gebäude rot umrandet. Bewerte nur die Dachfläche innerhalb der roten Linie.

Hintergrund: In Österreich ist Asbest seit 1990 verboten, Asbestzement für Dächer seit 1.1.1994 nicht mehr in Verkehr gebracht. Typische Verdachtsmerkmale im Luftbild:
- Wellplatten mit gleichmäßiger paralleler Rillenstruktur (häufig auf Scheunen, Ställen, Garagen, Werkhallen, Schuppen)
- grau bis grau-braun verwittert, fleckig, Moos- oder Flechtenbewuchs, dunkle Ablaufspuren
- rautenförmige oder rechteckige kleinformatige Faserzement-Schindeln in Grau, verwittert
Gegenindizien: Dachziegel (rot, braun, anthrazit, klar gegliederte Pfannenreihen), Blech mit Stehfalz und glänzender Fläche, Photovoltaik vollflächig, Gründach, Kies-Flachdach, sichtbar neue Eindeckung.

Wichtig: Aus einem Luftbild ist Asbest NIE sicher feststellbar. Neue asbestfreie Faserzementplatten sehen ähnlich aus. Gib daher nur einen Verdachtsgrad an und sei ehrlich, wenn die Auflösung nicht reicht.

Bei "hoch" oder "mittel" gib zusätzlich die verdächtige Dachfläche als Rechteck an: "bereich": [ymin, xmin, ymax, xmax], Werte 0 bis 1000 relativ zum jeweiligen Bild (0,0 = oben links). Das Rechteck soll nur die verdächtige Eindeckung umfassen, nicht das ganze Bild. Bei "gering" oder "unklar": "bereich": null.

Antworte NUR mit einem JSON-Array, ein Objekt pro Bild, in derselben Reihenfolge:
[{"bild": 1, "stufe": "hoch|mittel|gering|unklar", "dacheindeckung": "kurz, z.B. Wellplatten grau", "merkmale": ["...", "..."], "begruendung": "ein bis zwei Sätze", "bereich": [ymin, xmin, ymax, xmax] oder null}]
"""


def parse_json_array(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError("Antwort ohne JSON: " + text[:200])
    return json.loads(m.group(0))


GEMINI_DEAD = {}          # Modell -> Zeitpunkt, ab dem es heute nicht mehr geht
GEMINI_CANDS = []         # verfügbare Gratis-Modelle laut Google, beste zuerst
SKIP_WORDS = ("tts", "image", "live", "audio", "embed", "robotics", "computer-use", "native", "learnlm")


def gemini_candidates():
    """Fragt einmal bei Google ab, welche Modelle dieser Schlüssel nutzen darf."""
    if GEMINI_CANDS:
        return GEMINI_CANDS
    names = []
    try:
        raw, _ = http_get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                          timeout=20, headers={"x-goog-api-key": GEMINI_KEY})
        for m in json.loads(raw).get("models", []):
            n = m.get("name", "").replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if any(w in n for w in SKIP_WORDS):
                continue
            if ("gemini" in n and "flash" in n) or (n.startswith("gemma-") and n.endswith("-it")):
                names.append(n)
    except Exception as e:
        print("Modell-Liste nicht abrufbar:", e, flush=True)

    def rank(n):
        ver = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)", n)[:1]] or [0.0]
        return (n.startswith("gemma"),            # Gemini vor Gemma
                "preview" in n or "exp" in n,      # stabile vor Vorschau
                "lite" in n,                       # volle vor lite
                -ver[0],                           # neuere zuerst
                n)
    names.sort(key=rank)
    if GEMINI_MODEL in names:
        names.remove(GEMINI_MODEL)
    GEMINI_CANDS[:] = [GEMINI_MODEL] + names
    print("Gemini-Modelle in Reserve:", ", ".join(GEMINI_CANDS), flush=True)
    return GEMINI_CANDS


def next_gemini_model(reason):
    GEMINI_DEAD[GEMINI_ACTIVE[0]] = time.time()
    for n in gemini_candidates():
        t = GEMINI_DEAD.get(n)
        if t is None or time.time() - t > 6 * 3600:
            print(f"Gemini: {GEMINI_ACTIVE[0]} {reason}, wechsle auf {n}", flush=True)
            GEMINI_ACTIVE[0] = n
            return True
    return False


def call_gemini(prompt, items):
    parts = [{"text": prompt}]
    for text, jpeg in items:
        parts.append({"text": text})
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(jpeg).decode()}})
    headers = {"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"}
    for attempt in range(12):
        model = GEMINI_ACTIVE[0]
        cfg = {"temperature": 0.2}
        if not model.startswith("gemma"):
            cfg["responseMimeType"] = "application/json"   # Gemma kennt den JSON-Modus nicht
        body = json.dumps({"contents": [{"role": "user", "parts": parts}],
                           "generationConfig": cfg}).encode()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        # Gratis-Kontingent: höchstens ca. 1 Anfrage alle 7 Sekunden
        with GEMINI_LOCK:
            wait = 7 - (time.time() - GEMINI_LAST[0])
            if wait > 0:
                time.sleep(wait)
            GEMINI_LAST[0] = time.time()
        try:
            raw, _ = http_get(url, timeout=180, data=body, headers=headers)
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf8", "replace")[:1500]
            daily = e.code == 429 and re.search(r"(?i)per ?day|PerDay|daily", msg)
            if e.code in (400, 403, 404) or daily:
                why = "Tageskontingent aufgebraucht" if daily else f"nicht nutzbar ({e.code})"
                if GEMINI_ACTIVE[0] == model and next_gemini_model(why):
                    continue
                if GEMINI_ACTIVE[0] != model:
                    continue
                if daily:
                    raise RuntimeError("Das Gratis-Kontingent von Google ist für heute bei allen Modellen "
                                       "aufgebraucht. Es setzt sich um ca. 9 Uhr früh zurück.")
            if e.code in (429, 500, 503) and attempt < 8:
                time.sleep(15 * (1 + attempt % 3))
                continue
            raise RuntimeError(f"Gemini Fehler {e.code}: {msg[:600]}")
    else:
        raise RuntimeError("Gemini antwortet nicht. Bitte später nochmal.")
    js = json.loads(raw)
    cands = js.get("candidates") or []
    if not cands:
        raise RuntimeError("Gemini lieferte keine Antwort: " + json.dumps(js)[:200])
    text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
    return parse_json_array(text)


def ai_request(prompt, items):
    return call_gemini(prompt, items) if PROVIDER == "gemini" else call_claude(prompt, items)


def call_ai(batch):
    items = []
    for i, (b, jpeg) in enumerate(batch, 1):
        info = ", ".join(f"{k}={v}" for k, v in b["tags"].items()) or "keine Zusatzangaben"
        items.append((f"Bild {i}: Grundfläche ca. {b['area']} m². OSM-Angaben: {info}", jpeg))
    return ai_request(PROMPT, items)


def call_claude(prompt, items):
    content = [{"type": "text", "text": prompt}]
    for text, jpeg in items:
        content.append({"type": "text", "text": text})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(jpeg).decode()}})
    body = json.dumps({
        "model": MODEL, "max_tokens": 3000,
        "messages": [{"role": "user", "content": content}],
    }).encode()
    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    for attempt in range(3):
        try:
            raw, _ = http_get("https://api.anthropic.com/v1/messages", timeout=180,
                              data=body, headers=headers)
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            raise RuntimeError(f"Claude API Fehler {e.code}: {msg}")
    js = json.loads(raw)
    text = "".join(c.get("text", "") for c in js.get("content", []) if c.get("type") == "text")
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError("Antwort ohne JSON: " + text[:200])
    return json.loads(m.group(0))


# ------------------------------------------------------------- Kachel-Suche
GRID_PROMPT = """Du bekommst senkrechte Luftbild-Kacheln (Orthofotos, ca. 80 x 80 m, Vorarlberg, Österreich). Suche in jeder Kachel ALLE Dächer, die verdächtig auf asbesthaltige Eindeckung (Asbestzement, "Eternit") aussehen.

Hintergrund: In Österreich ist Asbest seit 1990 verboten, Asbestzement für Dächer seit 1.1.1994 nicht mehr in Verkehr gebracht. Verdachtsmerkmale:
- Wellplatten mit gleichmäßiger paralleler Rillenstruktur (oft Scheunen, Ställe, Garagen, Werkhallen, Schuppen, Carports)
- grau bis grau-braun verwittert, fleckig, Moos- oder Flechtenbewuchs, dunkle Ablaufspuren
- kleinformatige graue Faserzement-Schindeln (rautenförmig oder rechteckig), verwittert
Kein Verdacht: Dachziegel (rot, braun, anthrazit, klare Pfannenreihen), Blech mit Stehfalz, vollflächige Photovoltaik, Gründach, Kies-Flachdach, sichtbar neue Eindeckung.

Aus Luftbildern ist Asbest nie sicher feststellbar. Melde nur echte Verdachtsfälle mit "stufe" "hoch" oder "mittel". Keine Funde ist eine gültige Antwort.

Für jeden Fund: "box": [ymin, xmin, ymax, xmax] mit Werten 0 bis 1000 relativ zur Kachel (0,0 = oben links), eng um die verdächtige Dachfläche.

Antworte NUR mit einem JSON-Array, ein Objekt pro Kachel in derselben Reihenfolge:
[{"bild": 1, "funde": [{"box": [ymin, xmin, ymax, xmax], "stufe": "hoch|mittel", "dacheindeckung": "kurz", "merkmale": ["..."], "begruendung": "ein Satz"}]}]
"""

GRID_TILE_M = 80
GRID_BATCH = 10


GRID_MAX = 80


def tiles_for_circle(lat, lon, radius):
    cx, cy = to_3857(lon, lat)
    mpu = meters_per_unit(lat)
    side = GRID_TILE_M / mpu
    n = max(1, math.ceil(2 * radius / GRID_TILE_M))
    tiles = []
    for i in range(n):
        for j in range(n):
            tx = cx - radius / mpu + (i + 0.5) * side
            ty = cy - radius / mpu + (j + 0.5) * side
            d = math.hypot(tx - cx, ty - cy) * mpu
            if d <= radius + GRID_TILE_M * 0.5:
                tiles.append({"id": f"k{i}_{j}", "dist": d,
                              "bbox": (tx - side / 2, ty - side / 2, tx + side / 2, ty + side / 2)})
    tiles.sort(key=lambda t: t["dist"])
    return tiles[:GRID_MAX]


def tiles_for_bbox(south, west, north, east):
    """Kacheln à 80 m, die den sichtbaren Kartenausschnitt lückenlos abdecken."""
    x0, y0 = to_3857(west, south)
    x1, y1 = to_3857(east, north)
    lat = (south + north) / 2
    mpu = meters_per_unit(lat)
    side = GRID_TILE_M / mpu
    nx = max(1, math.ceil((x1 - x0) / side))
    ny = max(1, math.ceil((y1 - y0) / side))
    # Raster mittig über den Ausschnitt legen
    ox = x0 - (nx * side - (x1 - x0)) / 2
    oy = y0 - (ny * side - (y1 - y0)) / 2
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tiles = []
    for i in range(nx):
        for j in range(ny):
            bx, by = ox + i * side, oy + j * side
            d = math.hypot(bx + side / 2 - cx, by + side / 2 - cy) * mpu
            tiles.append({"id": f"k{i}_{j}", "dist": d, "bbox": (bx, by, bx + side, by + side)})
    tiles.sort(key=lambda t: t["dist"])
    return tiles


def run_grid(job, upd, lat, lon, tiles, source_pref):
    cx, cy = to_3857(lon, lat)
    mpu = meters_per_unit(lat)
    upd(phase="Luftbild-Kacheln laden", total=len(tiles), total_found=len(tiles))

    def do_crop(t):
        for s_ in (["vogis", "basemap"] if source_pref != "basemap" else ["basemap", "vogis"]):
            try:
                img = crop_from_wms(t["bbox"]) if s_ == "vogis" else crop_from_basemap(t["bbox"])
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=88)
                t["jpeg"], t["src"] = buf.getvalue(), s_
                break
            except Exception as e:
                t["error"] = f"{s_}: {e}"[:200]
        with JOBS_LOCK:
            job["cropped"] += 1

    with ThreadPoolExecutor(3) as ex:
        list(ex.map(do_crop, tiles))
    ok = [t for t in tiles if "jpeg" in t]
    bad = [t for t in tiles if "jpeg" not in t]
    print(f"Kacheln: {len(ok)} Bilder ok, {len(bad)} ohne Bild"
          + (f" (z.B. {bad[0].get('error')})" if bad else ""), flush=True)
    if not ok:
        raise RuntimeError("Keine Luftbild-Kachel ladbar: " + (tiles[0].get("error") or ""))

    upd(phase="KI durchsucht Kacheln")
    finds = []
    images = job.setdefault("images", {})
    batches = [ok[i:i + GRID_BATCH] for i in range(0, len(ok), GRID_BATCH)]

    def do_batch(ts):
        try:
            res = ai_request(GRID_PROMPT, [(f"Bild {k}:", t["jpeg"]) for k, t in enumerate(ts, 1)])
            for k, t in enumerate(ts, 1):
                r = next((x for x in res if isinstance(x, dict) and int(x.get("bild", -1)) == k), None)
                if r is None and len(res) == len(ts) and isinstance(res[k - 1], dict):
                    r = res[k - 1]
                for m, f in enumerate((r or {}).get("funde") or []):
                    if f.get("stufe") not in ("hoch", "mittel"):
                        continue
                    try:
                        ymin, xmin, ymax, xmax = [max(0.0, min(1000.0, float(v))) for v in f["box"]]
                    except Exception:
                        continue
                    if ymax - ymin < 5 or xmax - xmin < 5:
                        continue
                    finds.append((t, m, f, (ymin, xmin, ymax, xmax)))
        except Exception as e:
            print("Kachel-Fehler:", e, flush=True)
            for t in ts:
                t["error"] = str(e)[:300]
        with JOBS_LOCK:
            job["analyzed"] += len(ts)

    with ThreadPoolExecutor(1 if PROVIDER == "gemini" else 2) as ex:
        list(ex.map(do_batch, batches))
    if ok and all(t.get("error") for t in ok):
        raise RuntimeError(ok[0]["error"])

    colors = {"hoch": (255, 220, 0), "mittel": (240, 150, 0)}
    feats = []
    for t, m, f, (ymin, xmin, ymax, xmax) in finds:
        minx, miny, maxx, maxy = t["bbox"]
        img = Image.open(io.BytesIO(t["jpeg"])).convert("RGB")
        w, h = img.size
        col = colors[f["stufe"]]
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(ov).rectangle((xmin / 1000 * w, ymin / 1000 * h, xmax / 1000 * w, ymax / 1000 * h),
                                     fill=col + (70,), outline=col + (255,), width=max(3, w // 150))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        fid = f"{t['id']}_{m}"
        images[fid] = buf.getvalue()
        gx = lambda v: minx + v / 1000 * (maxx - minx)
        gy = lambda v: maxy - v / 1000 * (maxy - miny)
        ring = [from_3857(gx(xmin), gy(ymin)), from_3857(gx(xmax), gy(ymin)),
                from_3857(gx(xmax), gy(ymax)), from_3857(gx(xmin), gy(ymax))]
        ring = [[round(x, 7), round(y, 7)] for x, y in ring]
        ring.append(ring[0])
        area = (xmax - xmin) / 1000 * GRID_TILE_M * (ymax - ymin) / 1000 * GRID_TILE_M
        mx = (gx(xmin) + gx(xmax)) / 2
        my = (gy(ymin) + gy(ymax)) / 2
        feats.append({"type": "Feature", "id": fid,
                      "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"id": fid, "osm_id": "", "flaeche_m2": round(area),
                                     "distanz_m": round(math.hypot(mx - cx, my - cy) * mpu), "osm": {},
                                     "stufe": f["stufe"], "dacheindeckung": f.get("dacheindeckung", ""),
                                     "merkmale": f.get("merkmale", []), "begruendung": f.get("begruendung", ""),
                                     "bildquelle": t["src"], "fehler": "", "markierung": ring,
                                     "modus": "kachel"}})
    errs = [t for t in tiles if t.get("error")]
    return feats, len(ok), len(errs)


# ----------------------------------------------------------------------- Job
def run_job(job_id, lat, lon, radius, limit, source_pref, label, bbox=None):
    job = JOBS[job_id]

    def upd(**kw):
        with JOBS_LOCK:
            job.update(kw)

    try:
        grid_reason = None
        if bbox:
            tiles = tiles_for_bbox(*bbox)
            grid_reason = "ausschnitt"
        else:
            upd(phase="Gebäude laden")
            try:
                buildings, total = load_buildings(lat, lon, radius, limit)
            except Exception as e:
                print("Wechsel auf Kachel-Suche:", e, flush=True)
                tiles = tiles_for_circle(lat, lon, radius)
                grid_reason = "keine_gebaeudedaten"
        if grid_reason:
            feats, n_ok, n_err = run_grid(job, upd, lat, lon, tiles, source_pref)
            fc = {"type": "FeatureCollection", "features": feats,
                  "meta": {"ort": label, "lat": lat, "lon": lon, "radius_m": radius, "modus": "kachel",
                           "grund": grid_reason, "ausschnitt": bbox,
                           "kacheln": n_ok, "kachel_fehler": n_err,
                           "modell": f"{PROVIDER}:{GEMINI_ACTIVE[0] if PROVIDER == 'gemini' else MODEL}", "erstellt": datetime.now().isoformat(timespec="seconds"),
                           "hinweis": "KI-Vorauswahl aus Luftbildern. Kein Nachweis von Asbest."}}
            upd(phase="fertig", done=True, result=fc, saved="", mode="kachel")
            return
        upd(total_found=total, total=len(buildings))
        if not buildings:
            upd(phase="fertig", done=True)
            return

        upd(phase="Luftbilder schneiden")
        crops = {}

        def do_crop(b):
            try:
                jpeg, src, bbox = make_crop(b, lat, source_pref)
                crops[b["id"]] = (jpeg, src, bbox)
            except Exception as e:
                b["error"] = str(e)[:300]
            with JOBS_LOCK:
                job["cropped"] += 1

        with ThreadPoolExecutor(3) as ex:
            list(ex.map(do_crop, buildings))
        job["images"] = {k: v[0] for k, v in crops.items()}

        ok = [b for b in buildings if b["id"] in crops]
        print(f"Bilder: {len(ok)} ok, {len(buildings) - len(ok)} Fehler", flush=True)
        if not ok:
            raise RuntimeError("Kein einziges Luftbild konnte geladen werden. "
                               + (buildings[0].get("error") or ""))

        upd(phase="KI bewertet Dächer")
        batches = [ok[i:i + BATCH] for i in range(0, len(ok), BATCH)]

        def do_batch(bs):
            try:
                res = call_ai([(b, crops[b["id"]][0]) for b in bs])
                for i, b in enumerate(bs, 1):
                    r = next((x for x in res if int(x.get("bild", -1)) == i), None)
                    if r is None and len(res) == len(bs):
                        r = res[i - 1]
                    b["ki"] = r or {"stufe": "unklar", "begruendung": "Keine Antwort für dieses Bild."}
            except Exception as e:
                for b in bs:
                    b["error"] = str(e)[:300]
            with JOBS_LOCK:
                job["analyzed"] += len(bs)

        with ThreadPoolExecutor(1 if PROVIDER == "gemini" else 2) as ex:
            list(ex.map(do_batch, batches))
        if all(b.get("error") for b in ok):
            raise RuntimeError(ok[0]["error"])

        # Verdächtige Fläche ins Bild zeichnen und als Kartenfläche umrechnen
        colors = {"hoch": (255, 220, 0), "mittel": (240, 150, 0)}
        for b in buildings:
            ki = b.get("ki") or {}
            area = ki.get("bereich")
            if b["id"] not in crops or ki.get("stufe") not in colors:
                continue
            try:
                ymin, xmin, ymax, xmax = [max(0.0, min(1000.0, float(v))) for v in area]
                if ymax - ymin < 5 or xmax - xmin < 5:
                    continue
            except Exception:
                continue
            jpeg, src, (minx, miny, maxx, maxy) = crops[b["id"]]
            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
            w, h = img.size
            box = (xmin / 1000 * w, ymin / 1000 * h, xmax / 1000 * w, ymax / 1000 * h)
            col = colors[ki["stufe"]]
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            od.rectangle(box, fill=col + (70,), outline=col + (255,), width=max(3, w // 150))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
            job["images"][b["id"]] = buf.getvalue()
            gx = lambda xv: minx + xv / 1000 * (maxx - minx)
            gy = lambda yv: maxy - yv / 1000 * (maxy - miny)
            ring = [from_3857(gx(xmin), gy(ymin)), from_3857(gx(xmax), gy(ymin)),
                    from_3857(gx(xmax), gy(ymax)), from_3857(gx(xmin), gy(ymax))]
            b["markierung"] = [[round(x, 7), round(y, 7)] for x, y in ring + [ring[0]]]

        feats = []
        for b in buildings:
            ki = b.get("ki") or {}
            feats.append({
                "type": "Feature",
                "id": b["id"],
                "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in b["ring"] + [b["ring"][0]]]]},
                "properties": {
                    "id": b["id"], "osm_id": b["osm_id"], "flaeche_m2": b["area"],
                    "distanz_m": b["dist"], "osm": b["tags"],
                    "stufe": ki.get("stufe", "fehler" if b.get("error") else "unklar"),
                    "dacheindeckung": ki.get("dacheindeckung", ""),
                    "merkmale": ki.get("merkmale", []),
                    "begruendung": ki.get("begruendung", ""),
                    "bildquelle": crops.get(b["id"], (None, "", None))[1],
                    "markierung": b.get("markierung"),
                    "fehler": b.get("error", ""),
                },
            })
        fc = {"type": "FeatureCollection", "features": feats,
              "meta": {"ort": label, "lat": lat, "lon": lon, "radius_m": radius,
                       "modell": f"{PROVIDER}:{GEMINI_ACTIVE[0] if PROVIDER == 'gemini' else MODEL}", "erstellt": datetime.now().isoformat(timespec="seconds"),
                       "hinweis": "KI-Vorauswahl aus Luftbildern. Kein Nachweis von Asbest."}}
        os.makedirs(OUT_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9äöüÄÖÜß_-]+", "_", label)[:40] or "ort"
        path = os.path.join(OUT_DIR, f"{datetime.now():%Y-%m-%d_%H%M}_{safe}.geojson")
        with open(path, "w", encoding="utf8") as f:
            json.dump(fc, f, ensure_ascii=False, indent=1)
        upd(phase="fertig", done=True, result=fc, saved=path)
    except Exception as e:
        print("Auftrag fehlgeschlagen:", e, flush=True)
        upd(phase="Fehler", done=True, error=str(e)[:500])


# ---------------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if not APP_PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode("utf8").partition(":")
                if user == APP_USER and pw == APP_PASSWORD:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Asbest-Radar", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self.authorized():
            return
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self.send(200, {"api_key": bool(API_KEY or GEMINI_KEY), "model": MODEL, "provider": PROVIDER})
        if u.path == "/api/geocode":
            try:
                return self.send(200, geocode(qs.get("q", [""])[0]))
            except Exception as e:
                return self.send(502, {"error": f"Ortssuche fehlgeschlagen: {e}"})
        m = re.match(r"^/api/job/([0-9a-f-]+)$", u.path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self.send(404, {"error": "Auftrag unbekannt"})
            with JOBS_LOCK:
                view = {k: v for k, v in job.items() if k != "images"}
            return self.send(200, view)
        m = re.match(r"^/api/img/([0-9a-f-]+)/([wk][\w]+)\.jpg$", u.path)
        if m:
            img = JOBS.get(m.group(1), {}).get("images", {}).get(m.group(2))
            if img:
                return self.send(200, img, "image/jpeg")
            return self.send(404, {"error": "kein Bild"})
        self.send(404, {"error": "nicht gefunden"})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/api/analyze":
            return self.send(404, {"error": "nicht gefunden"})
        if not (API_KEY or GEMINI_KEY):
            return self.send(400, {"error": "Kein KI-Schlüssel gesetzt (GEMINI_API_KEY oder ANTHROPIC_API_KEY)."})
        n = int(self.headers.get("Content-Length", "0"))
        bbox = None
        try:
            p = json.loads(self.rfile.read(n) or b"{}")
            if p.get("bbox"):
                s, w, nn, e = [float(v) for v in p["bbox"]]
                if not (s < nn and w < e):
                    raise ValueError
                bbox = (s, w, nn, e)
                lat, lon = (s + nn) / 2, (w + e) / 2
            else:
                lat, lon = float(p["lat"]), float(p["lon"])
        except Exception:
            return self.send(400, {"error": "Ungültige Anfrage"})
        if not in_vbg(lat, lon):
            return self.send(400, {"error": "Der Ausschnitt liegt nicht in Vorarlberg."})
        if bbox:
            n_tiles = len(tiles_for_bbox(*bbox))
            if n_tiles > GRID_MAX:
                return self.send(400, {"error": f"Ausschnitt zu groß ({n_tiles} Kacheln à 80 m, "
                                                f"max. {GRID_MAX}). Bitte näher hineinzoomen."})
        radius = max(50, min(1500, int(p.get("radius", 250))))
        limit = max(1, min(400, int(p.get("max", 60))))
        job_id = str(uuid.uuid4())
        JOBS[job_id] = {"phase": "Start", "done": False, "total": 0, "total_found": 0,
                        "cropped": 0, "analyzed": 0, "started": time.time()}
        threading.Thread(target=run_job, daemon=True, args=(
            job_id, lat, lon, radius, limit, p.get("source", "vogis"),
            p.get("label", "Ort"), bbox)).start()
        self.send(200, {"job": job_id})


def selftest():
    """Einmaliger Live-Test beim Start (nur wenn SELFTEST=1 gesetzt). Ergebnis steht im Log."""
    time.sleep(3)
    print("SELBSTTEST Start", flush=True)
    t0 = time.time()
    try:
        hits = geocode("Rheinstr 57 Hard")
        print(f"SELBSTTEST Ortssuche: {len(hits)} Treffer in {time.time() - t0:.1f}s", flush=True)
        for h in hits[:4]:
            print(f"SELBSTTEST   {h['quelle']}: {h['name']} ({h['lat']:.5f}, {h['lon']:.5f})", flush=True)
    except Exception as e:
        print("SELBSTTEST Ortssuche FEHLER:", e, flush=True)
        hits = []
    lat, lon = (hits[0]["lat"], hits[0]["lon"]) if hits else (47.4927, 9.6842)
    d_lat, d_lon = 0.0007, 0.0011          # ca. 155 x 165 m -> 4 bis 6 Kacheln
    bbox = (lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon)
    JOBS["selftest"] = {"phase": "Start", "done": False, "total": 0, "total_found": 0,
                        "cropped": 0, "analyzed": 0, "started": time.time()}
    t1 = time.time()
    run_job("selftest", lat, lon, 250, 60, "vogis", "Selbsttest", bbox)
    j = JOBS["selftest"]
    if j.get("error"):
        print("SELBSTTEST Analyse FEHLER:", j["error"], flush=True)
        return
    m = j["result"]["meta"]
    print(f"SELBSTTEST Analyse fertig in {time.time() - t1:.0f}s: {m.get('kacheln')} Kacheln ok, "
          f"{m.get('kachel_fehler')} mit Fehler, {len(j['result']['features'])} Verdachtsflächen, "
          f"Modell {m.get('modell')}", flush=True)
    for f in j["result"]["features"][:5]:
        p = f["properties"]
        print(f"SELBSTTEST   {p['stufe']}: {p['dacheindeckung']} | {p['begruendung']} | Quelle {p['bildquelle']}", flush=True)


if __name__ == "__main__":
    if not API_KEY:
        print("Achtung: ANTHROPIC_API_KEY ist nicht gesetzt. Die Karte läuft, die KI-Analyse nicht.")
    try:
        srv = ThreadingHTTPServer((HOST, PORT), H)
    except OSError:
        sys.exit(f"Port {PORT} ist belegt. Läuft das Tool schon in einem anderen Fenster? "
                 f"Sonst anderen Port wählen, z.B.  PORT=8766 python3 app.py")
    if ONLINE and not APP_PASSWORD:
        sys.exit("APP_PASSWORD fehlt. Ohne Passwort könnte jeder deinen API-Zugang verbrauchen.")
    if ONLINE:
        print(f"Asbest-Radar online auf Port {PORT}. Passwortschutz: {'an' if APP_PASSWORD else 'AUS'}", flush=True)
    else:
        url = f"http://localhost:{PORT}"
        print(f"Asbest-Radar läuft:  {url}")
        print("Dieses Fenster offen lassen. Beenden mit Strg+C.")
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    if os.environ.get("SELFTEST") == "1":
        threading.Thread(target=selftest, daemon=True).start()
    srv.serve_forever()
