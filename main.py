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
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    _TZ = None

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
APP_BOOT = str(int(time.time()))  # ändert sich bei jedem (Re)Deploy -> TV lädt automatisch neu

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


def _add_col(pg_def: str, sqlite_def: str):
    if PG:
        _exec(f"ALTER TABLE photos ADD COLUMN IF NOT EXISTS {pg_def}")
    else:
        try:
            _exec(f"ALTER TABLE photos ADD COLUMN {sqlite_def}")
        except Exception:
            pass


def init_db():
    _exec(_DDL)
    # Zusatz-Spalten idempotent nachziehen (Backup-Worker + KI-Bewertung).
    _add_col("backed_up BOOLEAN NOT NULL DEFAULT FALSE", "backed_up INTEGER NOT NULL DEFAULT 0")
    _add_col("scored BOOLEAN NOT NULL DEFAULT FALSE", "scored INTEGER NOT NULL DEFAULT 0")
    _add_col("comm_score DOUBLE PRECISION", "comm_score REAL")
    _add_col("photo_score DOUBLE PRECISION", "photo_score REAL")
    _add_col("photo_desc TEXT", "photo_desc TEXT")
    _exec("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")


def setting_get(key: str, default: str = "") -> str:
    row = _exec("SELECT value FROM settings WHERE key=?", (key,), "one")
    return row[0] if row and row[0] is not None else default


def setting_set(key: str, value: str):
    if PG:
        _exec(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (key, value),
        )
    else:
        _exec("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


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
        "version": APP_BOOT,
    }


_RESIZE_CACHE: dict = {}  # (pid, w) -> jpeg bytes; entlastet den Fire-TV (kleinere Bilder)

@app.get("/photo/{pid}")
def get_photo(pid: str, w: int = 0):
    if not ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="Not found")
    hdr = {"Cache-Control": "public, max-age=31536000, immutable"}
    # Verkleinerte Variante (TV: scharfes fg ~1600px, Blur-Hintergrund ~320px) — gecacht.
    if w and 48 <= w <= 2200:
        ck = (pid, w)
        out = _RESIZE_CACHE.get(ck)
        if out is None:
            data = db_photo(pid)
            if data is None:
                raise HTTPException(status_code=404, detail="Not found")
            try:
                im = Image.open(io.BytesIO(data))
                im = ImageOps.exif_transpose(im)
                if im.mode != "RGB":
                    im = im.convert("RGB")
                im.thumbnail((w, w), Image.Resampling.LANCZOS)
                b = io.BytesIO()
                im.save(b, "JPEG", quality=82, optimize=True)
                out = b.getvalue()
            except Exception:
                out = data  # Fallback: Original
            if len(_RESIZE_CACHE) > 600:
                _RESIZE_CACHE.clear()
            _RESIZE_CACHE[ck] = out
        return Response(content=out, media_type="image/jpeg", headers=hdr)
    data = db_photo(pid)
    if data is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=data, media_type="image/jpeg", headers=hdr)


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


# ---- Sieger-Show (KI-Wettbewerb) --------------------------------------------
def _compute_winners() -> dict:
    cw = _exec(
        f"SELECT id, comment, comm_score FROM photos WHERE hidden={_FALSE} "
        f"AND comment<>'' AND comm_score IS NOT NULL ORDER BY comm_score DESC, sort ASC LIMIT 1",
        (), "one",
    )
    pw = _exec(
        f"SELECT id, photo_desc, photo_score FROM photos WHERE hidden={_FALSE} "
        f"AND photo_score IS NOT NULL ORDER BY photo_score DESC, sort ASC LIMIT 1",
        (), "one",
    )
    out = {}
    if cw:
        out["commonality"] = {"id": cw[0], "text": cw[1], "url": f"/photo/{cw[0]}", "score": cw[2]}
    if pw:
        out["photo"] = {"id": pw[0], "desc": pw[1] or "", "url": f"/photo/{pw[0]}", "score": pw[2]}
    return out


def _current_winners() -> dict:
    """Bei beendetem Wettbewerb die eingefrorenen Sieger, sonst live berechnet."""
    if setting_get("comp_state", "live") == "ended":
        try:
            return json.loads(setting_get("winners_locked", "{}"))
        except Exception:
            return {}
    return _compute_winners()


def _deadline_passed(end: str) -> bool:
    if not end:
        return False
    try:
        dt = datetime.fromisoformat(end)
        if dt.tzinfo is None and _TZ is not None:
            dt = dt.replace(tzinfo=_TZ)
        now = datetime.now(_TZ) if _TZ is not None else datetime.now()
        return now >= dt
    except Exception:
        return False


def _end_competition():
    setting_set("winners_locked", json.dumps(_compute_winners()))
    setting_set("comp_state", "ended")
    setting_set("winners_revealed", "0")  # Sieger erst NACH der Sieger-Show in der Diashow markieren


@app.post("/api/ceremony/start")
def ceremony_start(token: str = Query("")):
    _require_admin(token)
    setting_set("ceremony_active", "1")
    setting_set("winners_revealed", "1")  # Sieger gelten ab jetzt als gezeigt -> Badges in der Diashow
    return {"ok": True, "winners": _current_winners()}


@app.post("/api/ceremony/stop")
def ceremony_stop(token: str = Query("")):
    _require_admin(token)
    setting_set("ceremony_active", "0")
    return {"ok": True}


@app.post("/api/ceremony/end")
def ceremony_end(token: str = Query("")):
    """Wettbewerb beenden: Sieger einfrieren (werden danach in der Diashow markiert)."""
    _require_admin(token)
    _end_competition()
    return {"ok": True, "winners": _current_winners()}


@app.post("/api/ceremony/reopen")
def ceremony_reopen(token: str = Query("")):
    _require_admin(token)
    setting_set("comp_state", "live")
    setting_set("winners_locked", "")
    setting_set("winners_revealed", "0")
    return {"ok": True}


@app.post("/api/ceremony/deadline")
def ceremony_deadline(end: str = Form(""), token: str = Query("")):
    _require_admin(token)
    setting_set("competition_end", end.strip())
    return {"ok": True, "end": end.strip()}


@app.get("/api/ceremony")
def ceremony_get():
    state = setting_get("comp_state", "live")
    end = setting_get("competition_end", "")
    # Deadline erreicht? -> Wettbewerb automatisch beenden + Sieger-Show starten.
    if state == "live" and _deadline_passed(end):
        _end_competition()
        setting_set("ceremony_active", "1")
        setting_set("winners_revealed", "1")  # Deadline startet Sieger-Show automatisch
        state = "ended"
    return {
        "active": setting_get("ceremony_active", "0") == "1",
        "state": state,
        "end": end,
        "revealed": setting_get("winners_revealed", "0") == "1",
        "winners": _current_winners(),
    }


@app.get("/api/admin/standings")
def admin_standings(token: str = Query("")):
    _require_admin(token)
    comm = _exec(
        f"SELECT id, comment, comm_score FROM photos WHERE hidden={_FALSE} AND comment<>'' "
        f"AND comm_score IS NOT NULL ORDER BY comm_score DESC, sort ASC LIMIT 8", (), "all") or []
    phot = _exec(
        f"SELECT id, photo_desc, photo_score FROM photos WHERE hidden={_FALSE} "
        f"AND photo_score IS NOT NULL ORDER BY photo_score DESC, sort ASC LIMIT 8", (), "all") or []
    scored = (_exec(f"SELECT COUNT(*) FROM photos WHERE hidden={_FALSE} AND scored={_TRUE}", (), "one") or [0])[0]
    return {
        "state": setting_get("comp_state", "live"),
        "active": setting_get("ceremony_active", "0") == "1",
        "end": setting_get("competition_end", ""),
        "scored": int(scored), "total": db_count(),
        "commonalities": [{"id": r[0], "text": r[1], "url": f"/photo/{r[0]}", "score": r[2]} for r in comm],
        "photos": [{"id": r[0], "desc": r[1] or "", "url": f"/photo/{r[0]}", "score": r[2]} for r in phot],
    }


@app.post("/api/photos/{pid}/score")
def set_score(pid: str, comm_score: str | None = Form(None), photo_score: str | None = Form(None), token: str = Query("")):
    """Manuelle Score-Korrektur. Nur übergebene Felder werden geändert
    (leer = auf NULL setzen)."""
    _require_admin(token)
    if not ID_RE.match(pid) or not db_exists(pid):
        raise HTTPException(status_code=404, detail="Not found")

    def pf(s):
        s = s.strip().replace(",", ".")
        if s == "":
            return None
        try:
            return max(0.0, min(100.0, float(s)))
        except Exception:
            return None

    sets, params = [], []
    if comm_score is not None:
        sets.append(f"comm_score={PH}"); params.append(pf(comm_score))
    if photo_score is not None:
        sets.append(f"photo_score={PH}"); params.append(pf(photo_score))
    if not sets:
        return {"ok": True}
    sets.append(f"scored={_TRUE}")
    params.append(pid)
    _exec(f"UPDATE photos SET {', '.join(sets)} WHERE id={PH}", tuple(params))
    return {"ok": True}


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
_NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
def index():
    return FileResponse(STATIC / "upload.html", headers=_NOCACHE)


@app.get("/tv")
def tv():
    return FileResponse(STATIC / "tv.html", headers=_NOCACHE)


@app.get("/moderate")
def moderate():
    return FileResponse(STATIC / "moderate.html", headers=_NOCACHE)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
