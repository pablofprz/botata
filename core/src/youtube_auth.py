"""youtube_auth.py — token de USUARIO de YouTube (OAuth de Google, stdlib).

La `YOUTUBE_API_KEY` de T14 es app-only: sirve para LEER (buscar videos, listar
una playlist) y no puede escribir nada. Agregar un video a una playlist es una
escritura sobre la cuenta, así que necesita un token de usuario: una
autorización ÚNICA del admin en el browser (este módulo como CLI) y después
refresh headless para siempre con el refresh token guardado en
`context/.youtube_cache` (gitignored; a prod vía butterbot-secrets).

Uso:
    python src/youtube_auth.py --instance bots/x          # autorización inicial
    python src/youtube_auth.py --instance bots/x --test   # verifica el token

Consumidor runtime: `user_token()` — access token fresco o None si no hay cache
(el bot degrada graceful y dice qué falta, no explota).

Requisitos del lado de Google (una vez, en console.cloud.google.com):
- La YouTube Data API v3 habilitada en el proyecto.
- Un OAuth client de tipo "Web application" cuyo client id/secret son
  GOOGLE_OAUTH_ID / GOOGLE_OAUTH_SECRET del .env.
- El redirect URI de abajo declarado en ese client (si no, Google rebota con
  redirect_uri_mismatch antes de pedirte nada).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

log = logging.getLogger("botata.youtube_auth")

from instance import instance_dir

BASE_DIR = instance_dir()
CACHE_PATH = BASE_DIR / "context" / ".youtube_cache"

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DEFAULT_REDIRECT = "http://127.0.0.1:8890/callback"

# `youtube` (no el .readonly) porque el punto es ESCRIBIR en la playlist.
SCOPES = "https://www.googleapis.com/auth/youtube"

_token_cache: dict = {"value": None, "exp": 0.0}


# ─── Lo que pega un humano → lo que acepta la API ────────────────────────────
# Vive acá, y no en botata.py, porque la UI necesita exactamente la misma
# traducción al guardar una fuente: si las dos difieren, el panel deja guardar
# algo que el motor después no puede leer (pasó — una URL entera guardada como
# si fuera un id de playlist, 2026-08-01).

_LIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_CHANNEL_RE = re.compile(r"youtube\.com/channel/(UC[A-Za-z0-9_-]+)")
_HANDLE_RE = re.compile(r"youtube\.com/(@[A-Za-z0-9._-]+)")
_VIDEO_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def source_id(source: str) -> str:
    """Normaliza una fuente de YouTube a un id que la API entienda.

    Acepta lo que uno copia del browser (URL de playlist, de canal o el `@handle`)
    y también los ids pelados, que se devuelven tal cual. Lo que no matchea vuelve
    sin tocar: no es tarea de esta función decidir si existe.
    """
    s = (source or "").strip()
    if not s:
        return ""
    if m := _LIST_RE.search(s):
        return m.group(1)
    if m := _CHANNEL_RE.search(s):
        return m.group(1)
    if m := _HANDLE_RE.search(s):
        return m.group(1)
    return s


def video_id(referencia: str) -> str | None:
    """Id de video a partir de un link (watch, shorts, youtu.be, embed) o del id
    pelado. None si no parece ninguna de las dos cosas."""
    s = (referencia or "").strip()
    if not s:
        return None
    if m := _VIDEO_RE.search(s):
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_-]{11}", s) else None


# ─── Token ───────────────────────────────────────────────────────────────────
def _client_creds() -> tuple[str, str] | None:
    cid = os.environ.get("GOOGLE_OAUTH_ID")
    secret = os.environ.get("GOOGLE_OAUTH_SECRET")
    return (cid, secret) if cid and secret else None


def _token_request(data: dict) -> dict:
    cid, secret = _client_creds()  # el caller ya validó
    body = urllib.parse.urlencode({**data, "client_id": cid, "client_secret": secret})
    req = urllib.request.Request(
        _TOKEN_URL, data=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_cache() -> dict | None:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")


def user_token() -> str | None:
    """Access token de usuario fresco, refrescado headless. None = sin autorizar
    (no hay cache o faltan credenciales) — el caller degrada graceful."""
    if _client_creds() is None:
        return None
    now = time.time()
    if _token_cache["value"] and now < _token_cache["exp"]:
        return _token_cache["value"]
    cache = load_cache()
    if not cache or not cache.get("refresh_token"):
        return None
    try:
        tok = _token_request({"grant_type": "refresh_token",
                              "refresh_token": cache["refresh_token"]})
    except Exception as e:
        log.warning("youtube: no pude refrescar el token (%s)", e)
        return None
    if not tok.get("access_token"):
        return None
    _token_cache["value"] = tok["access_token"]
    _token_cache["exp"] = now + int(tok.get("expires_in", 3600)) - 60
    return _token_cache["value"]


# ─── Bootstrap interactivo (CLI, una sola vez) ───────────────────────────────
def _settings() -> dict:
    try:
        return json.loads((BASE_DIR / "config" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def redirect_uri() -> str:
    return str(_settings().get("GOOGLE_REDIRECT_URI") or _DEFAULT_REDIRECT)


def authorize_interactive() -> None:
    """Authorization Code completo: URL en el browser → callback local → canje
    del code → cache con refresh token. Correr EN la máquina del admin."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import webbrowser

    if _client_creds() is None:
        raise SystemExit("faltan GOOGLE_OAUTH_ID / GOOGLE_OAUTH_SECRET en el .env "
                         "de la instancia")
    redirect = redirect_uri()
    port = urllib.parse.urlparse(redirect).port or 8890
    state = secrets.token_urlsafe(16)
    cid, _ = _client_creds()
    url = _AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect,
        "scope": SCOPES, "state": state,
        # offline + consent: sin los dos, Google devuelve access token pero NO
        # refresh token en la segunda autorización en adelante, y el bot queda
        # funcionando una hora y después mudo.
        "access_type": "offline", "prompt": "consent",
    })

    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (nombre de la stdlib)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = qs.get("state", [None])[0] == state and "code" in qs
            if ok:
                result["code"] = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("listo, volvé a la terminal" if ok else
                              "callback inválido (state/code)").encode())

        def log_message(self, *a):  # silenciar el server
            pass

    print(f"Abriendo el browser para autorizar (scope: {SCOPES})...")
    print(f"Redirect URI: {redirect} — tiene que estar declarado igual en el "
          "OAuth client de Google Cloud.")
    print(f"Si no se abre solo, entrá a:\n{url}\n")
    webbrowser.open(url)
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    while "code" not in result:
        httpd.handle_request()
    httpd.server_close()

    tok = _token_request({"grant_type": "authorization_code",
                          "code": result["code"], "redirect_uri": redirect})
    if not tok.get("refresh_token"):
        raise SystemExit(f"Google no devolvió refresh_token: {tok}")
    save_cache({"refresh_token": tok["refresh_token"], "scope": tok.get("scope", SCOPES),
                "obtained_at": int(time.time())})
    print(f"OK — refresh token guardado en {CACHE_PATH}")
    print("Acordate de agregar 'context/.youtube_cache' al manifest de "
          "butterbot-secrets para que llegue a prod.")


def test_playlists() -> None:
    """Smoke test: refresca el token y lista las playlists de la cuenta."""
    token = user_token()
    if not token:
        raise SystemExit("sin token de usuario — corré primero: "
                         "python src/youtube_auth.py --instance <dir>")
    q = urllib.parse.urlencode({"part": "snippet,contentDetails", "mine": "true",
                                "maxResults": 25})
    req = urllib.request.Request(
        f"https://www.googleapis.com/youtube/v3/playlists?{q}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = data.get("items") or []
    print(f"OK — token de usuario funcionando. {len(items)} playlist(s) de la cuenta:")
    for it in items:
        print(f"  {it['id']}  «{it['snippet']['title']}» "
              f"({it['contentDetails']['itemCount']} videos)")


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv  # noqa: I001 (igual que spotify_auth: solo CLI)

    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description="OAuth de usuario de YouTube para Botata")
    parser.add_argument("--test", action="store_true",
                        help="verificar token + listar las playlists de la cuenta")
    parser.add_argument("--instance", help="Directorio de la instancia")
    args = parser.parse_args()
    test_playlists() if args.test else authorize_interactive()
