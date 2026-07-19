"""ig_api.py — adaptador de Instagram vía API privada mobile (instagrapi).

Camino alternativo al scraper por navegador (sources.IGSource). instagrapi habla la
misma API que la app mobile de Instagram: sin navegador, sin fingerprint de browser,
sesión persistida en un archivo de settings. Es la forma robusta de scrapear IG —
Meta detecta y bloquea los navegadores controlados por Playwright/patchright (body
vacío, FakeIncorrectPassword, captcha que no renderiza), incluso con Chrome real y
sin stealth. La API mobile no pasa por esa detección.

Misma interfaz que sources.IGSource (fetch_recent / download_media / is_logged_in)
para que scrape_ig.py los use de forma intercambiable.

Flujo:
  python scrape_ig.py api-login   # login una vez -> guarda posted/ig_session.json
  python scrape_ig.py api-run     # scrapea con la sesión guardada (sin password)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests
from instagrapi import Client
from instagrapi.exceptions import BadPassword, ChallengeRequired

from sources import SourceItem

log = logging.getLogger("botata.ig_api")

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class IGSourceAPI:
    """Source de Instagram vía instagrapi. Interfaz compatible con sources.IGSource."""

    platform = "instagram"

    def __init__(
        self,
        session_file: str | Path,
        *,
        proxy: str | None = None,
    ) -> None:
        self._session_file = Path(session_file)
        self._cl = Client()
        if proxy:
            # instagrapi set_proxy toma un DSN string: "http://host:port",
            # "http://user:pass@host:port" o "socks5://host:port". Útil cuando tu IP
            # está en blacklist de IG: salir por un proxy/VPN para el login.
            self._cl.set_proxy(proxy)
        # Consistencia device/region: los defaults de instagrapi son US (locale en_US,
        # country US, tz NY). Para cuentas argentinas, matchear AR evita el mismatch de
        # fingerprint que dispara bad_password/challenge. Override por env si usás proxy
        # de otro país (ej. IG_COUNTRY=US con proxy US).
        self._cl.set_locale(os.environ.get("IG_LOCALE", "es_AR"))
        self._cl.set_country(os.environ.get("IG_COUNTRY", "AR"))
        self._cl.set_country_code(int(os.environ.get("IG_COUNTRY_CODE", "54")))
        self._cl.set_timezone_offset(int(os.environ.get("IG_TIMEZONE_OFFSET", "-10800")))
        # BadPassword con challenge ("We can send you an email") NO se auto-resuelve en
        # instagrapi (solo ChallengeRequired dispara challenge_resolve). Lo atrapamos acá
        # para forzar el flow de email/SMS cuando IG lo ofrece.
        self._cl.handle_exception = self._handle_exception
        self._loaded = False

    def _handle_exception(self, client: Client, e: Exception) -> None:
        """Handler custom: atrapa ChallengeRequired (y BadPassword-con-challenge real)
        y fuerza challenge_resolve (flow email/SMS por input). Después private_request
        reintenta el login."""
        if isinstance(e, ChallengeRequired):
            client.challenge_resolve(client.last_json)
            return
        if isinstance(e, BadPassword):
            last = getattr(client, "last_json", None) or {}
            # Loguear la respuesta cruda de IG — clave para diagnosticar si hay challenge
            # accionable o es un bloqueo puro de IP/cuenta.
            log.error("BadPassword — respuesta de IG (last_json): %s", last)
            # Solo challenge_resolve si IG devolvió un challenge real (key 'challenge').
            # Si solo ofrece send_one_click_login_email (recovery por mail), NO hay
            # challenge accionable por API — es un bloqueo de IP/cuenta, falla honesta.
            if isinstance(last, dict) and "challenge" in last:
                log.info("IG devolvió challenge real — resolviendo (te pedirá código)...")
                try:
                    client.challenge_resolve(last)
                except Exception as ce:
                    log.error("challenge_resolve falló: %s", ce)

    # ── sesión ─────────────────────────────────────────────────────────────
    def login(self, username: str, password: str) -> None:
        """Login interactivo. Persiste settings en session_file.

        Si Instagram pide challenge (mail/SMS), instagrapi lo pide por input()
        automáticamente (manual_input_code). Para 2FA, login acepta verification_code
        pero acá delegamos al challenge flow nativo.
        """
        try:
            self._cl.login(username, password)
        except Exception as e:
            log.error("Login falló: %s", e)
            raise
        self._cl.dump_settings(str(self._session_file))
        log.info("Login OK. Sesión guardada en %s", self._session_file)
        self._loaded = True

    def _read_sessionid(self) -> str:
        """Lee el sessionid del archivo de settings.

        instagrapi lo guarda en `authorization_data.sessionid` y en la lista
        `cookies` (NO en una key top-level `sessionid`).
        """
        if not self._session_file.exists():
            return ""
        try:
            settings = self._cl.load_settings(str(self._session_file))
        except Exception as e:
            log.warning("No se pudo leer %s (%s)", self._session_file, e)
            return ""
        if not isinstance(settings, dict):
            return ""
        auth = settings.get("authorization_data") or {}
        if isinstance(auth, dict) and auth.get("sessionid"):
            return auth["sessionid"]
        for c in settings.get("cookies") or []:
            if isinstance(c, dict) and c.get("name") == "sessionid":
                return c.get("value") or ""
        return settings.get("sessionid") or ""  # top-level (formato viejo)

    def _load(self) -> bool:
        """Carga la sesión guardada y la revalida con login_by_sessionid (sin password)."""
        if self._loaded:
            return True
        sid = self._read_sessionid()
        if not sid:
            return False
        try:
            self._cl.login_by_sessionid(sid)
            self._loaded = True
            log.info("Sesión reusada vía sessionid")
            return True
        except Exception as e:
            log.warning("sessionid inválido (%s) — hay que re-loguear (api-session)", e)
            return False

    def is_logged_in(self) -> bool:
        """True si la sesión guardada revalida sin password."""
        return self._load()

    def login_by_session(self, sessionid: str) -> None:
        """Login con un sessionid capturado de una sesión de navegador real.

        Sidestep del login por password (que IG bloquea con FakeIncorrectPassword en la
        API mobile, sin importar la IP — el login web sí funciona). Vos te logueás en tu
        Chrome personal, extraés el sessionid, y acá lo usamos para autenticar los
        requests de la API mobile sin mandar password.
        """
        try:
            self._cl.login_by_sessionid(sessionid)
        except Exception as e:
            log.error("login_by_sessionid falló: %s", e)
            raise
        self._cl.dump_settings(str(self._session_file))
        log.info("Login por sessionid OK. Sesión guardada en %s", self._session_file)
        self._loaded = True

    # ── scraping ───────────────────────────────────────────────────────────
    def fetch_recent(self, target: str, limit: int) -> list[SourceItem]:
        if not self._load():
            raise RuntimeError("No hay sesión válida. Corré: python scrape_ig.py api-login")

        uid = self._cl.user_id_from_username(target)
        medias = self._cl.user_medias(uid, amount=limit)
        log.info("IG @%s: %d medias bruto", target, len(medias))

        items: list[SourceItem] = []
        skipped_video = 0
        for m in medias:
            # media_type: 1=photo, 2=video, 8=carousel. Descartamos videos puros.
            if m.media_type == 2:
                skipped_video += 1
                continue
            urls = self._collect_image_urls(m)
            if not urls:
                skipped_video += 1
                continue
            items.append(
                SourceItem(
                    platform=self.platform,
                    external_id=str(m.pk),
                    author=target,
                    text=m.caption_text or "",
                    media_urls=urls,
                    url=f"https://www.instagram.com/p/{m.code}/",
                    posted_at=m.taken_at.isoformat() if m.taken_at else None,
                )
            )
        if skipped_video:
            log.info("  filtrados: %d videos", skipped_video)
        return items

    @staticmethod
    def _best_url(m) -> str | None:
        """URL de mayor resolución disponible en un Media.

        image_versions2 (SharedMediaImageVersions) tiene `.candidates` con versiones
        por resolución; elegimos la de mayor width. Fallback a thumbnail_url.
        Devuelve str (instagrapi usa HttpUrl de pydantic, hay que castear).
        """
        iv = getattr(m, "image_versions2", None)
        cands = getattr(iv, "candidates", None) if iv else None
        if cands:
            try:
                best = max(cands, key=lambda c: getattr(c, "width", 0) or 0)
                url = getattr(best, "url", None)
                if url:
                    return str(url)
            except (TypeError, ValueError):
                pass
        t = getattr(m, "thumbnail_url", None)
        return str(t) if t else None

    def _collect_image_urls(self, m) -> list[str]:
        """Para carrusel, una URL por slide (solo fotos). Para foto simple, una sola.

        Los slides (Resource) no tienen image_versions2, solo thumbnail_url.
        """
        if m.media_type == 8 and m.resources:
            urls: list[str] = []
            for r in m.resources:
                if r.media_type == 2:  # slide de video → saltear
                    continue
                if r.thumbnail_url:
                    urls.append(str(r.thumbnail_url))
            return urls
        u = self._best_url(m)
        return [u] if u else []

    def download_media(self, item: SourceItem, dest_dir: str) -> list[str]:
        """Descarga las imágenes a dest_dir. No pisa existentes.

        Usa el sessionid de la sesión para autenticar la descarga del CDN (las URLs
        de image_versions suelen ser firmadas y públicas por un tiempo, pero por
        si acaso mandamos la cookie).
        """
        out_dir = Path(dest_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        sid = self._read_sessionid()
        cookies = {"sessionid": sid} if sid else None

        saved: list[str] = []
        for i, url in enumerate(item.media_urls):
            ext = ".mp4" if (".mp4" in url or "/video/" in url) else ".jpg"
            fname = f"{item.external_id}_{i}{ext}"
            fpath = out_dir / fname
            if fpath.exists():
                saved.append(str(fpath))
                continue
            try:
                r = requests.get(
                    url, headers={"User-Agent": _DEFAULT_UA}, cookies=cookies, timeout=30
                )
                r.raise_for_status()
                fpath.write_bytes(r.content)
                saved.append(str(fpath))
                log.info("  descargado: %s", fname)
            except Exception as e:
                log.warning("  falló descarga %s: %s", url, e)
        return saved
