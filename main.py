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
import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
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
MAX_COMMENT = 280

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
HIDDEN_DIR.mkdir(parents=True, exist_ok=True)
ORDER_FILE = DATA_DIR / "order.json"

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


def _load_order() -> list[str]:
    try:
        data = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
        return [i for i in data if isinstance(i, str)]
    except Exception:
        return []


def _save_order(ids: list[str]) -> None:
    ORDER_FILE.write_text(json.dumps(ids), encoding="utf-8")


def _ordered_ids() -> list[str]:
    """Anzeige-Reihenfolge: zuerst die in order.json gespeicherte Reihenfolge
    (nur noch existierende), dann der Rest chronologisch (neue Uploads hängen
    hinten an, bis der Admin umsortiert)."""
    existing = [p.stem for p in UPLOAD_DIR.glob("*.jpg")]
    existing_set = set(existing)
    ordered = [i for i in _load_order() if i in existing_set]
    in_order = set(ordered)
    rest = sorted(i for i in existing if i not in in_order)
    return ordered + rest


class OrderIn(BaseModel):
    ids: list[str]


def _clean_comment(text: str) -> str:
    # Steuerzeichen raus, Whitespace normalisieren, kappen.
    text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
    text = " ".join(text.split())
    return text[:MAX_COMMENT].strip()


def _read_comment(pid: str) -> str:
    f = UPLOAD_DIR / f"{pid}.txt"
    if f.exists():
        try:
            return f.read_text(encoding="utf-8")[:MAX_COMMENT]
        except Exception:
            return ""
    return ""


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
def get_photos():
    ids = _ordered_ids()
    return {
        "photos": [{"id": i, "url": f"/photo/{i}", "comment": _read_comment(i)} for i in ids],
        "count": len(ids),
    }


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
async def upload(file: UploadFile = File(...), comment: str = Form("")):
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

    text = _clean_comment(comment)
    if text:
        (UPLOAD_DIR / f"{pid}.txt").write_text(text, encoding="utf-8")
    return {"id": pid, "url": f"/photo/{pid}", "comment": text}


@app.get("/api/admin/check")
def admin_check(token: str = Query("")):
    _require_admin(token)
    return {"ok": True}


@app.get("/api/admin/photos")
def admin_photos(token: str = Query("")):
    _require_admin(token)
    ids = _ordered_ids()  # exakt die TV-Reihenfolge
    return {"photos": [{"id": i, "url": f"/photo/{i}", "comment": _read_comment(i)} for i in ids]}


@app.post("/api/photos/{pid}/hide")
def hide_photo(pid: str, token: str = Query("")):
    _require_admin(token)
    if not ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="Not found")
    for ext in ("jpg", "txt"):
        f = UPLOAD_DIR / f"{pid}.{ext}"
        if f.exists():
            f.rename(HIDDEN_DIR / f"{pid}.{ext}")
    _save_order([i for i in _load_order() if i != pid])
    return {"ok": True}


@app.post("/api/photos/{pid}/comment")
def set_comment(pid: str, comment: str = Form(""), token: str = Query("")):
    _require_admin(token)
    if not ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="Not found")
    if not (UPLOAD_DIR / f"{pid}.jpg").exists():
        raise HTTPException(status_code=404, detail="Not found")
    text = _clean_comment(comment)
    f = UPLOAD_DIR / f"{pid}.txt"
    if text:
        f.write_text(text, encoding="utf-8")
    elif f.exists():
        f.unlink()
    return {"ok": True, "comment": text}


@app.post("/api/order")
def set_order(body: OrderIn, token: str = Query("")):
    _require_admin(token)
    clean = [i for i in body.ids if ID_RE.match(i)]
    _save_order(clean)
    return {"ok": True, "count": len(clean)}


@app.get("/api/qr.png")
def qr_png(request: Request, data: str = Query("")):
    # data erlaubt beliebige Ziel-URL (z.B. Produkt-Link für die TV-Werbung);
    # nur http/https zulassen. Ohne data -> Upload-URL.
    data = data.strip()
    if data.startswith(("http://", "https://")):
        target = data[:512]
    else:
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
