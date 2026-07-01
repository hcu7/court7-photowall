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
import hashlib
import hmac
import io
import json
import os
import re
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    _TZ = None

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image, ImageOps
from starlette.background import BackgroundTask

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
DISPLAY_DIM = int(os.environ.get("DISPLAY_DIM", "1600"))   # an den TV ausgeliefertes Foto (klein -> schneller Decode auf Fire TV)
MED_DIM = int(os.environ.get("MED_DIM", "1000"))           # scharfe Album-Miniatur (lazy erzeugt + gecacht)
BG_DIM = int(os.environ.get("BG_DIM", "400"))              # winziger, vorab erzeugter Blur-Hintergrund
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "85"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "40")) * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 30_000_000   # Decompression-Bomb-Schutz: PIL bricht hart bei >2x (~60 MP) ab
MAX_COMMENT = 280
APP_BOOT = str(int(time.time()))  # ändert sich bei jedem (Re)Deploy -> TV lädt automatisch neu
ALBUM_HOST = os.environ.get("ALBUM_HOST", "").strip().lower()  # z.B. antje60.court7.world -> "/" zeigt das Album

ID_RE = re.compile(r"^\d{13,}-[a-f0-9]{8}$")

# Phasen des Abends (chronologische Reihenfolge) — gemeinsam von Worker, Moderate und Album genutzt.
PHASE_ORDER = ["geburtstag", "werdersee", "preparty", "auftritt", "party", "breakfast"]
PHASE_LABELS = {
    "geburtstag": "Donnerstag",
    "werdersee": "Werdersee",
    "preparty": "Goldene Stunde",
    "auftritt": "Musikalische Einlage",
    "party": "Die Tanznacht",
    "breakfast": "Frühstück im Grünen",
    "weitere": "Weitere",
}

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
    _add_col("data_bg BYTEA", "data_bg BLOB")   # vorab erzeugter Blur-Hintergrund (klein)
    _add_col("data_med BYTEA", "data_med BLOB")  # scharfe Album-Miniatur (~1000px, lazy erzeugt)
    _add_col("data_full BYTEA", "data_full BLOB")  # Original in voller Aufloesung (q95) — nur fuer Downloads (ab jetzt)
    _add_col("category TEXT", "category TEXT")          # Phase: geburtstag|werdersee|preparty|auftritt|party|breakfast
    _add_col("cat_source TEXT", "cat_source TEXT")      # ai | ai_fail | manual
    _exec("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")


def _resize_jpeg(raw: bytes, maxdim: int, quality: int) -> bytes:
    """Bytes -> EXIF-korrektes, auf maxdim begrenztes JPEG. Einmalig erzeugt, dann gespeichert."""
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail((maxdim, maxdim), Image.Resampling.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=quality, optimize=True)
    return b.getvalue()


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


def db_insert(pid: str, data: bytes, data_full: bytes, data_bg: bytes, comment: str, sort: float):
    _exec(
        "INSERT INTO photos (id, data, data_full, data_bg, comment, sort, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, data, data_full, data_bg, comment, sort, sort),
    )


def db_photo_full(pid: str):
    """Original (volle Aufloesung) falls vorhanden, sonst None -> Aufrufer faellt auf data zurueck."""
    row = _exec(f"SELECT data_full FROM photos WHERE id=? AND hidden={_FALSE}", (pid,), "one")
    return bytes(row[0]) if row and row[0] is not None else None


def db_photo(pid: str):
    row = _exec(f"SELECT data FROM photos WHERE id=? AND hidden={_FALSE}", (pid,), "one")
    return bytes(row[0]) if row else None


def db_photo_bg(pid: str):
    row = _exec(f"SELECT data_bg FROM photos WHERE id=? AND hidden={_FALSE}", (pid,), "one")
    return bytes(row[0]) if row and row[0] is not None else None


def db_set_bg(pid: str, data_bg: bytes):
    _exec("UPDATE photos SET data_bg=? WHERE id=?", (data_bg, pid))


def db_photo_med(pid: str):
    row = _exec(f"SELECT data_med FROM photos WHERE id=? AND hidden={_FALSE}", (pid,), "one")
    return bytes(row[0]) if row and row[0] is not None else None


def db_set_med(pid: str, data_med: bytes):
    _exec("UPDATE photos SET data_med=? WHERE id=?", (data_med, pid))


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
    if not ADMIN_TOKEN or not hmac.compare_digest(str(token), ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Nicht autorisiert")


# ---- Album-Zugang (Passwort, vom Admin gesetzt; Cookie-Session) -------------
def _album_hash(pw: str) -> str:
    # PBKDF2 mit ADMIN_TOKEN als Pepper/Salt -> Offline-Brute-Force erfordert zusaetzlich den Admin-Token.
    return hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), (ADMIN_TOKEN + ":antje-album").encode(), 100_000).hex()


def _album_cookie_value() -> str:
    # signiert mit ADMIN_TOKEN -> ohne Admin-Token nicht fälschbar
    return hmac.new(ADMIN_TOKEN.encode(), b"antje-album-v1", hashlib.sha256).hexdigest()


def _album_ok(request: Request) -> bool:
    if not ADMIN_TOKEN:
        return False
    c = request.cookies.get("album", "")
    return bool(c) and hmac.compare_digest(c, _album_cookie_value())


def _phase_key(cat) -> str:
    return cat if cat in PHASE_ORDER else "weitere"


def _album_rows():
    """Alle sichtbaren Fotos, sortiert nach Phase (chronologisch) dann Upload-Zeit."""
    rows = _exec(
        f"SELECT id, comment, category, created FROM photos WHERE hidden={_FALSE}", (), "all"
    ) or []
    order = {k: i for i, k in enumerate(PHASE_ORDER)}
    return sorted(rows, key=lambda r: (order.get(_phase_key(r[2]), len(PHASE_ORDER)), r[3] or 0, r[0]))


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


@app.get("/photo/{pid}")
def get_photo(pid: str, w: int = 0, full: int = 0):
    if not ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="Not found")
    hdr = {"Cache-Control": "public, max-age=31536000, immutable"}
    if full:  # Download in voller Aufloesung (Original). Fallback: Anzeige-Foto, falls kein Original gespeichert.
        data = db_photo_full(pid) or db_photo(pid)
        if data is None:
            raise HTTPException(status_code=404, detail="Not found")
        return Response(content=data, media_type="image/jpeg", headers=hdr)
    if w and w > 500:  # scharfe Album-Miniatur (~1000px): lazy erzeugen + cachen (aus dem Anzeige-Foto)
        med = db_photo_med(pid)
        if med is None:
            base = db_photo(pid)
            if base is None:
                raise HTTPException(status_code=404, detail="Not found")
            try:
                med = _resize_jpeg(base, MED_DIM, 82)
            except Exception:
                med = base
            db_set_med(pid, med)
        return Response(content=med, media_type="image/jpeg", headers=hdr)
    if w:  # winziger Blur-Hintergrund: vorab erzeugt; Altbestand einmalig nachziehen + speichern -> nie Resize-Stau
        bg = db_photo_bg(pid)
        if bg is None:
            base = db_photo(pid)
            if base is None:
                raise HTTPException(status_code=404, detail="Not found")
            try:
                bg = _resize_jpeg(base, BG_DIM, 72)
            except Exception:
                bg = base   # Fallback: Anzeigebild als BG (sehr selten)
            db_set_bg(pid, bg)   # immer speichern -> kein wiederholter Resize-Versuch pro Request
        return Response(content=bg, media_type="image/jpeg", headers=hdr)
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
        data = _resize_jpeg(raw, DISPLAY_DIM, JPEG_QUALITY)   # Anzeige-/Backup-Foto (klein, schnell)
        data_bg = _resize_jpeg(raw, BG_DIM, 72)               # Blur-Hintergrund (winzig)
        data_full = _resize_jpeg(raw, 100_000, 95)            # Original in voller Aufloesung (q95) -> Downloads
    except Image.DecompressionBombError:
        raise HTTPException(status_code=413, detail="Bild zu groß (Pixelmenge)")
    except Exception:
        raise HTTPException(status_code=400, detail="Kein gültiges Bild")

    pid = _new_id()
    text = _clean_comment(comment)
    db_insert(pid, data, data_full, data_bg, text, float(int(time.time() * 1000)))
    return {"id": pid, "url": f"/photo/{pid}", "comment": text}


@app.get("/api/admin/check")
def admin_check(token: str = Query("")):
    _require_admin(token)
    return {"ok": True}


@app.get("/api/admin/photos")
def admin_photos(token: str = Query("")):
    _require_admin(token)
    rows = _exec(
        f"SELECT id, comment, category, cat_source, created FROM photos WHERE hidden={_FALSE} "
        f"ORDER BY created ASC, id ASC", (), "all",
    ) or []
    return {
        "photos": [
            {"id": r[0], "url": f"/photo/{r[0]}", "comment": r[1] or "",
             "category": r[2] or "", "catSource": r[3] or ""}
            for r in rows
        ],
        "phases": [{"key": k, "label": PHASE_LABELS[k]} for k in PHASE_ORDER],
        "albumSet": bool(setting_get("album_pw", "")),
        "albumHost": ALBUM_HOST,
    }


@app.post("/api/admin/clear")
def admin_clear(token: str = Query("")):
    """Neustart: alle Fotos von der Wall ausblenden (NICHT hart löschen — bleiben in DB + Drive) + Wettbewerb auf 'läuft'."""
    _require_admin(token)
    n = db_count()
    _exec(f"UPDATE photos SET hidden={_TRUE} WHERE hidden={_FALSE}")
    _set_phase("running")
    return {"ok": True, "hidden": n}


@app.post("/api/admin/dedupe")
def admin_dedupe(token: str = Query(""), apply: int = Query(0)):
    """Findet exakte Bild-Duplikate (byte-identische data) und blendet Ueberzaehlige aus.
    Behaelt je Gruppe bevorzugt ein Foto MIT Kommentar (je distinktem Kommentar eins) und
    blendet kommentarlose sowie identisch-kommentierte Doppel aus. Soft-Delete (bleibt in DB+Drive).
    apply=0 -> nur zaehlen (Vorschau), apply=1 -> wirklich ausblenden."""
    _require_admin(token)
    rows = _exec(
        f"SELECT id, data, comment FROM photos WHERE hidden={_FALSE} ORDER BY created ASC, id ASC",
        (), "all",
    ) or []
    groups: dict = {}
    for r in rows:
        h = hashlib.sha256(bytes(r[1])).hexdigest()
        groups.setdefault(h, []).append((r[0], (r[2] or "").strip()))
    to_hide: list = []
    dup_groups = 0
    for items in groups.values():
        if len(items) < 2:
            continue
        dup_groups += 1
        kept: set = set()
        if any(cm for _, cm in items):           # mind. ein Kommentar vorhanden
            seen: set = set()
            for pid, cm in items:                # je distinktem Kommentar genau eins behalten
                if cm and cm not in seen:
                    seen.add(cm)
                    kept.add(pid)
        else:
            kept.add(items[0][0])                # alle kommentarlos -> aeltestes behalten
        to_hide.extend(pid for pid, _ in items if pid not in kept)
    if apply:
        for pid in to_hide:
            db_hide(pid)
    return {"ok": True, "applied": bool(apply), "groups": dup_groups, "removed": len(to_hide)}


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


@app.post("/api/photos/{pid}/category")
def set_category(pid: str, cat: str = Query(""), token: str = Query("")):
    """Manuelle Phasen-Zuordnung durch den Admin (überschreibt den KI-Vorschlag, ohne Bestätigung)."""
    _require_admin(token)
    if not ID_RE.match(pid) or not db_exists(pid):
        raise HTTPException(status_code=404, detail="Not found")
    if cat not in PHASE_ORDER:
        raise HTTPException(status_code=400, detail="Unbekannte Phase")
    _exec("UPDATE photos SET category=?, cat_source='manual' WHERE id=?", (cat, pid))
    return {"ok": True, "category": cat}


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
        f"SELECT id, photo_desc, photo_score, comment FROM photos WHERE hidden={_FALSE} "
        f"AND photo_score IS NOT NULL ORDER BY photo_score DESC, sort ASC LIMIT 1",
        (), "one",
    )
    out = {}
    if cw:
        out["commonality"] = {"id": cw[0], "text": cw[1], "url": f"/photo/{cw[0]}", "score": cw[2]}
    if pw:
        # comment = der vom Gast/Admin gesetzte Kommentar (das wird auf dem TV gezeigt, nicht photo_desc)
        out["photo"] = {"id": pw[0], "desc": pw[1] or "", "comment": pw[3] or "",
                        "url": f"/photo/{pw[0]}", "score": pw[2]}
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


# 4 manuelle Phasen (immer genau eine aktiv) — kein Zeitstempel/Deadline mehr:
#   running  = Wettbewerb laeuft (Fotos zaehlen, Mitmach-Spiel sichtbar)
#   waiting  = beendet, Siegerehrung folgt (Sieger eingefroren, noch nicht gezeigt)
#   ceremony = Siegerehrung laeuft (Sieger-Show auf dem TV)
#   done     = beendet, Sieger in der Diashow markiert (weiter hochladbar)
def _set_phase(phase: str):
    setting_set("ceremony_reveal", "none")  # jeder Phasenwechsel setzt die Sieger-Show auf den Wartescreen zurueck
    setting_set("winners_loop", "0")        # nur die Sieger-Loop-Phase schaltet ihn ein
    if phase == "running":
        setting_set("comp_state", "live")
        setting_set("ceremony_active", "0")
        setting_set("winners_revealed", "0")
        setting_set("winners_locked", "{}")
        return
    if setting_get("comp_state", "live") != "ended":
        _end_competition()  # friert die aktuellen Sieger ein
    if phase == "waiting":
        setting_set("ceremony_active", "0")
        setting_set("winners_revealed", "0")
    elif phase == "ceremony":
        setting_set("ceremony_active", "1")
        setting_set("winners_revealed", "1")
    elif phase == "done":
        setting_set("ceremony_active", "0")
        setting_set("winners_revealed", "1")
    elif phase == "winners":  # nur die beiden Sieger laufen als Loop auf dem TV
        setting_set("ceremony_active", "0")
        setting_set("winners_revealed", "1")
        setting_set("winners_loop", "1")


def _current_phase() -> str:
    if setting_get("comp_state", "live") != "ended":
        return "running"
    if setting_get("ceremony_active", "0") == "1":
        return "ceremony"
    if setting_get("winners_loop", "0") == "1":
        return "winners"
    if setting_get("winners_revealed", "0") == "1":
        return "done"
    return "waiting"


@app.post("/api/ceremony/phase")
def ceremony_phase(phase: str = Query(""), token: str = Query("")):
    _require_admin(token)
    if phase not in ("running", "waiting", "ceremony", "done", "winners"):
        raise HTTPException(status_code=400, detail="Unbekannte Phase")
    _set_phase(phase)
    return {"ok": True, "phase": _current_phase(), "winners": _current_winners()}


@app.post("/api/ceremony/reveal")
def ceremony_reveal(cat: str = Query(""), token: str = Query("")):
    """Moderator enthuellt eine Kategorie. Der TV spielt 1x Countdown + Reveal und laesst
    das Siegerfoto stehen, bis die naechste Kategorie/Phase kommt.
      none         = Wartescreen 'Siegerehrung'
      commonality  = Sieger 'Originellste Gemeinsamkeit'
      photo        = Sieger 'Originellstes Foto'
    Nur waehrend der Siegerehrung (Phase ceremony) sinnvoll."""
    _require_admin(token)
    if cat not in ("none", "commonality", "photo"):
        raise HTTPException(status_code=400, detail="Unbekannte Kategorie")
    if _current_phase() != "ceremony":
        setting_set("ceremony_active", "1")  # falls noch nicht in der Show: hineinschalten
        setting_set("winners_revealed", "1")
        if setting_get("comp_state", "live") != "ended":
            _end_competition()
    setting_set("ceremony_reveal", cat)
    return {"ok": True, "reveal": cat, "winners": _current_winners()}


@app.get("/api/ceremony")
def ceremony_get():
    return {
        "phase": _current_phase(),
        "state": setting_get("comp_state", "live"),
        "active": setting_get("ceremony_active", "0") == "1",
        "revealed": setting_get("winners_revealed", "0") == "1",
        "reveal": setting_get("ceremony_reveal", "none"),
        "winners": _current_winners(),
    }


@app.get("/api/admin/standings")
def admin_standings(token: str = Query("")):
    _require_admin(token)
    comm = _exec(
        f"SELECT id, comment, comm_score FROM photos WHERE hidden={_FALSE} AND comment<>'' "
        f"AND comm_score IS NOT NULL ORDER BY comm_score DESC, sort ASC LIMIT 60", (), "all") or []
    phot = _exec(
        f"SELECT id, photo_desc, photo_score FROM photos WHERE hidden={_FALSE} "
        f"AND photo_score IS NOT NULL ORDER BY photo_score DESC, sort ASC LIMIT 60", (), "all") or []
    scored = (_exec(f"SELECT COUNT(*) FROM photos WHERE hidden={_FALSE} AND scored={_TRUE}", (), "one") or [0])[0]
    return {
        "phase": _current_phase(),
        "state": setting_get("comp_state", "live"),
        "active": setting_get("ceremony_active", "0") == "1",
        "revealed": setting_get("winners_revealed", "0") == "1",
        "reveal": setting_get("ceremony_reveal", "none"),
        "winners": _current_winners(),
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


# ---- Fotoalbum (passwortgeschützt) ------------------------------------------
@app.post("/api/admin/album-password")
def set_album_password(pw: str = Form(""), token: str = Query("")):
    """Admin setzt das Album-Passwort (gehasht in settings; Klartext landet nie im Repo/Log)."""
    _require_admin(token)
    pw = (pw or "").strip()
    if len(pw) < 6:
        raise HTTPException(status_code=400, detail="Passwort zu kurz (min. 6 Zeichen)")
    setting_set("album_pw", _album_hash(pw))
    return {"ok": True}


@app.post("/api/album/login")
def album_login(pw: str = Form("")):
    stored = setting_get("album_pw", "")
    if not stored:
        raise HTTPException(status_code=403, detail="Album ist noch nicht freigeschaltet.")
    if not hmac.compare_digest(_album_hash(pw), stored):
        raise HTTPException(status_code=401, detail="Falsches Passwort")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "album", _album_cookie_value(), max_age=60 * 60 * 24 * 30,
        httponly=True, samesite="lax", secure=True, path="/",
    )
    return resp


@app.get("/api/album/state")
def album_state(request: Request):
    """Für die Album-Seite: ist ein Passwort gesetzt? Bin ich eingeloggt?"""
    return {"enabled": bool(setting_get("album_pw", "")), "authed": _album_ok(request), "title": TITLE}


@app.get("/api/album/photos")
def album_photos(request: Request):
    if not _album_ok(request):
        raise HTTPException(status_code=401, detail="Bitte einloggen")
    rows = _album_rows()
    return {
        "phases": [{"key": k, "label": PHASE_LABELS[k]} for k in PHASE_ORDER + ["weitere"]],
        "photos": [
            {"id": r[0], "url": f"/photo/{r[0]}", "comment": r[1] or "", "phase": _phase_key(r[2])}
            for r in rows
        ],
        "count": len(rows),
    }


@app.get("/api/album/zip")
def album_zip(request: Request, ids: str = Query(""), token: str = Query("")):
    """Zip-Download (alle oder ausgewählte Fotos). Zugang via Album-Cookie ODER Admin-Token.
    Kommentare werden NICHT mitgepackt (nur die Bilder), chronologisch benannt."""
    if not (_album_ok(request) or (ADMIN_TOKEN and token and hmac.compare_digest(token, ADMIN_TOKEN))):
        raise HTTPException(status_code=401, detail="Bitte einloggen")
    rows = _album_rows()
    sel = {i for i in ids.split(",") if ID_RE.match(i)} if ids.strip() else None
    if sel is not None:
        rows = [r for r in rows if r[0] in sel]
    if not rows:
        raise HTTPException(status_code=404, detail="Keine Fotos ausgewählt")
    if len(rows) > 1000:
        rows = rows[:1000]
    # In eine Temp-Datei streamen (ein Foto nach dem anderen) -> niedriger Speicherverbrauch.
    tmp = tempfile.NamedTemporaryFile(prefix="album-", suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:  # JPEGs sind schon komprimiert
            for i, r in enumerate(rows, 1):
                data = db_photo_full(r[0]) or db_photo(r[0])   # volle Aufloesung, Fallback Anzeige-Foto
                if data is None:
                    continue
                z.writestr(f"{i:03d}_{_phase_key(r[2])}.jpg", data)
        tmp.close()
    except Exception:
        tmp.close()
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Zip fehlgeschlagen")
    fname = "Antje-60-Fotoalbum.zip" if sel is None else "Antje-60-Auswahl.zip"

    def _cleanup(p=tmp.name):
        try:
            os.remove(p)
        except OSError:
            pass

    return FileResponse(tmp.name, media_type="application/zip", filename=fname,
                        background=BackgroundTask(_cleanup))


# ---------------------------------------------------------------------------
# Seiten
# ---------------------------------------------------------------------------
_NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
def index(request: Request):
    # Auf der Album-Subdomain (ALBUM_HOST) zeigt "/" direkt das Album, sonst die Upload-Seite.
    if ALBUM_HOST and request.url.hostname and request.url.hostname.lower() == ALBUM_HOST:
        return FileResponse(STATIC / "album.html", headers=_NOCACHE)
    return FileResponse(STATIC / "upload.html", headers=_NOCACHE)


@app.get("/tv")
def tv():
    return FileResponse(STATIC / "tv.html", headers=_NOCACHE)


@app.get("/moderate")
def moderate():
    return FileResponse(STATIC / "moderate.html", headers=_NOCACHE)


@app.get("/album")
def album():
    return FileResponse(STATIC / "album.html", headers=_NOCACHE)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
