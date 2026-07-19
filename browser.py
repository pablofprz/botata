"""browser.py — capa de navegador reusable (Patchright, perfil persistente).

Cimiento del scraping agéntico de botata. NO sabe de plataformas: abre páginas
y devuelve su contenido renderizado. La navegación y extracción específica de cada
sitio vive en los adaptadores de `sources.py`.

Usa patchright (Chromium parcheado a nivel binario) en vez de Playwright vanilla:
  - navigator.webdriver eliminado
  - Runtime.enable leak parcheado
  - __pwInitScripts removido
  - TLS fingerprint consistente

Modelo de sesión: te logueás UNA vez a mano (headful, ver `login()`), la sesión
queda persistida en el perfil de usuario del navegador, y la automatización la
reúsa para siempre (headless). Así se esquiva la detección de login automatizado.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchright.sync_api import BrowserContext, sync_playwright

log = logging.getLogger("botata.browser")

# UA realista de Chrome en Windows — evita el default de Playwright, que es señal de bot.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Args cosmeticos solamente. Patchright ya parchea a nivel binario los tells reales
# (navigator.webdriver, --enable-automation, Runtime.enable, __pwInitScripts); agregar
# flags anti-deteccion por arriba (ej. --disable-features=...) genera un fingerprint
# distinguible de un Chrome real. Los maintainers de patchright dicen literalmente:
# "Don't modify default launch args".
_HARDENED_ARGS = ["--no-first-run", "--no-default-browser-check"]


def _probe_ua(pw: Any, channel: str | None, executable_path: str | None) -> str | None:
    """Sondea el UA real del navegador y le quita el delator 'HeadlessChrome'.

    El UA headless de Chrome dice 'HeadlessChrome/<ver>' — un tell fuerte que además
    viaja en el header HTTP (no se puede tapar solo por JS). Lo reemplazamos por
    'Chrome/<ver>' conservando la versión real, así queda consistente con las client
    hints. Lanza un navegador descartable (no-persistente) solo para leer el UA.
    """
    kw: dict[str, Any] = dict(
        headless=True, args=_HARDENED_ARGS
    )
    try:
        if executable_path:
            b = pw.chromium.launch(executable_path=executable_path, **kw)
        elif channel:
            try:
                b = pw.chromium.launch(channel=channel, **kw)
            except Exception:
                b = pw.chromium.launch(**kw)
        else:
            b = pw.chromium.launch(**kw)
    except Exception as e:
        log.warning("no se pudo sondear el UA (%s); se usa el UA por defecto", e)
        return None
    try:
        ua = b.new_page().evaluate("() => navigator.userAgent")
        return ua.replace("HeadlessChrome", "Chrome") if ua else None
    finally:
        b.close()


def _launch_persistent(
    pw: Any,
    *,
    user_data_dir: str,
    headless: bool,
    proxy: str | None,
    locale: str,
    user_agent: str | None,
    timeout_ms: int,
    channel: str | None,
    executable_path: str | None,
) -> BrowserContext:
    """Lanza un persistent context endurecido. Prefiere Chrome/Brave real si se indica.

    Orden de preferencia del binario:
      1. executable_path explícito (ej. ruta a brave.exe)
      2. channel (ej. 'chrome' / 'msedge') — usa el navegador instalado del sistema
      3. Chromium bundled de Playwright (fallback)
    """
    kwargs: dict[str, Any] = dict(
        user_data_dir=user_data_dir,
        headless=headless,
        proxy={"server": proxy} if proxy else None,
        locale=locale,
        viewport=None if not headless else {"width": 1280, "height": 900},
        timezone_id="America/Argentina/Buenos_Aires",
        args=_HARDENED_ARGS,
    )
    # UA: en headless, Chrome real reporta 'HeadlessChrome' (tell fuerte) → lo sondeamos
    # y de-headlessizamos, conservando la versión real (consistente con Sec-CH-UA). En
    # headful (login) dejamos el UA nativo, que ya es normal. UA explícito siempre gana.
    if not user_agent and headless:
        user_agent = _probe_ua(pw, channel, executable_path)
    if user_agent:
        kwargs["user_agent"] = user_agent

    if executable_path:
        ctx = pw.chromium.launch_persistent_context(executable_path=executable_path, **kwargs)
    elif channel:
        try:
            ctx = pw.chromium.launch_persistent_context(channel=channel, **kwargs)
            log.info("navegador: usando channel=%s (binario del sistema)", channel)
        except Exception as e:
            log.warning("channel=%s no disponible (%s) — fallback a Chromium bundled", channel, e)
            ctx = pw.chromium.launch_persistent_context(**kwargs)
    else:
        ctx = pw.chromium.launch_persistent_context(**kwargs)

    ctx.set_default_timeout(timeout_ms)
    return ctx


@dataclass
class PageSnapshot:
    """Contenido renderizado de una página. Lo que consume el extractor LLM."""
    url: str
    text: str
    html: str
    screenshot: bytes | None = None


class BrowserSession:
    """Navegador con perfil persistente y anti-detección. Context manager.

    Reusable entre plataformas: `fetch(url)` devuelve el contenido de la página;
    `context` expone el BrowserContext crudo para adaptadores que necesiten DOM
    directo (ej. juntar permalinks de una grilla).
    """

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        headless: bool = True,
        proxy: str | None = None,
        locale: str = "es-AR",
        timeout_ms: int = 45_000,
        user_agent: str | None = None,
        channel: str | None = None,   # None = Chromium parcheado de patchright (mejor stealth)
        executable_path: str | None = None,
        cdp_endpoint: str | None = None,
    ) -> None:
        self._profile_dir = Path(profile_dir)
        self._headless = headless
        self._proxy = proxy
        self._locale = locale
        self._timeout_ms = timeout_ms
        self._user_agent = user_agent
        self._channel = channel
        self._executable_path = executable_path
        self._cdp_endpoint = cdp_endpoint
        self._pw: Any = None
        self._ctx: BrowserContext | None = None

    # ── ciclo de vida ────────────────────────────────────────────────────
    def open(self) -> None:
        self._pw = sync_playwright().start()
        if self._cdp_endpoint:
            # Conectarse a un Chrome que el usuario lanzó a mano con
            # --remote-debugging-port=9222 (su perfil real, ya logueado en IG). El login
            # queda en contexto limpio (sin automatización); el bot solo maneja el
            # navegador ya logueado. Instagram detecta CDP en navegadores lanzados por
            # Playwright e invalida la sesión / bloquea el form login
            # (FakeIncorrectPassword); conectar a un Chrome ya abierto por el usuario
            # es la forma robusta de esquivarlo.
            browser = self._pw.chromium.connect_over_cdp(self._cdp_endpoint)
            self._ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            self._ctx.set_default_timeout(self._timeout_ms)
            log.info("browser conectado via CDP (%s)", self._cdp_endpoint)
            return
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._ctx = _launch_persistent(
            self._pw,
            user_data_dir=str(self._profile_dir),
            headless=self._headless,
            proxy=self._proxy,
            locale=self._locale,
            user_agent=self._user_agent,
            timeout_ms=self._timeout_ms,
            channel=self._channel,
            executable_path=self._executable_path,
        )
        log.info(
            "browser abierto (perfil=%s headless=%s proxy=%s)",
            self._profile_dir.name, self._headless, bool(self._proxy),
        )

    def close(self) -> None:
        # En modo CDP no cerramos el contexto: es el Chrome del usuario, sigue abierto.
        if self._ctx is not None and not self._cdp_endpoint:
            self._ctx.close()
        self._ctx = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    def __enter__(self) -> BrowserSession:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── acceso ───────────────────────────────────────────────────────────
    @property
    def context(self) -> BrowserContext:
        if self._ctx is None:
            raise RuntimeError("BrowserSession no está abierta (usá 'with' o .open())")
        return self._ctx

    def fetch(
        self,
        url: str,
        *,
        wait_until: str = "networkidle",
        wait_selector: str | None = None,
        scroll: int = 0,
        screenshot: bool = False,
    ) -> PageSnapshot:
        """Abre `url` en una página nueva y devuelve su contenido renderizado.

        wait_selector: espera a que aparezca ese selector antes de leer (útil en SPAs).
        scroll: cantidad de scrolls para disparar lazy-load.
        """
        page = self.context.new_page()
        try:
            page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
            if wait_selector:
                page.wait_for_selector(wait_selector)
            for _ in range(scroll):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(1200)
            return PageSnapshot(
                url=page.url,
                text=page.inner_text("body"),
                html=page.content(),
                screenshot=page.screenshot(full_page=False) if screenshot else None,
            )
        finally:
            page.close()


def login(
    profile_dir: str | Path,
    start_url: str,
    *,
    proxy: str | None = None,
    channel: str | None = None,   # None = Chromium parcheado de patchright (mejor stealth)
    executable_path: str | None = None,
) -> None:
    """Abre un navegador HEADFUL endurecido para login manual y persiste la sesión.

    Bloquea hasta que cerrás la ventana. Todo lo que hagas (login, 2FA, challenges)
    queda guardado en `profile_dir`; después la automatización headless lo reúsa.
    """
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info("Abriendo navegador para login manual en %s — logueate y cerrá la ventana.", start_url)
    pw = sync_playwright().start()
    try:
        ctx = _launch_persistent(
            pw,
            user_data_dir=str(profile_dir),
            headless=False,
            proxy=proxy,
            locale="es-AR",
            user_agent=None,
            timeout_ms=45_000,
            channel=channel,
            executable_path=executable_path,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(start_url)
        # Bloquea hasta que el usuario cierra la ventana/context.
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    finally:
        pw.stop()
    log.info("Sesión persistida en %s", profile_dir)
