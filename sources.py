"""sources.py — adaptadores de plataforma sobre la capa de navegador.

Cada `Source` sabe navegar UNA plataforma (URLs, paginación, quirks) y delega la
extracción al `LLMExtractor` genérico. La parte específica por sitio se mantiene
delgada a propósito: juntar permalinks y poco más. IG está implementado; Reddit y
X quedan como ganchos (interfaz lista, sin implementar).

Estrategia de extracción para Instagram:
  - Primaria: meta tags (og:title, og:description, og:image) — instantánea, sin tokens.
  - Fallback: LLMExtractor sobre el texto de la página si no hay meta tags.
"""
from __future__ import annotations

import logging
import random
import re
import time
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from browser import BrowserSession
from extract import LLMExtractor

log = logging.getLogger("butterbot.sources")

# Delays humanos entre requests para evitar detección de bot
_DELAY_BETWEEN_POSTS = (8, 18)     # segundos entre cada post del feed
_DELAY_BETWEEN_ACCOUNTS = (15, 35)  # segundos entre cuentas


# ─── Modelos ────────────────────────────────────────────────────────────────
class SourceItem(BaseModel):
    """Un ítem scrapeado, agnóstico de plataforma. Lo que se persiste y procesa."""
    platform: str
    external_id: str                                    # shortcode / post id — dedup
    author: str | None = None
    text: str = ""
    media_urls: list[str] = Field(default_factory=list)
    url: str = ""
    posted_at: str | None = None


class ExtractedPost(BaseModel):
    """Schema que el LLM llena desde la página de un post individual (fallback)."""
    author: str | None = Field(None, description="handle/usuario del autor del post")
    text: str = Field("", description="texto o caption del post")
    media_urls: list[str] = Field(default_factory=list, description="URLs de imágenes o videos")
    posted_at: str | None = Field(None, description="fecha de publicación en ISO si es visible")


# ─── Interfaz ────────────────────────────────────────────────────────────────
class Source(ABC):
    """Adaptador de una plataforma. Reusable: todos devuelven SourceItem."""

    platform: str

    def __init__(self, browser: BrowserSession, extractor: LLMExtractor) -> None:
        self.browser = browser
        self.extractor = extractor

    @abstractmethod
    def fetch_recent(self, target: str, limit: int) -> list[SourceItem]:
        """Devuelve los `limit` ítems más recientes de `target` (cuenta/sub/etc.)."""
        ...


# ─── Instagram (implementado) ────────────────────────────────────────────────
class IGSource(Source):
    platform = "instagram"
    _PERMALINK_RE = re.compile(r"/(p|reel)/([^/?#]+)")

    # Meta tags de Instagram: contienen todo lo necesario sin LLM.
    # og:description = "N likes, M comments - handle el Fecha: \"caption\"."
    _OG_DESC_RE = re.compile(
        r"[\d,]+ likes.*?-\s*"           # likes + " - "
        r"(\S+)\s+el\s+"                  # grupo 1: author handle
        r"(.+?):\s*"                      # grupo 2: fecha ("July 6, 2026")
        r"\"(.+?)\"\s*\.?\s*$",           # grupo 3: caption (entre comillas)
        re.DOTALL,
    )
    _OG_TITLE_RE = re.compile(r"^.+en Instagram:")  # prefijo a remover del og:title

    # ---------- helpers de extracción de HTML ----------
    @staticmethod
    def _meta(html: str, name: str) -> str | None:
        """Extrae el content de un <meta property='og:...' y decodifica HTML entities."""
        m = re.search(
            rf'<meta\s+property=["\']og:{name}["\']\s+content=["\']([^"\']+)["\']',
            html,
        )
        if not m:
            return None
        # Decodificar HTML entities (&quot; → ", &amp; → &, etc.)
        import html as _html
        return _html.unescape(m.group(1))

    def _extract_post_meta(self, html: str, code: str, target: str, url: str,
                          media_urls: list[str]) -> SourceItem | None:
        """Extrae metadata (autor, texto, fecha) de los meta tags. Las imágenes ya vienen de fuera."""
        og_desc = self._meta(html, "description")

        if not og_desc:
            return None

        text = ""
        author = target
        posted_at = None

        # og:description tiene tres formatos:
        #   A) "N likes, M comments - handle el Fecha: \"caption\""
        #   B) "N likes, M comments - handle el Fecha"  (sin caption)
        #   C) "N likes, M comments - handle el Fecha: caption sin comillas"
        m = self._OG_DESC_RE.search(og_desc)
        if m:
            author = m.group(1)
            posted_at = m.group(2).strip()
            text = m.group(3).strip()
        else:
            m2 = re.match(
                r"[\d,]+ likes.*?-\s*(\S+)\s+el\s+(.+?):\s*(.+?)\s*\.?\s*$",
                og_desc, re.DOTALL,
            )
            if m2:
                author = m2.group(1)
                posted_at = m2.group(2).strip()
                text = m2.group(3).strip()
            else:
                m3 = re.match(
                    r"[\d,]+ likes.*?-\s*(\S+)\s+el\s+(.+?)$",
                    og_desc, re.DOTALL,
                )
                if m3:
                    author = m3.group(1)
                    posted_at = m3.group(2).strip()

        return SourceItem(
            platform=self.platform,
            external_id=code,
            author=author,
            text=text,
            media_urls=media_urls,
            url=url,
            posted_at=posted_at,
        )

    @staticmethod
    def _extract_carousel_images(page) -> list[str]:
        """Extrae TODAS las URLs de imágenes del post desde el DOM vivo.

        Las imágenes de Instagram están en <li> dentro del carrusel (uno por slide).
        Se recolectan todas las que apunten al CDN y no sean avatar (150x150).
        No se navega entre posts: solo se mira el DOM actual.
        """
        images: list[str] = page.evaluate('''
            () => {
                const seen = [];
                document.querySelectorAll('li').forEach(li => {
                    li.querySelectorAll('img').forEach(img => {
                        const src = img.src;
                        if (src && src.includes('cdninstagram.com') && !src.includes('150x150')) {
                            seen.push(src);
                        }
                    });
                });
                // Si no hay <li> con imágenes, fallback: primer <img> de CDN no-perfil
                if (!seen.length) {
                    const imgs = document.querySelectorAll('img');
                    for (const img of imgs) {
                        if (img.src && img.src.includes('cdninstagram.com') && !img.src.includes('150x150') && img.naturalWidth > 150) {
                            seen.push(img.src);
                            break;
                        }
                    }
                }
                // Dedup
                return [...new Set(seen)];
            }
        ''')
        return images

    # ---------- métodos públicos ----------
    def is_logged_in(self) -> bool:
        """Verifica sesión yendo a un perfil conocido (instagram). Si redirige a login, no hay sesión."""
        page = self.browser.context.new_page()
        try:
            page.goto("https://www.instagram.com/instagram/", wait_until="domcontentloaded")
            return "/accounts/login" not in page.url
        finally:
            page.close()

    def fetch_recent(self, target: str, limit: int) -> list[SourceItem]:
        codes = self._collect_permalinks(target, limit)
        log.info("IG @%s: %d permalinks (post-filtro)", target, len(codes))

        items: list[SourceItem] = []
        skipped_video = 0
        for code, url in codes:
            page = self.browser.context.new_page()
            try:
                page.goto(url, wait_until="networkidle")
                page.wait_for_timeout(1500)  # dejar que el carrusel renderice

                html = page.content()

                # Fallback: si la página tiene og:video o og:type=video, es video → skip
                og_type = self._meta(html, "type")
                og_video = self._meta(html, "video")
                if og_type == "video" or og_video:
                    skipped_video += 1
                    log.debug("  [skip] %s: video (detectado en meta tags)", code)
                    continue

                # Extraer TODAS las imágenes del carrusel (no solo el thumbnail)
                all_images = self._extract_carousel_images(page)
                if not all_images:
                    log.debug("  [skip] %s: sin imágenes (posible video)", code)
                    skipped_video += 1
                    continue

                # Extraer metadata (texto, autor, fecha) de los meta tags
                item = self._extract_post_meta(html, code, target, url, all_images)
                if item is None:
                    log.warning("IG @%s: extracción vacía para %s", target, code)
                    continue
                items.append(item)
                log.info("  [meta] %s | %s (%d img)", code,
                         (item.text or "(sin caption)")[:50].replace("\n", " "),
                         len(item.media_urls))
            finally:
                page.close()

            # Delay humano entre posts
            delay = random.uniform(*_DELAY_BETWEEN_POSTS)
            time.sleep(delay)

        if skipped_video:
            log.info("  filtrados en fetch: %d videos", skipped_video)
        return items

    def _collect_permalinks(self, target: str, limit: int) -> list[tuple[str, str]]:
        """Junta permalinks de la grilla del perfil, excluyendo fijados y videos/reels.

        Usa el DOM vivo (JavaScript) porque la grilla es SPA. Cada post se inspecciona
        para detectar:
          - Reels: URL /reel/ → excluido (siempre son video).
          - Videos: post /p/ con SVG de play (aria-label con "Clip" o "Reproducir").
          - Fijados: SVG de pin (aria-label con "Fijad" o "Pinned") en el contenedor.
        """
        page = self.browser.context.new_page()
        try:
            page.goto(f"https://www.instagram.com/{target}/", wait_until="networkidle")
            page.wait_for_timeout(2500)

            # Si redirigió a login, no hay sesión
            if "/accounts/login" in page.url:
                log.error("Instagram redirigió a login — las cookies expiraron. Re-importalas.")
                return []

            # Devolver [{code, isReel, isVideo, isPinned}] desde JS
            raw = page.evaluate('''
                () => {
                    const result = [];
                    const anchors = document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]');
                    anchors.forEach(a => {
                        const href = a.href;
                        const match = href.match(/\\/(p|reel)\\/([^\\/?#]+)/);
                        if (!match) return;
                        const code = match[2];
                        const isReel = match[1] === 'reel';

                        // Detectar video: SVG con aria-label de play/clip dentro del anchor
                        let isVideo = isReel;
                        if (!isVideo) {
                            const svgs = a.querySelectorAll('svg[aria-label]');
                            svgs.forEach(s => {
                                const label = (s.getAttribute('aria-label') || '').toLowerCase();
                                if (label.includes('clip') || label.includes('reproducir') || label.includes('play')) {
                                    isVideo = true;
                                }
                            });
                        }

                        // Detectar fijado: SVG con aria-label de pin en el contenedor
                        let isPinned = false;
                        const parent = a.closest('div');
                        const container = parent ? parent.parentElement : null;
                        if (container) {
                            const svgs = container.querySelectorAll('svg[aria-label]');
                            svgs.forEach(s => {
                                const label = s.getAttribute('aria-label') || '';
                                if (label.includes('Fijad') || label.includes('Pinned') || label.includes('Pin')) {
                                    isPinned = true;
                                }
                            });
                        }

                        result.push({code, isReel, isVideo, isPinned});
                    });
                    return result;
                }
            ''')
        finally:
            page.close()

        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        skipped_pinned = 0
        skipped_video = 0
        for item in raw:
            code = item['code']
            if code in seen:
                continue
            seen.add(code)

            if item['isPinned']:
                skipped_pinned += 1
                log.debug("  [skip] %s: fijado", code)
                continue
            if item['isVideo']:
                skipped_video += 1
                log.debug("  [skip] %s: video/reel", code)
                continue

            out.append((code, f"https://www.instagram.com/p/{code}/"))
            if len(out) >= limit:
                break

        if skipped_pinned or skipped_video:
            log.info("  filtrados: %d fijados, %d videos", skipped_pinned, skipped_video)
        return out

    def download_media(self, item: SourceItem, dest_dir: str) -> list[str]:
        """Descarga las imágenes de un ítem a `dest_dir`. No pisa archivos existentes.

        Devuelve paths locales de las imágenes nuevas (no las ya existentes).
        """
        from pathlib import Path
        import requests

        out_dir = Path(dest_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []

        for i, url in enumerate(item.media_urls):
            ext = ".jpg"
            if ".mp4" in url or "/video/" in url:
                ext = ".mp4"
            elif ".png" in url:
                ext = ".png"

            fname = f"{item.external_id}_{i}{ext}"
            fpath = out_dir / fname

            if fpath.exists():
                log.debug("  ya existe: %s", fname)
                saved.append(str(fpath))
                continue

            try:
                r = requests.get(url, headers={"User-Agent": _DEFAULT_UA}, timeout=30)
                r.raise_for_status()
                fpath.write_bytes(r.content)
                saved.append(str(fpath))
                log.info("  descargado: %s", fname)
            except Exception as e:
                log.warning("  falló descarga %s: %s", url, e)

        return saved


# Constante local para el downloader (no exponer la del browser)
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ─── Ganchos: Reddit / X (interfaz lista, sin implementar) ───────────────────
class RedditSource(Source):
    platform = "reddit"

    def fetch_recent(self, target: str, limit: int) -> list[SourceItem]:
        raise NotImplementedError(
            "Reddit: preferir la API oficial (PRAW) antes que navegador — es estable "
            "y gratis. Adaptador pendiente."
        )


class TwitterSource(Source):
    platform = "twitter"

    def fetch_recent(self, target: str, limit: int) -> list[SourceItem]:
        raise NotImplementedError("Twitter/X: adaptador pendiente.")
