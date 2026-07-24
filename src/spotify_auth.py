"""spotify_auth.py — token de USUARIO de Spotify (OAuth Authorization Code, stdlib).

El Client Credentials de T13 (search) es app-only: no puede leer ni escribir
playlists. Para eso hace falta un token de usuario, que sale de una autorización
ÚNICA del admin en el browser (este módulo como CLI) y después se refresca
headless para siempre con el refresh token guardado en `context/.spotify_cache`
(gitignored; sincronizarlo a prod vía butterbot-secrets).

Uso:
    python src/spotify_auth.py          # autorización inicial (abre el browser)
    python src/spotify_auth.py --test   # verifica token + lectura de la playlist

Consumidor runtime: `user_token()` — devuelve un access token fresco o None si
no hay cache (el bot degrada graceful: "falta autorizar").
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("botata.spotify_auth")

from instance import instance_dir

BASE_DIR   = instance_dir()  # T28c: directorio de instancia (default = raíz del repo)
CACHE_PATH = BASE_DIR / "context" / ".spotify_cache"

_AUTH_URL  = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"

# modify para agregar temas; read para dedup/lectura de la lista (privada o no)
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"

_token_cache: dict = {"value": None, "exp": 0.0}


def _client_creds() -> tuple[str, str] | None:
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


def _basic_auth() -> str:
    cid, secret = _client_creds()  # el caller ya validó
    return base64.b64encode(f"{cid}:{secret}".encode()).decode()


def _token_request(data: dict) -> dict:
    req = urllib.request.Request(
        _TOKEN_URL, data=urllib.parse.urlencode(data).encode(),
        headers={"Authorization": f"Basic {_basic_auth()}",
                 "Content-Type": "application/x-www-form-urlencoded"})
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
    tok = _token_request({"grant_type": "refresh_token",
                          "refresh_token": cache["refresh_token"]})
    # Spotify puede rotar el refresh token; persistir si vino uno nuevo
    if tok.get("refresh_token") and tok["refresh_token"] != cache["refresh_token"]:
        save_cache({**cache, "refresh_token": tok["refresh_token"]})
    _token_cache["value"] = tok["access_token"]
    _token_cache["exp"]   = now + tok.get("expires_in", 3600) - 60
    return tok["access_token"]


# ─── Bootstrap interactivo (CLI, una sola vez) ───────────────────────────────

def _settings() -> dict:
    try:
        return json.loads((BASE_DIR / "config" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def authorize_interactive() -> None:
    """Flujo Authorization Code completo: URL en el browser → callback local →
    canje del code → cache con refresh token. Correr EN la máquina del admin."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import webbrowser

    if _client_creds() is None:
        raise SystemExit("faltan SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET en .env")
    redirect = _settings().get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    port = urllib.parse.urlparse(redirect).port or 8888
    state = secrets.token_urlsafe(16)
    cid, _ = _client_creds()
    url = _AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect,
        "scope": SCOPES, "state": state,
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

    print(f"Abriendo el browser para autorizar (scopes: {SCOPES})...")
    print(f"Si no se abre solo, entrá a:\n{url}\n")
    webbrowser.open(url)
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    while "code" not in result:
        httpd.handle_request()
    httpd.server_close()

    tok = _token_request({"grant_type": "authorization_code",
                          "code": result["code"], "redirect_uri": redirect})
    if not tok.get("refresh_token"):
        raise SystemExit(f"Spotify no devolvió refresh_token: {tok}")
    save_cache({"refresh_token": tok["refresh_token"], "scope": tok.get("scope", SCOPES),
                "obtained_at": int(time.time())})
    print(f"OK — refresh token guardado en {CACHE_PATH}")
    print("Acordate de agregar 'context/.spotify_cache' al manifest de butterbot-secrets "
          "y pushearlo para que llegue a prod.")


def test_playlist() -> None:
    """Smoke test: refresca el token y lee la playlist configurada."""
    token = user_token()
    if not token:
        raise SystemExit("sin token de usuario — corré primero: python src/spotify_auth.py")
    playlist = _settings().get("SPOTIFY_PLAYLIST_ID", "")
    if not playlist:
        raise SystemExit("falta SPOTIFY_PLAYLIST_ID en config/settings.json")

    def _get(url: str) -> dict:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    base = f"https://api.spotify.com/v1/playlists/{playlist}"
    meta  = _get(base + "?fields=name")
    # /items, no /tracks: la migración feb-2026 removió /tracks en Dev Mode
    total = _get(base + "/items?fields=total&limit=1").get("total")
    print(f"OK — playlist «{meta.get('name')}» con {total} temas. "
          "Token de usuario funcionando (lectura de items OK).")


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description="OAuth de usuario de Spotify para Botata")
    parser.add_argument("--test", action="store_true",
                        help="verificar token + lectura de la playlist configurada")
    parser.add_argument("--instance", help="Directorio de la instancia (default: raíz del repo)")
    args = parser.parse_args()
    test_playlist() if args.test else authorize_interactive()
