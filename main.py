"""Court 7 Photowall — FastAPI Selfie-Upload + Live-TV-Diashow.

Single-file app im Stil von tennis-opc. Gäste laden vom Handy Selfies hoch,
die als Live-Diashow auf einem Fernseher erscheinen.

Speicherung: Fotos (als Bytes), Kommentar und Sortierung liegen in einer
DATENBANK — in Produktion Postgres (DATABASE_URL gesetzt; Coolify hält das
DB-Volume persistent + sichert es), lokal SQLite (DATA_DIR/photowall.db).
So überlebt alles jedes Redeploy ("immer alles speichern").

Alle Personalisierungen kommen aus der Umgebung, nicht aus dem Code.

Endpoints
---------
  GET  /                        Handy-Upload-Seite
  GET  /tv                      TV-Diashow (Vollbild, auto-aktualisierend)
  GET  /moderate                Admin: alle Fotos sehen/bearbeiten (PIN)
  POST /api/upload              Foto hochladen (file, optional comment)
  GET  /api/photos              Voller Feed in Anzeige-Reihenfolge (+ comment)
  GET  /photo/{id}              Foto ausliefern (JPEG)
  GET  /api/config              Laufzeit-Konfig fürs Frontend
  GET  /api/qr.png[?data=URL]   QR-Code
  GET  /api/admin/check         PIN prüfen
  GET  /api/admin/photos        Alle Fotos (Anzeige-Reihenfolge) für Admin
  POST /api/photos/{id}/hide    Foto entfernen (soft-delete; Token)
  POST /api/photos/{id}/comment Kommentar setzen/löschen (Token)
  POST /api/order               Reihenfolge setzen (Token, JSON {ids:[...]})
  GET  /healthz                 Healthcheck
"""
import io
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

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover
    HEIC_OK = False

import qrcode

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
TITLE = os.environ.get("TITLE", "Happy Birthday! \U0001F389")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SLIDE_SECONDS = float(os.environ.get("SLIDE_SECONDS", "4"))
FRONT_CAMERA = os.environ.get("FRONT_CAMERA", "1") not in ("0", "false", "False", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_DIM = int(os.environ.get("MAX_DIM", "2200"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "85"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "40")) * 1024 * 1024
MAX_COMMENT = 280

ID_RE = re.compile(r"^\d{13,}-[a-f0-9]{8}$")

# ---------------------------------------------------------------------------
# Datenbank-Schicht (Postgres in Prod, SQLite lokal)
# ---------------------------------------------------------------------------
PG = bool(DATABASE_URL)
if PG:
    import psycopg

    PH = "%s"
    _DDL = (
        "CREATE TABLE IF NOT EXISTS photos ("
        "id TEXT PRIMARY KEY, data BYTEA NOT NULL, comment TEXT NOT NULL DEFAULT '', "
        "sort DOUBLE PRECISION NOT NULL DEFAULT 0, hidden BOOLEAN NOT NULL DEFAULT FALSE, "
        "created DOUBLE PRECISION NOT NULL DEFAULT 0)"
    )
    _FALSE = "FALSE"
    _TRUE = "TRUE"

    def _connect():
        return psycopg.connect(DATABASE_URL, connect_timeout=10)
else:
    import sqlite3

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _DBFILE = str(DATA_DIR / "photowall.db")
    PH = "?"
    _DDL = (
        "CREATE TABLE IF NOT EXISTS photos ("
        "id TEXT PRIMARY KEY, data BLOB NOT NULL, comment TEXT NOT NULL DEFAULT '', "
        "sort REAL NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0, "
        "created REAL NOT NULL DEFAULT 0)"
    )
    _FALSE = "0"
    _TRUE = "1"

    def _connect():
        return sqlite3.connect(_DBFILE)


def _exec(sql: str, params=(), fetch=None):
    sql = sql.replace("?", PH) if PH != "?" else sql
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        out = None
        if fetch == "one":
            out = cur.fetchone()
        elif fetch == "all":
            out = cur.fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def init_db():
    _exec(_DDL)
    # Backup-Flag (vom getrennten Backup-Worker genutzt) — idempotent nachziehen.
    if PG:
        _exec("ALTER TABLE photos ADD COLUMN IF NOT EXISTS backed_up BOOLEAN NOT NULL DEFAULT FALSE")
    else:
        try:
            _exec("ALTER TABLE photos ADD COLUMN backed_up INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass


def db_insert(pid: str, data: bytes, comment: str, sort: float):
    _exec(
        "INSERT INTO photos (id, data, comment, sort, created) VALUES (?, ?, ?, ?, ?)",
        (pid, data, comment, sort, sort),
    )


def db_photo(pid: str):
    row = _exec(f"SELECT data FROM photos WHERE id=? AND hidden={_FALSE}", (pid,), "one")
    return bytes(row[0]) if row else None


def db_exists(pid: str) -> bool:
    return _exec(f"SELECT 1 FROM photos WHERE id=? AND hidden={_FALSE}", (pid,), "one") is not None


def db_list():
    return _exec(
        f"SELECT id, comment FROM photos WHERE hidden={_FALSE} ORDER BY sort ASC, id ASC",
        (),
        "all",
    ) or []


def db_count() -> int:
    row = _exec(f"SELECT COUNT(*) FROM photos WHERE hidden={_FALSE}", (), "one")
    return int(row[0]) if row else 0


def db_hide(pid: str):
    _exec(f"UPDATE photos SET hidden={_TRUE} WHERE id=?", (pid,))


def db_set_comment(pid: str, text: str):
    _exec("UPDATE photos SET comment=? WHERE id=?", (text, pid))


def db_set_order(ids: list[str]):
    if not ids:
        return
    sql = f"UPDATE photos SET sort={PH} WHERE id={PH}"
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.executemany(sql, [(float(i), pid) for i, pid in enumerate(ids)])
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
def _new_id() -> str:
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


def _clean_comment(text: str) -> str:
    text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
    text = " ".join(text.split())
    return text[:MAX_COMMENT].strip()


def _require_admin(token: str) -> None:
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Nicht autorisiert")


STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Court 7 Photowall")
init_db()


class OrderIn(BaseModel):
    ids: list[str]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    try:
        n = db_count()
        return {"ok": True, "photos": n, "heic": HEIC_OK, "db": "pg" if PG else "sqlite"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db: {e}")


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
    rows = db_list()
    return {
        "photos": [{"id": r[0], "url": f"/photo/{r[0]}", "comment": r[1] or ""} for r in rows],
        "count": len(rows),
    }


@app.get("/photo/{pid}")
def get_photo(pid: str):
    if not ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="Not found")
    data = db_photo(pid)
    if data is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        content=data,
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
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((MAX_DIM, MAX_DIM), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Kein gültiges Bild")

    pid = _new_id()
    text = _clean_comment(comment)
    db_insert(pid, buf.getvalue(), text, float(int(time.time() * 1000)))
    return {"id": pid, "url": f"/photo/{pid}", "comment": text}


@app.get("/api/admin/check")
def admin_check(token: str = Query("")):
    _require_admin(token)
    return {"ok": True}


@app.get("/api/admin/photos")
def admin_photos(token: str = Query("")):
    _require_admin(token)
    rows = db_list()
    return {"photos": [{"id": r[0], "url": f"/photo/{r[0]}", "comment": r[1] or ""} for r in rows]}


@app.post("/api/photos/{pid}/hide")
def hide_photo(pid: str, token: str = Query("")):
    _require_admin(token)
    if not ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="Not found")
    db_hide(pid)  # soft-delete: bleibt in der DB, nur ausgeblendet
    return {"ok": True}


@app.post("/api/photos/{pid}/comment")
def set_comment(pid: str, comment: str = Form(""), token: str = Query("")):
    _require_admin(token)
    if not ID_RE.match(pid) or not db_exists(pid):
        raise HTTPException(status_code=404, detail="Not found")
    db_set_comment(pid, _clean_comment(comment))
    return {"ok": True, "comment": _clean_comment(comment)}


@app.post("/api/order")
def set_order(body: OrderIn, token: str = Query("")):
    _require_admin(token)
    clean = [i for i in body.ids if ID_RE.match(i)]
    db_set_order(clean)
    return {"ok": True, "count": len(clean)}


@app.get("/api/qr.png")
def qr_png(request: Request, data: str = Query("")):
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
