"""Court 7 Photowall — Backup-Worker (getrennt von der öffentlichen App).

Läuft als eigener Coolify-Dienst OHNE öffentliche Domain. Pollt dieselbe
Postgres-DB nach noch nicht gesicherten Fotos und lädt sie in einen Google-
Drive-Ordner hoch (zweite Sicherung neben der persistenten DB). Die Google-
Credentials liegen NUR hier, nicht in der öffentlich erreichbaren App.

ENV:
  DATABASE_URL        Postgres (gleiche DB wie die App)
  DRIVE_FOLDER_ID     Ziel-Ordner in Google Drive
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN   OAuth
  BACKUP_INTERVAL     Sekunden zwischen den Durchläufen (default 90)
"""
import base64
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg
import requests
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

DATABASE_URL = os.environ["DATABASE_URL"]
FOLDER = os.environ["DRIVE_FOLDER_ID"]
INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "90"))
BATCH = int(os.environ.get("BACKUP_BATCH", "25"))

# --- KI-Bewertung (Vertex Gemini, EU) ---
VERTEX_SA_JSON = os.environ.get("VERTEX_SA_JSON", "")
try:
    _SA_INFO = json.loads(VERTEX_SA_JSON) if VERTEX_SA_JSON else {}
except Exception:
    _SA_INFO = {}
# Projekt autoritativ aus der SA-JSON (sonst ENV), Whitespace strippen.
VERTEX_PROJECT = (_SA_INFO.get("project_id") or os.environ.get("VERTEX_PROJECT_ID", "")).strip()
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "europe-west3").strip()
VERTEX_MODEL = os.environ.get("VERTEX_MODEL", "gemini-2.5-flash").strip()
SCORE_BATCH = int(os.environ.get("SCORE_BATCH", "15"))
SCORING_ON = bool(VERTEX_SA_JSON and VERTEX_PROJECT)

_SCORE_PROMPT = (
    "Du bist Juror einer Party-Foto-Wand. Bewerte STRENG und konsistent. "
    "Antworte NUR mit JSON: "
    '{"photo_score": <0-100 ganzzahlig>, '
    '"photo_desc": "<charmante deutsche Kurzbeschreibung des Fotos, max 12 Woerter>", '
    '"comm_score": <0-100 ganzzahlig oder null>}. '
    "photo_score = wie originell/witzig/besonders das FOTO ist "
    "(0815-Schnappschuss ~40, kreativ/lustig/ueberraschend ~80+). "
    "comm_score = wie originell die angegebene GEMEINSAMKEIT ist "
    "(banal wie 'beide moegen Pizza' ~30, spezifisch/ueberraschend ~80+); "
    "null wenn keine Gemeinsamkeit angegeben. "
    "Angegebene Gemeinsamkeit: {COMMENT}"
)

_sa_creds = None


def _vertex_token() -> str:
    global _sa_creds
    if _sa_creds is None:
        _sa_creds = service_account.Credentials.from_service_account_info(
            _SA_INFO, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _sa_creds.valid:
        _sa_creds.refresh(GAuthRequest())
    return _sa_creds.token


def score_photo(jpg: bytes, comment: str):
    """Returns (photo_score, photo_desc, comm_score or None)."""
    body = {
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpg).decode()}},
            {"text": _SCORE_PROMPT.replace("{COMMENT}", comment or "(keine)")},
        ]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "maxOutputTokens": 1024,
            "thinkingConfig": {"thinkingBudget": 0},  # 2.5-flash: Thinking aus, sonst MAX_TOKENS-Abbruch
        },
    }
    url = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{VERTEX_PROJECT}"
        f"/locations/{VERTEX_LOCATION}/publishers/google/models/{VERTEX_MODEL}:generateContent"
    )
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {_vertex_token()}", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    d = json.loads(txt)
    ps = float(d.get("photo_score") or 0)
    cs = d.get("comm_score")
    cs = float(cs) if cs is not None else None
    return ps, (d.get("photo_desc") or "")[:200], cs


def _creds():
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],
    )


def _drive():
    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


def ensure_schema():
    with psycopg.connect(DATABASE_URL, connect_timeout=15) as c:
        for ddl in (
            "ALTER TABLE photos ADD COLUMN IF NOT EXISTS backed_up BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE photos ADD COLUMN IF NOT EXISTS scored BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE photos ADD COLUMN IF NOT EXISTS comm_score DOUBLE PRECISION",
            "ALTER TABLE photos ADD COLUMN IF NOT EXISTS photo_score DOUBLE PRECISION",
            "ALTER TABLE photos ADD COLUMN IF NOT EXISTS photo_desc TEXT",
        ):
            c.execute(ddl)
        c.commit()


def run_scoring() -> int:
    if not SCORING_ON:
        return 0
    with psycopg.connect(DATABASE_URL, connect_timeout=15) as c:
        rows = c.execute(
            "SELECT id, data, comment FROM photos WHERE scored=FALSE AND hidden=FALSE "
            "ORDER BY created ASC LIMIT %s",
            (SCORE_BATCH,),
        ).fetchall()
    done = 0
    for pid, data, comment in rows:
        try:
            ps, desc, cs = score_photo(bytes(data), comment)
        except Exception as e:
            print(f"[score] error {pid}: {e}", flush=True)
            continue  # spaeter erneut versuchen
        with psycopg.connect(DATABASE_URL, connect_timeout=15) as c:
            c.execute(
                "UPDATE photos SET scored=TRUE, photo_score=%s, photo_desc=%s, comm_score=%s WHERE id=%s",
                (ps, desc, cs, pid),
            )
            c.commit()
        done += 1
        print(f"[score] {pid} photo={ps} comm={cs}", flush=True)
    return done


def run_once(svc) -> int:
    with psycopg.connect(DATABASE_URL, connect_timeout=15) as c:
        rows = c.execute(
            "SELECT id, data, comment FROM photos WHERE backed_up=FALSE ORDER BY created ASC LIMIT %s",
            (BATCH,),
        ).fetchall()
    done = 0
    for pid, data, comment in rows:
        meta = {"name": f"photo_{pid}.jpg", "parents": [FOLDER]}
        if comment:
            meta["description"] = comment
        media = MediaIoBaseUpload(io.BytesIO(bytes(data)), mimetype="image/jpeg", resumable=False)
        svc.files().create(body=meta, media_body=media, fields="id").execute()
        with psycopg.connect(DATABASE_URL, connect_timeout=15) as c:
            c.execute("UPDATE photos SET backed_up=TRUE WHERE id=%s", (pid,))
            c.commit()
        done += 1
        print(f"[backup] uploaded {pid}", flush=True)
    return done


# Mini-Healthserver, damit der Docker-Healthcheck (curl :8000/healthz) grün ist.
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _serve_health():
    HTTPServer(("0.0.0.0", 8000), _H).serve_forever()


def main():
    threading.Thread(target=_serve_health, daemon=True).start()
    while True:
        try:
            ensure_schema()
            break
        except Exception as e:
            print(f"[backup] schema wait: {e}", flush=True)
            time.sleep(5)
    print(
        f"[backup] gestartet · interval={INTERVAL}s · folder={FOLDER} · "
        f"scoring={'an ('+VERTEX_MODEL+'/'+VERTEX_LOCATION+')' if SCORING_ON else 'aus'}",
        flush=True,
    )
    while True:
        try:
            n = run_once(_drive())
            if n:
                print(f"[backup] {n} Foto(s) gesichert", flush=True)
        except Exception as e:
            print(f"[backup] error: {e}", flush=True)
        try:
            s = run_scoring()
            if s:
                print(f"[score] {s} Foto(s) bewertet", flush=True)
        except Exception as e:
            print(f"[score] loop error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
