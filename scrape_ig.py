"""scrape_ig.py — runner del scraping de Instagram.

Dos caminos (dos adapters, misma DB y mismo destino de imágenes):

  Camino API (instagrapi, recomendado — IG bloquea los navegadores controlados):
    python scrape_ig.py api-login   # login una vez -> posted/ig_session.json
                                    # (challenge mail/SMS/2FA se pide por input)
    python scrape_ig.py api-run     # scrapea con la sesión guardada, sin password

  Camino navegador (fallback — patchright + Chrome real, suele ser bloqueado por IG):
    python scrape_ig.py launch-chrome   # abre tu Chrome con --remote-debugging-port=9222
    python scrape_ig.py run             # se conecta a ese Chrome vía CDP y scrapea
    python scrape_ig.py login           # login manual headful (perfil posted/browser/ig)
    python scrape_ig.py import-cookies  # importa cookies de tu navegador real

Las imágenes se guardan en scrape/pictures/instagram/<post_id>_<n>.jpg.
No se pisan: si el archivo ya existe, se saltea.
La DB (posted/botata.db) trackea qué posts ya se procesaron.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import db as dbmod
from browser import BrowserSession, login
from extract import LLMExtractor
from ig_api import IGSourceAPI
from sources import IGSource

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("botata.scrape_ig")

IG_CONFIG      = BASE_DIR / "config" / "instagram.json"
IG_PROFILE     = BASE_DIR / "posted" / "browser" / "ig"   # perfil persistente (sesión logueada)
IG_PICTURES    = BASE_DIR / "scrape" / "pictures" / "instagram"  # destino de imágenes
IG_HOME        = "https://www.instagram.com/"
IG_PROXY       = os.environ.get("IG_PROXY") or None
IG_API_SESSION = BASE_DIR / "posted" / "ig_session.json"  # settings de instagrapi (sessionid)

# Anti-detección: Chrome real del sistema (channel="chrome") tiene fingerprint mucho más
# limpio que el Chromium bundled de patchright. Instagram bloquea logins desde Chromium
# automatizado (FakeIncorrectPassword). Override con IG_BROWSER_CHANNEL="" para usar Chromium.
IG_BROWSER_CHANNEL = os.environ.get("IG_BROWSER_CHANNEL") or "chrome"
IG_BROWSER_PATH    = os.environ.get("IG_BROWSER_PATH") or None
# CDP: conectarse a un Chrome que el usuario lanza a mano con
# --remote-debugging-port=9222 (perfil real, ya logueado en IG). Es la única forma
# robusta de scrapear IG: el form login desde un navegador lanzado por Playwright
# está bloqueado (FakeIncorrectPassword) y la sesión importada se invalida. Setear
# IG_CDP_ENDPOINT="" desactiva CDP y usa launch_persistent (modo fallback).
IG_CDP_ENDPOINT = os.environ.get("IG_CDP_ENDPOINT", "http://localhost:9222")


def load_accounts() -> list[dict]:
    if not IG_CONFIG.exists():
        log.error("No existe %s", IG_CONFIG)
        return []
    data = json.loads(IG_CONFIG.read_text(encoding="utf-8"))
    return data.get("accounts", [])


def cmd_login() -> None:
    """Login manual headful — persiste la sesión en el perfil."""
    login(
        IG_PROFILE, IG_HOME, proxy=IG_PROXY,
        channel=IG_BROWSER_CHANNEL, executable_path=IG_BROWSER_PATH,
    )
    log.info("Listo. Ahora podés correr: python scrape_ig.py run")


def cmd_launch_chrome() -> None:
    """Lanza TU Chrome real con --remote-debugging-port para que el bot se conecte vía CDP.

    Usa tu perfil default (ya logueado en IG). Cerrá todas las ventanas de Chrome antes
    de correrlo, si no el puerto de debug no se expone sobre el perfil existente. Después
    de lanzado, corré `python scrape_ig.py run` en otra terminal.
    """
    import subprocess
    candidates = [
        IG_BROWSER_PATH or "",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/brave-browser",
    ]
    chrome = next((c for c in candidates if c and Path(c).exists()), None)
    if not chrome:
        log.error("No se encontró Chrome. Seteá IG_BROWSER_PATH al binario.")
        return
    port = (IG_CDP_ENDPOINT or "").rsplit(":", 1)[-1] or "9222"
    log.info("Lanzando Chrome: %s --remote-debugging-port=%s", chrome, port)
    subprocess.Popen([chrome, f"--remote-debugging-port={port}"])
    log.info("Chrome lanzado. Abrí IG y confirmá que estás logueado, después corré:")
    log.info("  python scrape_ig.py run")


def cmd_import_cookies(cookies_path: str | None = None) -> None:
    """Importa cookies de Instagram desde un JSON exportado del navegador real.

    Usa la misma BrowserSession que el scrapeo (con stealth, locale, viewport, etc.)
    para que la sesión sea consistente. Inyecta las cookies, las persiste en el perfil,
    y verifica que pueda acceder a un perfil real (instagram) sin ser redirigido a login.
    """
    src = Path(cookies_path or (BASE_DIR / "posted" / "ig_cookies.json"))
    if not src.exists():
        log.error("No existe %s. Exportá cookies de tu navegador real primero.", src)
        return

    raw = src.read_text(encoding="utf-8").strip()

    # Detectar formato
    cookies: list[dict[str, Any]] = []
    if raw.startswith("["):
        cookies = json.loads(raw)
    elif raw.startswith("# Netscape") or raw.startswith("# HTTP Cookie"):
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain = parts[0]
                # Netscape: HttpOnly cookies tienen prefijo "#HttpOnly_"
                http_only = False
                if domain.startswith("#HttpOnly_"):
                    http_only = True
                    domain = domain[len("#HttpOnly_"):]
                cookies.append({
                    "domain": domain,
                    "httpOnly": http_only,
                    "path": parts[2],
                    "secure": parts[3] == "TRUE",
                    "name": parts[5],
                    "value": parts[6],
                })
    else:
        log.error("Formato no reconocido. Usá JSON array o Netscape cookie file.")
        return

    # Normalizar sameSite
    def _norm_samesite(raw: str | None) -> str:
        if raw is None:
            return "Lax"
        r = raw.strip().lower()
        if r in ("unspecified", "lax", "medium"):
            return "Lax"
        if r in ("no_restriction", "none"):
            return "None"
        if r == "strict":
            return "Strict"
        return "Lax"

    # Filtrar solo cookies de Instagram
    ig_cookies = [
        {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".instagram.com"),
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", True) if isinstance(c.get("secure"), bool) else True,
            "sameSite": _norm_samesite(c.get("sameSite")),
        }
        for c in cookies
        if "instagram.com" in (c.get("domain") or "")
    ]
    if not ig_cookies:
        log.error("No se encontraron cookies de instagram.com en el archivo.")
        return

    # Usar BrowserSession (con stealth, channel, locale, viewport — todo consistente)
    # para inyectar las cookies en el perfil persistente.
    log.info("Importando %d cookies de Instagram al perfil %s", len(ig_cookies), IG_PROFILE)
    browser = BrowserSession(
        IG_PROFILE, headless=True, proxy=IG_PROXY,
        channel=IG_BROWSER_CHANNEL, executable_path=IG_BROWSER_PATH,
        cdp_endpoint=IG_CDP_ENDPOINT or None,
    )
    browser.open()
    try:
        browser.context.add_cookies(ig_cookies)  # type: ignore[arg-type]

        # Verificar: /accounts/edit/ es login-gated → redirige a /accounts/login si no hay
        # sesión. (Antes verificábamos contra /instagram/, un perfil público accesible
        # logueado o no → daba falso positivo.)
        page = browser.context.new_page()
        try:
            page.goto("https://www.instagram.com/accounts/edit/", wait_until="domcontentloaded")
            logged_in = "/accounts/login" not in page.url
            if logged_in:
                log.info("✅ Sesión importada y verificada. Ya podés correr: python scrape_ig.py run")
            else:
                log.error("❌ Las cookies no dieron sesión válida. ¿Exportaste sessionid y ds_user_id?")
                log.error("   URL final: %s", page.url)
        finally:
            page.close()
    finally:
        browser.close()


def _make_source() -> tuple[IGSource, BrowserSession]:
    # Reusa el cliente LLM de botata (LITE_MODEL alcanza para extraer).
    from botata import LITE_MODEL, OPENAI_ENDPOINT, OPENROUTER_API_KEY, LLMClient

    llm = LLMClient(api_key=OPENROUTER_API_KEY, base_url=OPENAI_ENDPOINT, model=LITE_MODEL)
    browser = BrowserSession(
        IG_PROFILE, headless=True, proxy=IG_PROXY,
        channel=IG_BROWSER_CHANNEL, executable_path=IG_BROWSER_PATH,
        cdp_endpoint=IG_CDP_ENDPOINT or None,
    )
    browser.open()
    return IGSource(browser, LLMExtractor(llm)), browser


def _scrape(conn, src: "IGSource | IGSourceAPI") -> None:
    """Loop de scrapeo compartido entre browser y api.

    `src` expone is_logged_in / fetch_recent / download_media (ambos adapters cumplen).
    """
    accounts = load_accounts()
    if not accounts:
        log.warning("Sin cuentas configuradas en %s", IG_CONFIG)
        return
    if not src.is_logged_in():
        log.error("No hay sesión de IG válida. Logueá primero (api-login o login).")
        return

    for acc in accounts:
        username = acc["username"]
        name     = acc.get("name", username)
        limit    = acc.get("max_posts", 12)
        log.info("── Scrapeando @%s (max %d) ──", username, limit)
        try:
            items = src.fetch_recent(username, limit)
        except Exception as e:
            log.error("Falló @%s: %s", username, e)
            continue

        nuevos = 0
        for it in items:
            if dbmod.has_scraped_item(conn, it.platform, it.external_id):
                continue

            # 1) Guardar metadata en DB
            dbmod.save_scraped_item(
                conn,
                platform=it.platform,
                external_id=it.external_id,
                source_name=name,
                author=it.author,
                text=it.text,
                media_urls=json.dumps(it.media_urls),
                url=it.url,
                posted_at=it.posted_at,
            )

            # 2) Descargar imágenes a scrape/pictures/instagram/
            if it.media_urls:
                saved = src.download_media(it, str(IG_PICTURES))
                tag = f"{len(saved)} img" if saved else "sin img"
            else:
                tag = "sin img"
            log.info("  + %s | %s (%s)", it.external_id,
                     (it.text or "(sin caption)")[:60].replace("\n", " "), tag)
            nuevos += 1

        log.info("@%s: %d ítems, %d nuevos", username, len(items), nuevos)

        # Delay humano entre cuentas
        if len(accounts) > 1:
            delay = random.uniform(15, 35)
            log.info("  esperando %.0fs antes de la siguiente cuenta...", delay)
            time.sleep(delay)


def cmd_run() -> None:
    """Scrapeo vía navegador (camino browser). IG suele bloquearlo — preferir api-run."""
    conn = dbmod.init_db()
    src, browser = _make_source()
    try:
        _scrape(conn, src)
    finally:
        browser.close()
        conn.close()


def cmd_api_login() -> None:
    """Login vía instagrapi (API mobile). Guarda sesión en posted/ig_session.json.

    Usuario/password de IG_USERNAME/IG_PASSWORD (env), o prompt interactivo. Si IG
    pide challenge (mail/SMS/2FA), instagrapi lo pide por input() automáticamente.
    """
    username = os.environ.get("IG_USERNAME") or input("Usuario de IG: ").strip()
    password = os.environ.get("IG_PASSWORD") or os.environ.get("IG_PASS") or ""
    if not password:
        import getpass
        password = getpass.getpass("Password de IG (no se muestra): ")
    src = IGSourceAPI(IG_API_SESSION, proxy=IG_PROXY)
    src.login(username, password)
    log.info("Listo. Ahora podés correr: python scrape_ig.py api-run")


def cmd_api_run() -> None:
    """Scrapeo vía instagrapi (camino api, recomendado). Reusa la sesión guardada."""
    conn = dbmod.init_db()
    src = IGSourceAPI(IG_API_SESSION, proxy=IG_PROXY)
    try:
        _scrape(conn, src)
    finally:
        conn.close()


def _extract_sessionid(cookies_path: str | None) -> str | None:
    """Saca el valor de la cookie `sessionid` de un export de cookies del navegador.

    Acepta JSON array (EditThisCookie) o Netscape cookie file.
    """
    src = Path(cookies_path or (BASE_DIR / "posted" / "ig_cookies.json"))
    if not src.exists():
        return None
    raw = src.read_text(encoding="utf-8").strip()
    if raw.startswith("["):
        for c in json.loads(raw):
            if c.get("name") == "sessionid":
                return c.get("value")
    elif raw.startswith("# Netscape") or raw.startswith("# HTTP Cookie"):
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] == "sessionid":
                return parts[6]
    return None


def cmd_api_session(cookies_path: str | None = None, sessionid: str | None = None) -> None:
    """Login vía sessionid del navegador (saltea el login por password bloqueado).

    Vos te logueás en tu Chrome personal en instagram.com, exportás las cookies a
    posted/ig_cookies.json (o pasás --sessionid), y este comando alimenta el
    sessionid a instagrapi con login_by_sessionid — sin mandar password.
    """
    sid = sessionid or _extract_sessionid(cookies_path)
    if not sid:
        log.error("No se encontró sessionid.")
        log.error("  Opción 1: exportá cookies frescas de tu Chrome (logueado en IG) a posted/ig_cookies.json")
        log.error("  Opción 2: pasá --sessionid <valor_de_la_cookie_sessionid>")
        return
    src = IGSourceAPI(IG_API_SESSION, proxy=IG_PROXY)
    src.login_by_session(sid)
    log.info("Listo. Ahora podés correr: python scrape_ig.py api-run")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scraper de Instagram (botata)")
    parser.add_argument(
        "cmd",
        choices=["api-login", "api-session", "api-run",
                 "launch-chrome", "login", "run", "import-cookies"],
        help="api-session (login con sessionid del navegador, recomendado) | "
             "api-login (login con password, IG suele bloquearlo) | api-run (scrapea) | "
             "launch-chrome/login/run/import-cookies (camino navegador, fallback)",
    )
    parser.add_argument("--cookies", type=str, default=None,
                        help="Ruta al archivo de cookies JSON (default: posted/ig_cookies.json)")
    parser.add_argument("--sessionid", type=str, default=None,
                        help="Sessionid de Instagram (cookie de sesión web) para api-session")
    args = parser.parse_args()
    if args.cmd == "api-session":
        cmd_api_session(args.cookies, args.sessionid)
    elif args.cmd == "api-login":
        cmd_api_login()
    elif args.cmd == "api-run":
        cmd_api_run()
    elif args.cmd == "launch-chrome":
        cmd_launch_chrome()
    elif args.cmd == "login":
        cmd_login()
    elif args.cmd == "import-cookies":
        cmd_import_cookies(args.cookies)
    else:
        cmd_run()


if __name__ == "__main__":
    sys.exit(main())
