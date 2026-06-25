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
import io
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

DATABASE_URL = os.environ["DATABASE_URL"]
FOLDER = os.environ["DRIVE_FOLDER_ID"]
INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "90"))
BATCH = int(os.environ.get("BACKUP_BATCH", "25"))


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
        c.execute(
            "ALTER TABLE photos ADD COLUMN IF NOT EXISTS backed_up BOOLEAN NOT NULL DEFAULT FALSE"
        )
        c.commit()


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
    print(f"[backup] gestartet · interval={INTERVAL}s · folder={FOLDER}", flush=True)
    while True:
        try:
            svc = _drive()
            n = run_once(svc)
            if n:
                print(f"[backup] {n} Foto(s) gesichert", flush=True)
        except Exception as e:
            print(f"[backup] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
