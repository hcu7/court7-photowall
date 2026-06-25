"""Court 7 Photowall — FastAPI Selfie-Upload + Live-TV-Diashow.

Single-file app im Stil von tennis-opc. Gäste laden vom Handy Selfies hoch,
die Fotos erscheinen live als Diashow auf einem Fernseher (Fire TV o.ä.).

Gebaut ursprünglich für einen Geburtstag, aber komplett ENV-konfigurierbar und
für jedes Event wiederverwendbar — alle Personalisierungen (Titel, Domain)
kommen aus der Umgebung, nicht aus dem Code.

Endpoints
---------
  GET  /                      Handy-Upload-Seite
  GET  /tv                    TV-Diashow (Vollbild, auto-aktualisierend)
  GET  /moderate              Handy-Moderation (Foto entfernen, PIN-geschützt)
  POST /api/upload            Foto hochladen (multipart "file")
  GET  /api/photos?since=ID   Liste der Foto-IDs (neuer als ID)
  GET  /photo/{id}            Foto ausliefern (JPEG)
  GET  /api/config            Laufzeit-Konfig fürs Frontend (Titel etc.)
  GET  /api/qr.png            QR-Code zur Upload-URL
  POST /api/photos/{id}/hide  Foto verstecken (Token nötig)
  GET  /healthz               Healthcheck
"""
import io
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

# HEIC/HEIF-Support (iPhone-Fotos) — Wheel bringt libheif mit, daher optional.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover
    HEIC_OK = False

import qrcode

# ---------------------------------------------------------------------------
# Konfiguration — alles via ENV, damit dieselbe App für jedes Event taugt.
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
HIDDEN_DIR = DATA_DIR / "hidden"
TITLE = os.environ.get("TITLE", "Happy Birthday! \U0001F389")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SLIDE_SECONDS = float(os.environ.get("SLIDE_SECONDS", "4"))
FRONT_CAMERA = os.environ.get("FRONT_CAMERA", "1") not in ("0", "false", "False", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_DIM = int(os.environ.get("MAX_DIM", "2200"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "85"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "40")) * 1024 * 1024

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
HIDDEN_DIR.mkdir(parents=True, exist_ok=True)

STATIC = Path(__file__).parent / "static"

# Foto-ID-Format: 13+ stellige ms-Epoche + "-" + 8 Hex (lexikographisch == chronologisch)
ID_RE = re.compile(r"^\d{13,}-[a-f0-9]{8}$")

app = FastAPI(title="Court 7 Photowall")


def _new_id() -> str:
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


def _list_ids(since: str = "") -> list[str]:
    ids = [p.stem for p in UPLOAD_DIR.glob("*.jpg")]
    if since:
        ids = [i for i in ids if i > since]
    ids.sort()
    return ids


def _require_admin(token: str) -> None:
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Nicht autorisiert")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "photos": len(_list_ids()), "heic": HEIC_OK}


@app.get("/api/config")
def get_config(request: Request):
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    return {
        "title": TITLE,
        "slideSeconds": SLIDE_SECONDS,
        "frontCamera": FRONT_CAMERA,
        "publicUrl": base,
        "maxDim": MAX_DIM,
    }


@app.get("/api/photos")
def get_photos(since: str = Query("")):
    ids = _list_ids(since)
    return {"photos": [{"id": i, "url": f"/photo/{i}"} for i in ids], "count": len(_list_ids())}


@app.get("/photo/{pid}")
def get_photo(pid: str):
    if not ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="Not found")
    f = UPLOAD_DIR / f"{pid}.jpg"
    if not f.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        f,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Leere Datei")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Datei zu groß")
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)  # EXIF-Orientierung anwenden
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((MAX_DIM, MAX_DIM), Image.Resampling.LANCZOS)
    except Exception:
        raise HTTPException(status_code=400, detail="Kein gültiges Bild")

    pid = _new_id()
    out = UPLOAD_DIR / f"{pid}.jpg"
    img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return {"id": pid, "url": f"/photo/{pid}"}


@app.get("/api/admin/check")
def admin_check(token: str = Query("")):
    _require_admin(token)
    return {"ok": True}


@app.get("/api/admin/photos")
def admin_photos(token: str = Query("")):
    _require_admin(token)
    ids = _list_ids()
    ids.reverse()  # neueste zuerst
    return {"photos": [{"id": i, "url": f"/photo/{i}"} for i in ids]}


@app.post("/api/photos/{pid}/hide")
def hide_photo(pid: str, token: str = Query("")):
    _require_admin(token)
    if not ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="Not found")
    f = UPLOAD_DIR / f"{pid}.jpg"
    if f.exists():
        f.rename(HIDDEN_DIR / f"{pid}.jpg")
    return {"ok": True}


@app.get("/api/qr.png")
def qr_png(request: Request):
    target = PUBLIC_URL or str(request.base_url).rstrip("/")
    qr = qrcode.QRCode(border=1, box_size=10)
    qr.add_data(target or "/")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# Seiten
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "upload.html")


@app.get("/tv")
def tv():
    return FileResponse(STATIC / "tv.html")


@app.get("/moderate")
def moderate():
    return FileResponse(STATIC / "moderate.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
