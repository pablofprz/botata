"""MCP server `reddit` — lectura de subreddits vía RSS/Atom, sin credenciales (T18).

Decisión T17: la API OAuth de Reddit está gateada (aprobación manual que rechaza
proyectos hobby); el RSS público funciona con un límite duro de **1 request por
minuto por IP, global**. Este server encapsula ese límite: serializa los
requests (≥61s entre hits, con espera bloqueante), cachea respuestas (TTL 10
min) y respeta `Retry-After` ante un 429.

Solo lectura. Stdlib + SDK mcp; sin credenciales ni estado en disco.

Config en butterbot (settings.json → MCP) — `call_timeout_s` alto porque una
llamada puede esperar hasta ~61s por el slot del rate limit:

    "reddit": { "transport": "stdio", "command": "python",
                "args": ["mcp_servers/reddit_server.py"],
                "call_timeout_s": 90 }

Correr a mano para probar: `python mcp_servers/reddit_server.py` (stdio).
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from mcp.server.fastmcp import FastMCP

UA = "butterbot-mcp-reddit/0.1 (community bot; RSS read-only)"
MIN_INTERVAL_S = 61.0   # límite medido en T17: 1 req/min por IP, global
CACHE_TTL_S = 600.0
MAX_LIMIT = 25

_ATOM = "{http://www.w3.org/2005/Atom}"
_VALID_SORT = {"hot", "new", "top", "rising"}
_VALID_WINDOW = {"hour", "day", "week", "month", "year", "all"}
_SUB_RE = re.compile(r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$")  # sub o multi (a+b)

_lock = threading.Lock()
_last_request_at = 0.0
_cache: dict[str, tuple[float, str]] = {}


# ─── HTTP con rate limit + cache ─────────────────────────────────────────────
def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def _fetch(url: str) -> str:
    """GET serializado: respeta el slot de 1 req/min y cachea por URL."""
    global _last_request_at
    with _lock:
        hit = _cache.get(url)
        if hit and (time.time() - hit[0]) < CACHE_TTL_S:
            return hit[1]
        wait = MIN_INTERVAL_S - (time.time() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            body = _http_get(url)
        except urllib.error.HTTPError as e:
            _last_request_at = time.time()
            if e.code == 429:  # respetar Retry-After si es razonable, un solo retry
                retry_after = float(e.headers.get("Retry-After") or MIN_INTERVAL_S)
                if retry_after <= 90:
                    time.sleep(retry_after)
                    body = _http_get(url)
                else:
                    raise RuntimeError(f"Reddit rate limit (429), Retry-After={retry_after}s") from e
            else:
                raise
        _last_request_at = time.time()
        _cache[url] = (time.time(), body)
        return body


# ─── Atom → posts ────────────────────────────────────────────────────────────
def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_atom(xml_text: str, limit: int) -> list[dict]:
    root = ET.fromstring(xml_text)
    posts = []
    for entry in root.iter(f"{_ATOM}entry"):
        link = entry.find(f"{_ATOM}link")
        author = entry.find(f"{_ATOM}author/{_ATOM}name")
        summary = _strip_html(entry.findtext(f"{_ATOM}content", ""))
        posts.append({
            "title":     (entry.findtext(f"{_ATOM}title") or "").strip(),
            "url":       link.get("href") if link is not None else None,
            "author":    author.text if author is not None else None,
            "published": entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated"),
            "summary":   summary[:300],
        })
        if len(posts) >= limit:
            break
    return posts


def _feed_url(subreddit: str, sort: str, time_window: str) -> str:
    sub = subreddit.removeprefix("r/").removeprefix("/r/")
    if not _SUB_RE.match(sub):
        raise ValueError(f"subreddit inválido: '{subreddit}'")
    if sort not in _VALID_SORT:
        raise ValueError(f"sort inválido: '{sort}' (usar {sorted(_VALID_SORT)})")
    if time_window not in _VALID_WINDOW:
        raise ValueError(f"time_window inválido: '{time_window}' (usar {sorted(_VALID_WINDOW)})")
    base = f"https://www.reddit.com/r/{sub}/"
    if sort == "hot":
        url = base + ".rss"
    else:
        url = base + f"{sort}/.rss"
    if sort == "top":
        url += f"?t={time_window}"
    return url


# ─── Tools ───────────────────────────────────────────────────────────────────
mcp = FastMCP("reddit")


@mcp.tool()
def get_subreddit_posts(subreddit: str, sort: str = "hot",
                        time_window: str = "week", limit: int = 10) -> str:
    """Lee los posts recientes de un subreddit (o multireddit 'a+b') vía RSS.

    sort: hot | new | top | rising. time_window (solo para top): hour | day |
    week | month | year | all. Devuelve JSON con title/url/author/published/summary.
    OJO: Reddit limita a 1 request/min — la llamada puede tardar hasta ~1 min
    si hubo otra consulta reciente (las repetidas salen de cache).
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    url = _feed_url(subreddit, sort, time_window)
    posts = _parse_atom(_fetch(url), limit)
    if not posts:
        return json.dumps({"subreddit": subreddit, "posts": [],
                           "note": "sin posts (¿subreddit vacío, privado o inexistente?)"},
                          ensure_ascii=False)
    return json.dumps({"subreddit": subreddit, "sort": sort, "posts": posts},
                      ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()  # stdio
