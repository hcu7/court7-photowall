# Court 7 Photowall

Eine Event-Foto-Wall: Gäste laden vom **Handy** Selfies hoch, die live als
**Diashow auf einem Fernseher** erscheinen (Fire TV / Browser). Gebaut für
Geburtstage & Partys — komplett ENV-konfigurierbar und für jedes Event
wiederverwendbar.

Im Stil von `tennis-opc`: ein einzelnes FastAPI-File + statisches HTML,
Dockerfile, deployed auf Coolify (`*.court7.world`).

## Seiten

| Pfad | Zweck | Wo öffnen |
|---|---|---|
| `/` | Selfie aufnehmen & hochladen | Handy der Gäste (QR scannen) |
| `/tv` | Vollbild-Diashow, auto-aktualisierend | Fernseher / Fire TV |
| `/moderate` | Fotos entfernen (PIN-geschützt) | Handy des Gastgebers |

- Neue Fotos erscheinen sofort als „Spotlight" auf dem TV (mit `Neu! 🎉`),
  danach läuft die normale Endlos-Diashow weiter.
- TV blendet dauerhaft einen QR-Code + die URL ein → Gäste können jederzeit
  einsteigen.
- `/tv?seconds=5` überschreibt die Anzeigedauer pro Bild live.

## Konfiguration (Coolify-ENV)

| ENV | Default | Bedeutung |
|---|---|---|
| `TITLE` | `Happy Birthday! 🎉` | Titel auf Handy & TV |
| `PUBLIC_URL` | _(leer → aus Request)_ | Basis-URL für QR-Code |
| `SLIDE_SECONDS` | `4` | Sekunden pro Bild |
| `FRONT_CAMERA` | `1` | `1` = Selfie-Kamera zuerst, `0` = Rückkamera |
| `ADMIN_TOKEN` | _(leer)_ | PIN für `/moderate` (leer = Moderation aus) |
| `MAX_DIM` | `2200` | Längste Kante (px), auf die Fotos verkleinert werden |
| `MAX_UPLOAD_MB` | `40` | Max. Upload-Größe |

## Technik

- **Upload:** Client verkleinert das Foto via Canvas (EXIF-korrekt) vor dem
  Senden; Server re-prozessiert als Sicherheitsnetz (EXIF-Transpose, HEIC via
  `pillow-heif`, JPEG-Recompress). iPhone-HEIC funktioniert über beide Wege.
- **Live-Update:** TV pollt `/api/photos?since=<id>` alle 2,5 s (robust hinter
  Cloudflare/Traefik — kein SSE nötig).
- **Speicher:** Fotos liegen als JPEG unter `/data/uploads` (persistentes
  Coolify-Volume). Versteckte Fotos wandern nach `/data/hidden` (nicht
  gelöscht — wiederherstellbar).
- **Querformat:** Handy-Seite weist auf Querformat hin und warnt bei Hochkant,
  blockt aber nicht (lieber ein Foto als keins).

## Lokal testen

```bash
pip install -r requirements.txt
DATA_DIR=./data TITLE="Test 🎉" ADMIN_TOKEN=test1234 \
  uvicorn main:app --reload --port 8000
# Handy:  http://localhost:8000/
# TV:     http://localhost:8000/tv
# Mod:    http://localhost:8000/moderate
```

## Deployment

Coolify-App `photowall` auf Server `court7-cx53`, Build Pack Dockerfile,
Port 8000, Domain `60.court7.world` (Wildcard `*.court7.world` →
`178.105.31.62`, Cloudflare-proxied). Personalisierung ausschließlich über ENV.

**Repo ist privat.** Der Coolify-MCP hat keinen „private-repo-mit-Deploy-Key"-
Endpoint, daher zwei Wege:

1. **Git-basiert (genutzt):** Repo kurz auf `public` schalten, Coolify-Deploy
   triggern (zieht `main`), nach erfolgreichem Smoke-Test wieder auf `private`.
   Der laufende Container hängt nicht am Repo — bleibt privat unbeschadet.
   Für ein erneutes Git-Deploy diesen Schritt wiederholen ODER in der
   Coolify-UI einen Deploy-Key hinterlegen (Muster wie die anderen Apps).
2. **Self-contained (Fallback, ohne GitHub-Zugriff):**
   `scripts/build-inline-dockerfile.sh > /tmp/inline.Dockerfile` erzeugt ein
   Dockerfile mit eingebackenem Code (gzip+base64 + SHA-256-Check). Damit kann
   die App rein lokal/offline gebaut werden.

**Wichtig (Healthcheck):** Das `python:3.12-slim`-Image braucht `curl` im Image,
weil Coolifys Healthcheck `curl`/`wget` aufruft — sonst 503 + Rollback trotz
laufender App. Ist im Dockerfile installiert.
