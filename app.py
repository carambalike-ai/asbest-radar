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
MODEL = os.environ.get("ASBEST_MODEL", "claude-sonnet-5-5")
BATCH = 6            # Dächer pro KI-Anfrage
CROP_MAX_PX = 768    # max. Kantenlänge eines Dachausschnitts
UA = "Asbest-Radar-Vorarlberg/1.0 (private Nutzung)"
OUT_DIR = os.path.join(HERE, "ergebnisse")

# Bildquellen (in dieser Reihenfolge versucht)
VOGIS_WMS = "https://vogis.cnv.at/geoserver/vogis/ef2022_10cm/wms"
VOGIS_LAYER = "ef2022_10cm"
BASEMAP_TILE = "https://maps{s}.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg"

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
def geocode(q):
    params = urllib.parse.urlencode({
        "q": q, "format": "jsonv2", "limit": 6, "countrycodes": "at",
        "viewbox": VBG_VIEWBOX, "bounded": 1, "accept-language": "de",
    })
    raw, _ = http_get("https://nominatim.openstreetmap.org/search?" + params)
    out = []
    for r in json.loads(raw):
        out.append({
            "name": r.get("display_name", ""),
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "bbox": [float(v) for v in r.get("boundingbox", [])],
        })
    return out


# ---------------------------------------------------------------- Gebäude (OSM)
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def load_buildings(lat, lon, radius, limit):
    q = f'[out:json][timeout:60];way["building"](around:{int(radius)},{lat},{lon});out tags geom;'
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for url in OVERPASS:
        try:
            raw, _ = http_get(url, timeout=90, data=data)
            js = json.loads(raw)
            break
        except Exception as e:  # nächster Server
            last = e
    else:
        raise RuntimeError(f"Gebäudedaten nicht erreichbar ({last})")

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
    raw, ctype = http_get(VOGIS_WMS + "?" + params, timeout=40)
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
            url = BASEMAP_TILE.format(s=1 + (tx + ty) % 4, z=z, y=ty, x=tx)
            raw, _ = http_get(url, timeout=30)
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
    return buf.getvalue(), source


# ------------------------------------------------------------------- KI-Analyse
PROMPT = """Du bewertest Luftbild-Ausschnitte (Orthofotos, senkrecht von oben, Vorarlberg, Österreich) auf einen möglichen Verdacht auf asbesthaltige Dacheindeckung (Asbestzement, umgangssprachlich "Eternit").

Pro Bild ist genau EIN Gebäude rot umrandet. Bewerte nur die Dachfläche innerhalb der roten Linie.

Hintergrund: In Österreich ist Asbest seit 1990 verboten, Asbestzement für Dächer seit 1.1.1994 nicht mehr in Verkehr gebracht. Typische Verdachtsmerkmale im Luftbild:
- Wellplatten mit gleichmäßiger paralleler Rillenstruktur (häufig auf Scheunen, Ställen, Garagen, Werkhallen, Schuppen)
- grau bis grau-braun verwittert, fleckig, Moos- oder Flechtenbewuchs, dunkle Ablaufspuren
- rautenförmige oder rechteckige kleinformatige Faserzement-Schindeln in Grau, verwittert
Gegenindizien: Dachziegel (rot, braun, anthrazit, klar gegliederte Pfannenreihen), Blech mit Stehfalz und glänzender Fläche, Photovoltaik vollflächig, Gründach, Kies-Flachdach, sichtbar neue Eindeckung.

Wichtig: Aus einem Luftbild ist Asbest NIE sicher feststellbar. Neue asbestfreie Faserzementplatten sehen ähnlich aus. Gib daher nur einen Verdachtsgrad an und sei ehrlich, wenn die Auflösung nicht reicht.

Antworte NUR mit einem JSON-Array, ein Objekt pro Bild, in derselben Reihenfolge:
[{"bild": 1, "stufe": "hoch|mittel|gering|unklar", "dacheindeckung": "kurz, z.B. Wellplatten grau", "merkmale": ["...", "..."], "begruendung": "ein bis zwei Sätze"}]
"""


def call_claude(batch):
    content = [{"type": "text", "text": PROMPT}]
    for i, (b, jpeg) in enumerate(batch, 1):
        t = b["tags"]
        info = ", ".join(f"{k}={v}" for k, v in t.items()) or "keine Zusatzangaben"
        content.append({"type": "text",
                        "text": f"Bild {i}: Grundfläche ca. {b['area']} m². OSM-Angaben: {info}"})
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


# ----------------------------------------------------------------------- Job
def run_job(job_id, lat, lon, radius, limit, source_pref, label):
    job = JOBS[job_id]

    def upd(**kw):
        with JOBS_LOCK:
            job.update(kw)

    try:
        upd(phase="Gebäude laden")
        buildings, total = load_buildings(lat, lon, radius, limit)
        upd(total_found=total, total=len(buildings))
        if not buildings:
            upd(phase="fertig", done=True)
            return

        upd(phase="Luftbilder schneiden")
        crops = {}

        def do_crop(b):
            try:
                jpeg, src = make_crop(b, lat, source_pref)
                crops[b["id"]] = (jpeg, src)
            except Exception as e:
                b["error"] = str(e)[:300]
            with JOBS_LOCK:
                job["cropped"] += 1

        with ThreadPoolExecutor(4) as ex:
            list(ex.map(do_crop, buildings))
        job["images"] = {k: v[0] for k, v in crops.items()}

        ok = [b for b in buildings if b["id"] in crops]
        if not ok:
            raise RuntimeError("Kein einziges Luftbild konnte geladen werden. "
                               + (buildings[0].get("error") or ""))

        upd(phase="KI bewertet Dächer")
        batches = [ok[i:i + BATCH] for i in range(0, len(ok), BATCH)]

        def do_batch(bs):
            try:
                res = call_claude([(b, crops[b["id"]][0]) for b in bs])
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

        with ThreadPoolExecutor(2) as ex:
            list(ex.map(do_batch, batches))

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
                    "bildquelle": crops.get(b["id"], (None, ""))[1],
                    "fehler": b.get("error", ""),
                },
            })
        fc = {"type": "FeatureCollection", "features": feats,
              "meta": {"ort": label, "lat": lat, "lon": lon, "radius_m": radius,
                       "modell": MODEL, "erstellt": datetime.now().isoformat(timespec="seconds"),
                       "hinweis": "KI-Vorauswahl aus Luftbildern. Kein Nachweis von Asbest."}}
        os.makedirs(OUT_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9äöüÄÖÜß_-]+", "_", label)[:40] or "ort"
        path = os.path.join(OUT_DIR, f"{datetime.now():%Y-%m-%d_%H%M}_{safe}.geojson")
        with open(path, "w", encoding="utf8") as f:
            json.dump(fc, f, ensure_ascii=False, indent=1)
        upd(phase="fertig", done=True, result=fc, saved=path)
    except Exception as e:
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
            return self.send(200, {"api_key": bool(API_KEY), "model": MODEL})
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
        m = re.match(r"^/api/img/([0-9a-f-]+)/(w\d+)\.jpg$", u.path)
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
        if not API_KEY:
            return self.send(400, {"error": "ANTHROPIC_API_KEY ist nicht gesetzt. Siehe Anleitung."})
        n = int(self.headers.get("Content-Length", "0"))
        try:
            p = json.loads(self.rfile.read(n) or b"{}")
            lat, lon = float(p["lat"]), float(p["lon"])
        except Exception:
            return self.send(400, {"error": "Ungültige Anfrage"})
        radius = max(50, min(1500, int(p.get("radius", 250))))
        limit = max(1, min(400, int(p.get("max", 60))))
        job_id = str(uuid.uuid4())
        JOBS[job_id] = {"phase": "Start", "done": False, "total": 0, "total_found": 0,
                        "cropped": 0, "analyzed": 0, "started": time.time()}
        threading.Thread(target=run_job, daemon=True, args=(
            job_id, lat, lon, radius, limit, p.get("source", "vogis"),
            p.get("label", "Ort"))).start()
        self.send(200, {"job": job_id})


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
    srv.serve_forever()
