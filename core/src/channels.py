"""channels.py — T28: abstracción de canal (multi-plataforma).

El grafo de botata no sabe en qué red habla: le habla a un *canal* por duck
typing. El contrato Channel es la superficie que los nodos usan hoy (BskyClient
en botata.py es la implementación original y de referencia):

    .handle                                   # cuenta del bot en el canal
    .get_mentions() -> list[dict]             # {uri, cid, author_handle, text}
    .mark_all_read() -> None
    .get_thread_info(uri, cid) -> (context_text, root_uri, root_cid, leaf_media)
    .get_mention_by_uri(uri) -> dict | None   # + thread_context/thread_root_uri/cid
    .reply(text, parent_uri, parent_cid, root_uri, root_cid, media_path=None) -> uri
    .post(text, limit=None, media_path=None, target=None) -> uri
        (limit=None → MAX_POST_LEN de la clase: 295 Bluesky, 490 Mastodon,
         1990 Discord, 4000 WhatsApp)
                                              # target: destino opcional (rutinas con channel) —
                                              # Discord = id de canal; Bluesky y
                                              # Mastodon lo ignoran (un solo timeline)
    .like_post(uri, cid) -> bool              # "me gusta" sobre un post ajeno
                                              # (like en Bluesky, favourite en
                                              # Mastodon, reacción en Discord/WhatsApp)
    .set_bio(text) -> bool                    # actualiza la bio del perfil del bot
    .get_profile(handle) -> obj(.did, .display_name, .description) | None
    .block_user(handle) -> bool
    .set_media_describer(fn) -> None          # vision inyectada (puede ignorarse)
    .get_feed_posts(source_type, identifier, since, limit) -> list[dict]
                                              # {handle, text, uri, indexed_at, reply_to}
    .get_list_members(uri) -> list[str] · .get_follows() -> list[str]

Identificadores: cada canal usa los suyos (Bluesky: at:// URIs + cid; Mastodon:
ids de status; Discord: "channel_id/message_id" — la reply necesita el canal).
El resto del sistema los trata como strings opacos — la DB (`replied_posts`,
`bot_posts`) ya es agnóstica. En Mastodon `cid` == id del status; en Discord
`cid` == message_id (el concepto no existe; se duplica para no tocar el grafo).

MastodonChannel usa Mastodon.py (import lazy: los deploys Bluesky no lo pagan).
DiscordChannel usa la API REST v10 por httpx (ya es dep del motor) — SIN
discord.py ni gateway: el poll loop del bot ya marca la cadencia, y el dedup
vive en la DB, así que releer los últimos mensajes por canal es inocuo.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
from collections import deque
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

log = logging.getLogger("botata.channels")

# Con qué gesto marca el bot que algo le gustó donde no hay "like" (Discord,
# WhatsApp): una reacción. Uno solo y para todos los canales — el gesto es el
# mismo, cambia la mecánica.
LIKE_EMOJI = "❤️"

_BR_RE = re.compile(r"<br\s*/?>", re.I)
_P_RE = re.compile(r"</p>\s*<p[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    """Contenido de Mastodon (HTML) → texto plano. <br> y saltos de párrafo → \\n."""
    if not html:
        return ""
    text = _BR_RE.sub("\n", html)
    text = _P_RE.sub("\n\n", text)
    text = _TAG_RE.sub("", text)
    return unescape(text).strip()


# El contexto del bot anota la media que VE con corchetes: "[imagen: un gato]",
# "[video, primer frame: …]", "[GIF: …]", "[image: foto.jpg]" (Discord). El LLM
# aprende ese formato de tanto verlo y termina ESCRIBIÉNDOLO en sus posts, o sea
# fingiendo un adjunto que no existe — el usuario ve "[imagen: un mapache]" en vez
# de un mapache. Se limpia acá, en la salida, porque es el único lugar por donde
# pasa todo lo que el bot publica en cualquier canal.
_MEDIA_FAKE_RE = re.compile(
    r"\[\s*(?:imagen|image|img|foto|video|vídeo|gif|adjunto|attachment)\b[^\]]*\]",
    re.I)

# La otra forma de fingir un adjunto, vista en producción el 2026-07-28:
#   [📸 mapache para el admin](https://cdn.bsky.app/img/feed_thumbnail/…)
# Un link markdown con una URL de CDN INVENTADA. No lo cazaba la regex de arriba
# (la etiqueta no empieza con una palabra de media) y el usuario veía texto donde
# esperaba una foto. Se resuelve en dos pasos: si la etiqueta huele a media, se
# tira todo; si no, se desenvuelve a la URL pelada — el bot no escribe markdown
# (lo dice el SOUL) pero compartir un link sí es legítimo.
_MD_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\(\s*([^)\s]*)[^)]*\)")
_ETIQUETA_MEDIA_RE = re.compile(
    r"imagen|image|img|foto|photo|video|vídeo|gif|adjunto|attachment"
    r"|[\U0001F4F8\U0001F4F7\U0001F5BC\U0001F3A5\U0001F3AC\U0001F4F9]",
    re.I)


def _desarmar_link_markdown(m: re.Match) -> str:
    etiqueta, url = m.group(1), m.group(2)
    if m.group(0).startswith("!") or _ETIQUETA_MEDIA_RE.search(etiqueta):
        return ""                       # adjunto fingido: no queda nada
    return url if url.startswith(("http://", "https://")) else etiqueta


def strip_fake_media(text: str) -> str:
    """Saca las anotaciones de media que el LLM haya escrito en el texto.

    Las anotaciones son ENTRADA (lo que el bot ve), nunca salida. Adjuntar de
    verdad es cosa del pipeline (`media_path`), no del texto.
    """
    limpio = _MD_LINK_RE.sub(_desarmar_link_markdown, text or "")
    limpio = _MEDIA_FAKE_RE.sub("", limpio)
    # Puede quedar el renglón vacío o espacios dobles donde estaba la anotación.
    limpio = re.sub(r"[ \t]{2,}", " ", limpio)
    limpio = re.sub(r"\n{3,}", "\n\n", limpio)
    return limpio.strip()


def truncate_post(text: str, limit: int) -> str:
    """Corte en frontera de oración (o último espacio) — mismo criterio que
    BskyClient.post, extraído para compartirlo entre canales."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut) - 1, max(len(cut) - 60, 0), -1):
        if cut[i] in ".!?":
            return cut[: i + 1]
    # Si el único espacio está al principio, cortar ahí devolvía "" — y un
    # post vacío es 400/422 en cualquier canal. Mejor a hacha que vacío.
    if " " in cut and cut.rfind(" ") > 0:
        return cut[: cut.rfind(" ")]
    return cut


class MentionRefetchError(Exception):
    """get_mention_by_uri no pudo AVERIGUAR si el post sigue existiendo (red
    caída, bridge apagado, 5xx). No es lo mismo que None (= borrado seguro):
    ante esto el caller deja la mención como 'failed' y reintenta después.
    Confundir ambos hacía que un arranque con el endpoint caído 30 segundos
    descartara como 'ignored' PERMANENTE todas las menciones pendientes."""


def _http_status(e: Exception) -> int | None:
    """Status code de una excepción de httpx/mastodon, si lo trae."""
    return getattr(getattr(e, "response", None), "status_code", None)


class MastodonChannel:
    """Canal Mastodon (Mastodon.py). Primera implementación no-Bluesky de T28.

    Diferencias absorbidas acá (el grafo no se entera):
    - Sin facets: Mastodon linkea @menciones y URLs solo.
    - Las replies DEBEN mencionar al autor para notificarlo → se antepone
      `@acct` si el LLM no lo incluyó.
    - Límite 500 chars (el default de los callers, 295/300, entra sobrado).
    - `mark_all_read` es no-op: el dedup vive en la DB (`has_replied`), igual
      que en Bluesky, así que releer notificaciones viejas es inocuo.
    - Media del hilo: se usa el alt-text de los attachments (sin vision por
      ahora); `set_media_describer` se acepta y se ignora.
    """

    def __init__(self, api_base_url: str, access_token: str, api=None):
        if api is None:
            from mastodon import Mastodon  # lazy: dep solo si el canal se usa
            api = Mastodon(access_token=access_token, api_base_url=api_base_url,
                           request_timeout=30)
        self._api = api
        me = api.account_verify_credentials()
        self._me_id = me["id"]
        self.handle = me["acct"]
        log.info("Logged in to Mastodon as @%s (%s)", self.handle, api_base_url)

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _status_text(status) -> str:
        return strip_html(status.get("content") or "")

    @staticmethod
    def _media_note(status) -> str:
        """Alt-texts de los attachments como anotación estilo READ_THREAD_MEDIA."""
        pieces = []
        for att in status.get("media_attachments") or []:
            kind = att.get("type") or "media"
            desc = (att.get("description") or "").strip()
            pieces.append(f"[{kind}: {desc}]" if desc else f"[{kind} sin descripción]")
        return " ".join(pieces)

    def _full_text(self, status) -> str:
        text = self._status_text(status)
        media = self._media_note(status)
        return f"{text} {media}".strip() if media else text

    def _lookup_account(self, handle: str):
        handle = handle.lstrip("@")
        try:
            return self._api.account_lookup(handle)
        except Exception:
            try:  # instancias/libs viejas sin account_lookup
                results = self._api.account_search(handle, limit=1)
                return results[0] if results else None
            except Exception as e:
                log.warning("No pude resolver la cuenta @%s: %s", handle, e)
                return None

    # ── contrato Channel ─────────────────────────────────────────────────
    def get_mentions(self) -> list[dict]:
        try:
            notifs = self._api.notifications(limit=25)
        except Exception as e:
            log.error("notifications falló: %s", e)
            return []
        mentions = []
        for n in notifs:
            if n.get("type") != "mention" or not n.get("status"):
                continue
            status = n["status"]
            mentions.append({
                "uri"           : str(status["id"]),
                "cid"           : str(status["id"]),
                "author_handle" : n["account"]["acct"],
                "text"          : self._status_text(status),
            })
        return mentions

    def mark_all_read(self) -> None:
        pass  # dedup por DB; descartar notificaciones sería destructivo

    def get_thread_info(self, uri: str, cid: str) -> tuple[str, str, str, str]:
        try:
            ctx = self._api.status_context(uri)
            leaf = self._api.status(uri)
        except Exception as e:
            log.warning("No pude traer el hilo de %s: %s", uri, e)
            return "", uri, cid, ""
        ancestors = ctx.get("ancestors") or []
        lines = [f"{s['account']['acct']}: {self._full_text(s)}" for s in ancestors]
        root_id = str(ancestors[0]["id"]) if ancestors else uri
        return "\n".join(lines), root_id, root_id, self._media_note(leaf)

    def get_mention_by_uri(self, uri: str) -> dict | None:
        try:
            status = self._api.status(uri)
        except Exception as e:
            # 404 = borrado de verdad → None. Cualquier otra cosa (red, 5xx,
            # auth) no dice nada sobre el post: no habilita descartarlo.
            if _http_status(e) == 404 or type(e).__name__ == "MastodonNotFoundError":
                return None
            raise MentionRefetchError(f"{type(e).__name__}: {e}") from e
        context, root_uri, root_cid, _ = self.get_thread_info(uri, uri)
        return {
            "uri"            : str(status["id"]),
            "cid"            : str(status["id"]),
            "author_handle"  : status["account"]["acct"],
            "text"           : self._full_text(status),
            "thread_context" : context,
            "thread_root_uri": root_uri,
            "thread_root_cid": root_cid,
        }

    def reply(self, text: str, parent_uri: str, parent_cid: str,
              root_uri: str, root_cid: str, media_path: str | None = None) -> str:
        text = strip_fake_media(text)
        prefijo = ""
        try:
            parent = self._api.status(parent_uri)
            author = parent["account"]["acct"]
            if f"@{author}".lower() not in text.lower():
                prefijo = f"@{author} "
        except Exception:
            log.debug("no pude traer el parent %s para mencionar al autor", parent_uri)
        # El truncado va DESPUÉS de reservar la mención: truncar a 490 y recién
        # ahí anteponer @autor pasaba los 500 con un acct remoto largo → 422 →
        # la mención quedaba 'failed' y reintentaba con el mismo resultado.
        # (Y truncate_post, no [:490] a hacha: mismo criterio que post.)
        text = prefijo + truncate_post(text, 490 - len(prefijo))
        media_ids = self._upload_media(media_path) if media_path else None
        status = self._api.status_post(text, in_reply_to_id=parent_uri,
                                       media_ids=media_ids)
        return str(status["id"])

    MAX_POST_LEN = 490  # 500 de Mastodon, con margen (mismo criterio que reply)

    def post(self, text: str, limit: int | None = None, media_path: str | None = None,
             target: str | None = None) -> str:
        if target:
            log.debug("post: Mastodon no tiene canales — target %r ignorado", target)
        text = truncate_post(strip_fake_media(text), limit or self.MAX_POST_LEN)
        media_ids = self._upload_media(media_path) if media_path else None
        status = self._api.status_post(text, media_ids=media_ids)
        return str(status["id"])

    def _upload_media(self, media_path: str) -> list | None:
        try:
            media = self._api.media_post(media_path)
            return [media]
        except Exception as e:
            log.warning("upload de media falló (%s): %s — posteo sin media",
                        Path(media_path).name, e)
            return None

    def like_post(self, uri: str, cid: str = "") -> bool:
        """Favorito sobre un status. Idempotente del lado de Mastodon."""
        try:
            self._api.status_favourite(uri)
            return True
        except Exception as e:
            log.warning("like_post (Mastodon) falló para %s: %s", uri, e)
            return False

    def set_bio(self, text: str) -> bool:
        """Actualiza la bio (note) de la cuenta del bot. True si OK."""
        text = strip_fake_media(text)
        try:
            self._api.account_update_credentials(note=text)
            return True
        except Exception as e:
            log.error("set_bio (Mastodon): %s", e)
            return False

    def get_profile(self, handle: str):
        account = self._lookup_account(handle)
        if account is None:
            return None
        return SimpleNamespace(
            did=str(account["id"]),
            display_name=account.get("display_name") or None,
            description=strip_html(account.get("note") or ""),
        )

    def resolve_did(self, handle: str) -> str | None:
        account = self._lookup_account(handle)
        return str(account["id"]) if account else None

    def block_user(self, handle: str) -> bool:
        account = self._lookup_account(handle)
        if account is None:
            log.error("block_user: no pude resolver @%s", handle)
            return False
        try:
            self._api.account_block(account["id"])
            log.info("block_user: bloqueado @%s", handle)
            return True
        except Exception as e:
            log.error("block_user: falló bloquear @%s: %s", handle, e)
            return False

    def set_media_describer(self, fn) -> None:
        # Vision sobre media ajena: pendiente en Mastodon; se usa el alt-text.
        # Warning y no debug: el admin tiene que ENTERARSE de que en este canal
        # el bot no ve imágenes (antes se lo tragaba en silencio).
        log.warning("Mastodon: vision sobre imágenes no implementada — el bot "
                    "solo lee el alt-text de los attachments")

    # ── fuentes de feed (loop proactivo) ─────────────────────────────────
    def get_feed_posts(self, source_type: str, identifier: str | None,
                       since: datetime | None, limit: int = 50) -> list[dict]:
        """`following` → home; `local` → timeline local; `list` → lista por id."""
        try:
            if source_type == "following":
                statuses = self._api.timeline_home(limit=min(limit, 40))
            elif source_type == "local":
                statuses = self._api.timeline_local(limit=min(limit, 40))
            elif source_type == "list" and identifier:
                statuses = self._api.timeline_list(identifier, limit=min(limit, 40))
            else:
                log.warning("fuente de feed no soportada en Mastodon: %s", source_type)
                return []
        except Exception as e:
            log.error("timeline %s falló: %s", source_type, e)
            return []
        posts = []
        for s in statuses:
            created = s.get("created_at")
            if isinstance(created, str):
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if since and created and created <= since:
                break  # las timelines vienen en orden reverso-cronológico
            text = self._status_text(s)
            if not text:
                continue
            posts.append({
                "handle"     : s["account"]["acct"],
                "text"       : text,
                "uri"        : str(s["id"]),
                "indexed_at" : created.isoformat() if created else "",
                # el acct del padre pediría un fetch extra por post — se omite
                "reply_to"   : None,
            })
        log.info("timeline %s: %d posts", source_type, len(posts))
        return posts

    def get_list_members(self, list_id: str) -> list[str]:
        try:
            accounts = self._api.list_accounts(list_id)
            return [a["acct"].lower() for a in accounts]
        except Exception as e:
            log.warning("list_accounts falló para %s: %s", list_id, e)
            return []

    def get_follows(self) -> list[str]:
        try:
            page = self._api.account_following(self._me_id, limit=80)
            accounts = self._api.fetch_remaining(page)
            return [a["acct"].lower() for a in accounts]
        except Exception as e:
            log.warning("account_following falló: %s", e)
            return []


# ─────────────────────────────────────────────────────────────────────────────
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
_CHAN_MENTION_RE = re.compile(r"<#(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


class DiscordChannel:
    """Canal Discord por REST v10 (httpx, sin discord.py ni gateway).

    Modelo: el bot escucha una lista fija de canales (`channel_ids`, settings
    `DISCORD_CHANNEL_IDS`). "Mención" = mensaje que @menciona al bot o que
    responde (reply) a un mensaje del bot. El resto de los mensajes se ignora.

    Diferencias absorbidas acá (el grafo no se entera):
    - Push vs poll: no hay endpoint de notificaciones — se leen los mensajes
      nuevos por canal en cada poll (cursor `after` en memoria, paginado con
      tope) y el dedup lo hace la DB (has_replied).
    - uri = "channel_id/message_id" (postear una reply exige el canal);
      cid = message_id.
    - Hilo: se reconstruye subiendo por message_reference (cadena de replies),
      no existe el thread à la Bluesky.
    - Menciones en el content vienen como <@id> → se traducen a @username con
      el array `mentions` del mensaje.
    - Sin bios para bots ni bloqueo de usuarios: get_profile devuelve
      description vacía (con cache de usuarios vistos), block_user es no-op.
    - Los mensajes de otros bots se ignoran (anti-loop).
    - Media: los attachments traen URL del CDN → vision sobre las imágenes del
      mensaje que se va a contestar; el resto se anota por filename/content_type.
    - Límite 2000 chars (los callers pasan ≤300, entra sobrado).
    """

    API = "https://discord.com/api/v10"
    CATCHUP_PAGE = 100        # máximo por request que acepta la API
    MAX_CATCHUP_PAGES = 5     # tope duro por canal y ciclo (500 msgs)
    THREAD_HOPS = 12          # tope de la cadena de replies al reconstruir la raíz
    BUFFER_POR_CANAL = 300    # cuánto se recuerda por canal (techo de memoria)

    def __init__(self, bot_token: str, channel_ids: list[str], http=None,
                 context_messages: int = 20):
        if http is None:
            import httpx  # lazy: simetría con los otros canales
            http = httpx.Client(
                base_url=self.API,
                headers={"Authorization": f"Bot {bot_token}"},
                timeout=30,
            )
        self._http = http
        self._channel_ids = [str(c) for c in channel_ids]
        self._users: dict[str, dict] = {}  # username → user visto en mensajes
        self._last_seen: dict[str, str] = {}   # channel_id → id del último mensaje leído
        self._media_describer = None           # vision inyectada (set_media_describer)
        self._context_messages = max(0, int(context_messages))
        # Conversación reciente por canal (mismo patrón que WhatsApp): en un
        # canal de Discord la gente contesta sin reply formal, así que la
        # cadena de references sola dejaba al bot sordo — contestaba sin saber
        # de qué se estaba hablando.
        self._recientes: dict[str, deque] = {}
        me = self._get("/users/@me")
        self._me_id = str(me["id"])
        self.handle = me["username"]
        log.info("Logged in to Discord as @%s (escuchando %d canal(es))",
                 self.handle, len(self._channel_ids))

    # ── HTTP helpers (retry simple ante 429) ─────────────────────────────
    def _request(self, method: str, path: str, **kw):
        for attempt in (1, 2):
            resp = self._http.request(method, path, **kw)
            if resp.status_code == 429 and attempt == 1:
                wait = float(resp.headers.get("Retry-After", "1"))
                log.warning("Discord 429 en %s — espero %.1fs", path, wait)
                time.sleep(min(wait, 10))
                continue
            resp.raise_for_status()
            return resp.json() if resp.status_code != 204 else None
        return None

    def _get(self, path: str, **params):
        return self._request("GET", path, params=params or None)

    # ── mapeo mensaje → dict del grafo ───────────────────────────────────
    def _remember_users(self, msg: dict) -> None:
        for u in [msg.get("author") or {}, *(msg.get("mentions") or [])]:
            if u.get("username"):
                self._users[u["username"].lower()] = u

    def _msg_text(self, msg: dict) -> str:
        """content → texto plano: <@id> → @username, <#id>/<@&id> se limpian."""
        by_id = {str(u["id"]): u for u in msg.get("mentions") or []}
        if msg.get("author"):
            by_id.setdefault(str(msg["author"]["id"]), msg["author"])

        def _user(m):
            u = by_id.get(m.group(1))
            return f"@{u['username']}" if u else "@?"

        text = _USER_MENTION_RE.sub(_user, msg.get("content") or "")
        text = _CHAN_MENTION_RE.sub("#canal", text)
        text = _ROLE_MENTION_RE.sub("", text)
        return text.strip()

    def _media_note(self, msg: dict, *, describe: bool = False) -> str:
        """Anota los adjuntos del mensaje. Con `describe`, corre vision sobre las
        imágenes (los attachments de Discord traen URL directa al CDN) en vez de
        anotar solo el nombre del archivo — sin esto el bot 've' `[image: foto.jpg]`
        y no puede comentar un meme que le mandan.

        Se describe SOLO donde importa (el mensaje que el bot está por contestar),
        nunca en la lectura masiva del feed: una llamada de vision por imagen y por
        mensaje leído sería carísimo."""
        pieces = []
        for att in msg.get("attachments") or []:
            kind = (att.get("content_type") or "file").split("/")[0]
            name = att.get("filename") or ""
            desc = ""
            if describe and kind == "image" and self._media_describer and att.get("url"):
                try:
                    desc = self._media_describer(att["url"]) or ""
                except Exception:
                    log.debug("vision sobre attachment falló", exc_info=True)
            if desc:
                pieces.append(f"[{kind}: {desc}]")
            else:
                pieces.append(f"[{kind}: {name}]" if name else f"[{kind}]")
        return " ".join(pieces)

    def _full_text(self, msg: dict, *, describe: bool = False) -> str:
        text = self._msg_text(msg)
        media = self._media_note(msg, describe=describe)
        return f"{text} {media}".strip() if media else text

    def _is_for_me(self, msg: dict) -> bool:
        if str((msg.get("author") or {}).get("id")) == self._me_id:
            return False
        if (msg.get("author") or {}).get("bot"):
            return False  # anti-loop entre bots
        if any(str(u["id"]) == self._me_id for u in msg.get("mentions") or []):
            return True
        ref = msg.get("referenced_message")
        return bool(ref and str((ref.get("author") or {}).get("id")) == self._me_id)

    def _to_mention(self, channel_id: str, msg: dict) -> dict:
        self._remember_users(msg)
        return {
            "uri"           : f"{channel_id}/{msg['id']}",
            "cid"           : str(msg["id"]),
            "author_handle" : msg["author"]["username"],
            "text"          : self._msg_text(msg),
        }

    def _new_messages(self, ch: str) -> list[dict]:
        """Mensajes del canal desde el último poll, paginando hacia el presente.

        Sin gateway, el riesgo real no es releer de más sino **perder menciones**:
        si en un ciclo entran más mensajes que el lote que leemos, los más viejos
        del lote nunca se ven y el dedup por DB no los recupera (nunca se leyeron).
        Por eso, con cursor, se pagina con `after` hasta alcanzar el presente, con
        un tope por ciclo para que un canal desbocado no se coma el loop.

        El cursor vive en memoria: al reiniciar se relee la ventana inicial, y las
        menciones a medio procesar las recupera `retry_stuck_mentions` por URI.
        """
        last = self._last_seen.get(ch)
        if last is None:
            # Primer poll del canal: se SIEMBRA el cursor y no se procesa nada.
            # Procesar la ventana inicial hacía que un bot recién arrancado
            # contestara en ráfaga hasta 50 mensajes viejos del canal — al
            # validar en vivo eso se ve como un bot desbocado. Costo asumido:
            # las menciones de mientras estuvo caído no se responden (las que
            # ya había VISTO y quedaron failed las recupera retry_stuck_mentions
            # por URI; estas nunca se leyeron).
            msgs = self._get(f"/channels/{ch}/messages", limit=1) or []
            if msgs:
                self._last_seen[ch] = str(max(int(m["id"]) for m in msgs))
                log.info("canal %s: cursor sembrado — arranco a escuchar desde ahora", ch)
            return []

        out: list[dict] = []
        for _ in range(self.MAX_CATCHUP_PAGES):
            batch = self._get(f"/channels/{ch}/messages",
                              after=last, limit=self.CATCHUP_PAGE) or []
            if not batch:
                break
            out.extend(batch)
            # Los ids de Discord son snowflakes crecientes: avanzar al máximo del
            # lote es correcto sin importar en qué orden los devuelva la API.
            last = str(max(int(m["id"]) for m in batch))
            self._last_seen[ch] = last
            if len(batch) < self.CATCHUP_PAGE:
                break
        else:
            log.warning(
                "canal %s: más de %d mensajes nuevos en un ciclo — puede haber "
                "menciones sin leer. Bajá POLL_INTERVAL_SECONDS o revisá el canal.",
                ch, self.MAX_CATCHUP_PAGES * self.CATCHUP_PAGE)
        # La API devuelve newest-first: sin este sort, dos mensajes encadenados
        # de la misma persona se contestaban al revés dentro del mismo poll.
        out.sort(key=lambda m: int(m["id"]))
        return out

    # ── contrato Channel ─────────────────────────────────────────────────
    def get_mentions(self) -> list[dict]:
        mentions = []
        for ch in self._channel_ids:
            try:
                msgs = self._new_messages(ch)
            except Exception as e:
                log.error("messages de canal %s falló: %s", ch, e)
                continue
            for msg in msgs or []:
                # Se recuerda TODO el canal, no solo lo dirigido al bot: el
                # contexto de un canal son las charlas ajenas (ver _recordar).
                self._recordar(ch, msg)
                if self._is_for_me(msg):
                    mentions.append(self._to_mention(ch, msg))
        return mentions

    def _recordar(self, ch: str, msg: dict) -> None:
        """Conversación reciente del canal (mismo gesto que WhatsApp). El deque
        acota la memoria; el dedup por id cubre reentregas del paginado."""
        cola = self._recientes.setdefault(ch, deque(maxlen=self.BUFFER_POR_CANAL))
        mid = str(msg.get("id"))
        if cola and any(str(m.get("id")) == mid for m in cola):
            return
        cola.append(msg)

    def mark_all_read(self) -> None:
        pass  # no existe el concepto; dedup por DB

    def _fetch_message(self, channel_id: str, message_id: str, *,
                       strict: bool = False) -> dict | None:
        """`strict` distingue "no existe" (404 → None) de "no pude averiguar"
        (red/5xx → MentionRefetchError). El modo laxo (default) devuelve None
        para todo: es el correcto para armar contexto de hilo best-effort,
        pero NUNCA para decidir si una mención se descarta."""
        try:
            msg = self._get(f"/channels/{channel_id}/messages/{message_id}")
            if msg:
                self._remember_users(msg)
            return msg
        except Exception as e:
            if _http_status(e) == 404:
                return None
            if strict:
                raise MentionRefetchError(f"{type(e).__name__}: {e}") from e
            log.warning("fetch de %s/%s falló: %s", channel_id, message_id, e)
            return None

    def get_thread_info(self, uri: str, cid: str) -> tuple[str, str, str, str]:
        """Contexto = la conversación reciente del canal, no solo la cadena de
        replies (portado de WhatsApp): en Discord la gente contesta sin reply
        formal, así que la cadena sola era casi siempre vacía y el bot
        contestaba sordo. La raíz de la cadena se sigue usando como root para
        agrupar la conversación en la DB; el reply formal no se pierde — se
        marca en la línea del mensaje que lo tiene."""
        channel_id, _, message_id = uri.partition("/")
        leaf = self._fetch_message(channel_id, message_id)
        if leaf is None:
            return "", uri, cid, ""
        # subir por la cadena de replies (message_reference) hasta la raíz
        chain, msg = [], leaf
        for _ in range(self.THREAD_HOPS):
            ref = (msg.get("message_reference") or {}).get("message_id")
            if not ref:
                break
            parent = msg.get("referenced_message") or self._fetch_message(channel_id, str(ref))
            if parent is None:
                break
            chain.append(parent)
            msg = parent
        root = chain[-1] if chain else leaf
        root_uri = f"{channel_id}/{root['id']}"

        ventana = list(self._recientes.get(channel_id, ()))[-self._context_messages:]
        if not any(str(m.get("id")) == message_id for m in ventana):
            ventana.append(leaf)            # el mensaje que se contesta, siempre
        # El referenciado directo entra aunque haya salido de la ventana: es lo
        # más específico que hay sobre qué le están contestando.
        ref_directo = chain[0] if chain else None
        if ref_directo and not any(str(m.get("id")) == str(ref_directo["id"])
                                   for m in ventana):
            ventana.insert(0, ref_directo)

        lines = [self._linea(m) for m in ventana] if self._context_messages else \
                [f"{m['author']['username']}: {self._full_text(m)}"
                 for m in reversed(chain)]
        # Vision solo sobre la hoja: es el mensaje que el bot está por contestar.
        # El resto del contexto queda con la anotación barata (costo acotado).
        return "\n".join(lines), root_uri, str(root["id"]), self._media_note(leaf, describe=True)

    def _linea(self, msg: dict) -> str:
        """Una línea de contexto; si el mensaje es reply formal, lo marca."""
        quien = (msg.get("author") or {}).get("username") or "alguien"
        cita = ""
        ref = msg.get("referenced_message")
        if (msg.get("message_reference") or {}).get("message_id"):
            a_quien = ((ref or {}).get("author") or {}).get("username") or "alguien"
            frag = (self._msg_text(ref) if ref else "")[:40]
            cita = f" [le contesta a {a_quien}" + (f": «{frag}»]" if frag else "]")
        return f"{quien}{cita}: {self._full_text(msg)}"

    def get_mention_by_uri(self, uri: str) -> dict | None:
        channel_id, _, message_id = uri.partition("/")
        msg = self._fetch_message(channel_id, message_id, strict=True)
        if msg is None:
            return None
        context, root_uri, root_cid, _ = self.get_thread_info(uri, str(msg["id"]))
        out = self._to_mention(channel_id, msg)
        out["text"] = self._full_text(msg, describe=True)
        out.update(thread_context=context, thread_root_uri=root_uri,
                   thread_root_cid=root_cid)
        return out

    def reply(self, text: str, parent_uri: str, parent_cid: str,
              root_uri: str, root_cid: str, media_path: str | None = None) -> str:
        channel_id, _, message_id = parent_uri.partition("/")
        payload = {
            "content": strip_fake_media(text)[:2000],
            "message_reference": {"message_id": message_id,
                                  "fail_if_not_exists": False},
        }
        msg = self._post_message(channel_id, payload, media_path)
        return f"{channel_id}/{msg['id']}"

    MAX_POST_LEN = 1990  # 2000 de Discord, con margen

    def post(self, text: str, limit: int | None = None, media_path: str | None = None,
             target: str | None = None) -> str:
        """Sin target → canal principal (el primero); con target (rutinas) → ese canal."""
        text = strip_fake_media(text)
        channel_id = str(target) if target else \
            (self._channel_ids[0] if self._channel_ids else None)
        if not channel_id:
            raise RuntimeError("DISCORD_CHANNEL_IDS vacío: no hay canal donde postear")
        payload = {"content": truncate_post(text, limit or self.MAX_POST_LEN)}
        msg = self._post_message(channel_id, payload, media_path)
        return f"{channel_id}/{msg['id']}"

    def _post_message(self, channel_id: str, payload: dict,
                      media_path: str | None) -> dict:
        path = f"/channels/{channel_id}/messages"
        if media_path:
            p = Path(media_path)
            try:
                mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                files = {"files[0]": (p.name, p.read_bytes(), mime)}
                return self._request("POST", path, files=files,
                                     data={"payload_json": json.dumps(payload)})
            except OSError as e:
                log.warning("upload de media falló (%s): %s — posteo sin media",
                            p.name, e)
        return self._request("POST", path, json=payload)

    def like_post(self, uri: str, cid: str = "") -> bool:
        """"Me gusta" = reacción del bot al mensaje. Discord no tiene like: la
        reacción es el gesto equivalente (y el único que la API le deja a un bot).
        Idempotente: reaccionar dos veces con el mismo emoji no duplica nada."""
        channel_id, _, message_id = uri.partition("/")
        try:
            self._request("PUT", f"/channels/{channel_id}/messages/{message_id}"
                                 f"/reactions/{quote(LIKE_EMOJI)}/@me")
            return True
        except Exception as e:
            log.warning("like_post (Discord) falló para %s: %s", uri, e)
            return False

    def set_bio(self, text: str) -> bool:
        """Actualiza el "About Me" del bot (description de la application):
        único campo tipo bio que la API REST le deja tocar a un bot."""
        try:
            # Tope de Discord para la description: 400. Sin el recorte, una bio
            # larga era 400 Bad Request → False en cada pase de la rutina.
            self._request("PATCH", "/applications/@me",
                          json={"description": truncate_post(text, 400)})
            return True
        except Exception as e:
            log.error("set_bio (Discord): %s", e)
            return False

    def get_profile(self, handle: str):
        u = self._users.get(handle.lstrip("@").lower())
        if u is None:
            return None  # sin lookup global por username en la API de bots
        return SimpleNamespace(
            did=str(u["id"]),
            display_name=u.get("global_name") or None,
            description="",  # Discord no expone bios a los bots
        )

    def resolve_did(self, handle: str) -> str | None:
        u = self._users.get(handle.lstrip("@").lower())
        return str(u["id"]) if u else None

    # Discord no tiene bloqueo de usuarios para bots (moderación = permisos del
    # server). El flag hace que `block_me` NI SE REGISTRE en este canal: una
    # tool que promete lo que no puede es peor que no tenerla.
    CAN_BLOCK = False

    def block_user(self, handle: str) -> bool:
        log.warning("block_user: Discord no tiene bloqueo de usuarios para bots "
                    "(moderación = permisos del server); @%s queda sin bloquear", handle)
        return False

    def set_media_describer(self, fn) -> None:
        """Vision sobre los attachments: el describidor se llama con la URL del CDN
        (`make_media_describer` acepta post_view de Bluesky o URL suelta)."""
        self._media_describer = fn

    # ── fuentes de feed (loop proactivo) ─────────────────────────────────
    def get_feed_posts(self, source_type: str, identifier: str | None,
                       since: datetime | None, limit: int = 50) -> list[dict]:
        """`channel` → mensajes recientes de un canal por id (o del principal)."""
        if source_type != "channel":
            log.warning("fuente de feed no soportada en Discord: %s", source_type)
            return []
        channel_id = str(identifier) if identifier else \
            (self._channel_ids[0] if self._channel_ids else None)
        if not channel_id:
            return []
        try:
            msgs = self._get(f"/channels/{channel_id}/messages",
                             limit=min(limit, 100))
        except Exception as e:
            log.error("feed del canal %s falló: %s", channel_id, e)
            return []
        posts = []
        for msg in msgs or []:  # vienen en orden reverso-cronológico
            created = msg.get("timestamp")
            # El .replace("Z", ...) es el mismo fallback que Mastodon: Discord
            # hoy manda +00:00, pero fromisoformat de Python <3.11 no come "Z".
            created_dt = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                          if created else None)
            if created_dt and created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            if since and created_dt and created_dt <= since:
                break
            if (msg.get("author") or {}).get("bot"):
                continue
            text = self._msg_text(msg)
            if not text:
                continue
            self._remember_users(msg)
            ref = (msg.get("message_reference") or {}).get("message_id")
            posts.append({
                "handle"     : msg["author"]["username"],
                "text"       : text,
                "uri"        : f"{channel_id}/{msg['id']}",
                "indexed_at" : created or "",
                "reply_to"   : f"{channel_id}/{ref}" if ref else None,
            })
        log.info("canal %s: %d posts", channel_id, len(posts))
        return posts

    def get_list_members(self, list_id: str) -> list[str]:
        log.warning("get_list_members: Discord no tiene listas (usar USER_GROUPS)")
        return []

    def get_follows(self) -> list[str]:
        log.warning("get_follows: Discord no tiene follows (usar USER_GROUPS)")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# WhatsApp (T41)
# ═══════════════════════════════════════════════════════════════════════════
# WhatsApp no tiene API usable para un bot comunitario (la Groups API oficial
# topea en 8 participantes), así que se entra por el mismo camino que WhatsApp
# Web: el número se agrega como DISPOSITIVO VINCULADO. Eso lo hace una librería
# que no es Python —Baileys (Node) o whatsmeow (Go)—, así que a diferencia de
# Mastodon y Discord acá hay un proceso aparte: el **bridge**.
#
# Este módulo NO habla el protocolo de WhatsApp: habla HTTP contra el bridge,
# que corre en localhost. Fijar ese contrato acá es lo que permite escribir y
# testear el canal entero contra un bridge falso, sin vincular un número y sin
# haber elegido todavía el motor.
#
# ── Contrato del bridge ────────────────────────────────────────────────────
#   GET  /status              → {"connected": bool, "qr": str|null,
#                                "me": {"id": "<jid>", "name": str}}
#   GET  /messages?after=<c>  → {"cursor": str, "messages": [msg, ...]}
#   GET  /messages/<id>       → msg (para releer una cita)
#   POST /send                → {"chat_id","text","reply_to"?,"media_path"?}
#                               → {"id": "<message_id>"}
#   POST /profile             → {"status": str}     (opcional)
#
#   msg = {"id","chat_id","chat_name","author_id","author_name","text",
#          "quoted_id": str|null, "quoted_author_id": str|null,
#          "mentions": [jid,...], "media": [{"url"?,"path"?,"mime","filename"}],
#          "from_me": bool, "ts": <epoch>}
#
# El cursor lo define el bridge (es su cola); acá solo se guarda y se devuelve.

def wa_handle(valor: str | None) -> str:
    """El "handle" de WhatsApp es el teléfono, en dígitos y nada más.

    Acepta las tres formas en que el mismo número llega: el JID del bridge
    (`5491111111111@s.whatsapp.net`, o `…@lid` si la otra punta tiene el modo
    privado), lo que tipea el admin (`+54 9 11 1111-1111`) y el número pelado.
    Devuelve siempre lo mismo, que es lo que el canal pone en `author_handle` —
    así ADMIN_HANDLE y USER_GROUPS se escriben con el número y matchean.

    Lo que no parece un número (un `feed:algo`, un handle de otra red que quedó
    de arrastre) vuelve tal cual: normalizarlo sería inventar un match.
    """
    s = str(valor or "").strip()
    if not s:
        return s
    if "@" in s:
        # Es un JID: puede traer sufijo de device (`549...:12@s.whatsapp.net`).
        # El ":" del device se saca ANTES del chequeo de refs — si no, el JID
        # entero volvía crudo y no matcheaba ADMIN_HANDLE ni USER_GROUPS.
        user = s.split("@", 1)[0].split(":", 1)[0]
        digitos = re.sub(r"\D", "", user)
        return digitos or s
    if ":" in s:
        return s  # ref tipo `feed:algo`, no un número
    digitos = re.sub(r"\D", "", s)
    return digitos or s


class WhatsAppChannel:
    """Canal WhatsApp a través de un bridge local (T41).

    Modelo: el bot escucha una lista fija de chats (`chat_ids`, settings
    `WHATSAPP_CHAT_IDS`) — normalmente UN grupo. "Mención" = mensaje que
    @menciona al bot o que cita un mensaje del bot.

    Ese allowlist no es cosmético: es lo que permite compartir el número con
    otro bot ya vinculado. Todos los dispositivos vinculados reciben TODOS los
    mensajes, así que la partición la hace cada cliente decidiendo en qué chats
    actúa — el protocolo no la ofrece.

    Diferencias absorbidas acá (el grafo no se entera):
    - uri = "chat_id/message_id"; cid = message_id (igual que Discord).
    - Hilo: WhatsApp solo tiene citas de un nivel, no hilos. Se reconstruye
      subiendo por `quoted_id` mientras el bridge pueda resolverlo.
    - Contexto: en un grupo la gente contesta sin citar, así que la cadena de
      citas sola deja al bot sordo. El contexto es la CONVERSACIÓN RECIENTE del
      chat (`context_messages`), que es lo que ve un humano al entrar.
    - Feed: no hay timeline, pero el pase de feed sí aplica — la fuente es la
      conversación del grupo (tipo `chat`). Leer cada tanto de qué se habla y
      aprender de la gente es la misma idea que en Bluesky.
    - `author_handle` es el teléfono, no un @handle (ver `wa_handle`).
    - No aplican (decisión del admin, 2026-07-31): `block_user` —en un grupo
      bloquear no saca a nadie— y el perfil de terceros, que es una frase de
      estado y no dice quién es nadie. `get_profile` da el nombre visible y nada
      más; lo que el bot sabe de alguien sale de su memoria.
    """

    THREAD_HOPS = 12       # tope al subir por citas
    POLL_LIMIT = 100       # mensajes por request al bridge
    BUFFER_POR_CHAT = 300  # cuánto se recuerda por chat (techo de memoria)

    def __init__(self, bridge_url: str, chat_ids: list[str], http=None,
                 context_messages: int = 20):
        if http is None:
            import httpx  # lazy: simetría con los otros canales
            http = httpx.Client(base_url=bridge_url.rstrip("/"), timeout=30)
        self._http = http
        self._chat_ids = [str(c) for c in chat_ids]
        self._cursor: str | None = None
        self._media_describer = None
        self._context_messages = max(0, int(context_messages))
        self._msgs: dict[str, dict] = {}      # id → msg visto (citas y re-lectura)
        # Conversación reciente por chat, en orden. Solo de los chats del
        # allowlist: los personales del número no se guardan ni en memoria.
        self._recientes: dict[str, deque] = {}
        estado = self._get("/status") or {}
        if not estado.get("connected"):
            raise SystemExit(
                "el bridge de WhatsApp está levantado pero el número no está "
                "vinculado. Escaneá el QR desde WhatsApp → Ajustes → Dispositivos "
                "vinculados (el bridge lo publica en /status)."
            )
        me = estado.get("me") or {}
        self._me_id = str(me.get("id") or "")
        # El bot tiene DOS identidades y las dos aparecen: su número y su LID
        # (los grupos nuevos direccionan por LID). Una cita a un mensaje suyo
        # viene firmada con la que use ese grupo, así que hay que aceptar las
        # dos o el bot no se entera de que le están contestando.
        self._me_ids = {wa_handle(v) for v in (me.get("id"), me.get("lid")) if v}
        self.handle = me.get("name") or self._me_id
        # Versión sin espacios para escribir @menciones en los textos: con el
        # display name crudo ("Botata Rancher"), el parser de comandos comía
        # solo "@Botata" y el resto del nombre ensuciaba el comando — /stop,
        # /sleep y compañía quedaban mudos.
        self._handle_token = re.sub(r"\s+", "", self.handle)
        log.info("WhatsApp vinculado como %s (escuchando %d chat(s))",
                 self.handle, len(self._chat_ids))

    # ── HTTP contra el bridge ────────────────────────────────────────────
    def _request(self, method: str, path: str, **kw):
        resp = self._http.request(method, path, **kw)
        resp.raise_for_status()
        return resp.json() if resp.status_code != 204 else None

    def _get(self, path: str, **params):
        return self._request("GET", path, params=params or None)

    # ── mapeo mensaje → dict del grafo ───────────────────────────────────
    def _msg_text(self, msg: dict) -> str:
        """El texto con las menciones legibles: el bridge las manda como JIDs."""
        text = msg.get("text") or ""
        for jid in msg.get("mentions") or []:
            text = text.replace(f"@{jid}", f"@{str(jid).split('@')[0]}")
        # Arrobarlo deja en el texto el número CRUDO con el que ese grupo lo
        # direcciona —el LID, que no es su teléfono ni su nombre—, así que el
        # bot leía "@104913921159305 sobre esto qué opinás?" y no reconocía que
        # ese número es él. Se escribe el token SIN espacios (ver __init__).
        for propio in self._me_ids:
            text = text.replace(f"@{propio}", f"@{self._handle_token}")
        return text.strip()

    def _media_note(self, msg: dict, *, describe: bool = False) -> str:
        """Igual que en Discord: se describe SOLO el mensaje que se va a
        contestar, nunca en lectura masiva — sería una llamada de vision por
        imagen y por mensaje leído."""
        pieces = []
        for att in msg.get("media") or []:
            kind = str(att.get("mime") or "file").split("/")[0]
            fuente = att.get("url") or att.get("path")
            desc = ""
            if describe and kind == "image" and self._media_describer and fuente:
                try:
                    desc = self._media_describer(fuente) or ""
                except Exception:
                    log.debug("vision sobre media de WhatsApp falló", exc_info=True)
            if desc:
                pieces.append(f"[{kind}: {desc}]")
            else:
                nombre = att.get("filename") or ""
                pieces.append(f"[{kind}: {nombre}]" if nombre else f"[{kind}]")
        return " ".join(pieces)

    def _full_text(self, msg: dict, *, describe: bool = False) -> str:
        text = self._msg_text(msg)
        media = self._media_note(msg, describe=describe)
        return f"{text} {media}".strip() if media else text

    def _is_for_me(self, msg: dict) -> bool:
        if msg.get("from_me") or wa_handle(msg.get("author_id")) in self._me_ids:
            return False
        if self._me_ids & {wa_handle(j) for j in msg.get("mentions") or []}:
            return True
        if self._nombrado_en_texto(msg):
            return True
        return bool(msg.get("quoted_id")
                    and wa_handle(msg.get("quoted_author_id")) in self._me_ids)

    def _nombrado_en_texto(self, msg: dict) -> bool:
        """Arrobado a mano, sin mención de verdad.

        WhatsApp solo genera una mención si el que escribe elige al bot del
        listado que aparece al tipear `@`. Escribir `@botata` a mano no genera
        nada — y desde afuera se ve exactamente igual, así que el bot quedaba
        mudo sin motivo visible. Se acepta el nombre y el número; el allowlist
        de chats sigue siendo el que decide dónde vale."""
        texto = (msg.get("text") or "").lower()
        if "@" not in texto:
            return False
        candidatos = {c.lower()
                      for c in (self.handle, self._handle_token, *self._me_ids) if c}
        for c in candidatos:
            for hit in re.finditer(rf"@{re.escape(c)}\b", texto):
                # El @ tiene que abrir palabra: si no, "juan@botata.com" sería
                # un arrobado y el bot contestaría un mail ajeno.
                if hit.start() == 0 or texto[hit.start() - 1].isspace():
                    return True
        return False

    def _to_mention(self, msg: dict) -> dict:
        return {
            "uri"           : f"{msg['chat_id']}/{msg['id']}",
            "cid"           : str(msg["id"]),
            # El teléfono, no el JID: es el id que el admin puede escribir a
            # mano en ADMIN_HANDLE y en USER_GROUPS (ver wa_handle).
            "author_handle" : wa_handle(msg.get("author_id")),
            "text"          : self._msg_text(msg),
        }

    # ── lectura ──────────────────────────────────────────────────────────
    def get_mentions(self) -> list[dict]:
        try:
            data = self._get("/messages", after=self._cursor or "",
                             limit=self.POLL_LIMIT) or {}
        except Exception as e:
            log.error("WhatsApp: el bridge no respondió: %s", e)
            return []
        if data.get("cursor"):
            self._cursor = str(data["cursor"])
        menciones = []
        for msg in data.get("messages") or []:
            # El allowlist: acá vive la partición con cualquier otro cliente
            # vinculado al mismo número. Va PRIMERO: lo que no es de un chat
            # del bot no se guarda ni en memoria (son chats personales).
            chat = str(msg.get("chat_id"))
            if chat not in self._chat_ids:
                continue
            self._guardar_msg(msg)
            self._recordar(chat, msg)
            if self._is_for_me(msg):
                menciones.append(self._to_mention(msg))
        return menciones

    # Techo del cache id→msg. `_recientes` topea solo por deque, pero `_msgs`
    # crecía sin límite: en un grupo activo era un leak lento pero seguro en un
    # proceso que corre semanas. Los dicts preservan orden de inserción, así
    # que sacar el primero = sacar el más viejo.
    MSGS_MAX = 2000

    def _guardar_msg(self, msg: dict) -> None:
        self._msgs[str(msg.get("id"))] = msg
        while len(self._msgs) > self.MSGS_MAX:
            del self._msgs[next(iter(self._msgs))]

    def _recordar(self, chat: str, msg: dict) -> None:
        """Guarda el mensaje en la conversación reciente del chat.

        Se guarda TODO lo del chat, no solo lo que le hablan al bot: el contexto
        de un grupo son las charlas ajenas. El deque acota la memoria, y el
        dedup por id evita que un mensaje releído aparezca dos veces (el bridge
        puede reentregar si el cursor se reinicia)."""
        cola = self._recientes.setdefault(chat, deque(maxlen=self.BUFFER_POR_CHAT))
        mid = str(msg.get("id"))
        if cola and any(str(m.get("id")) == mid for m in cola):
            return
        cola.append(msg)

    def mark_all_read(self) -> None:
        pass  # no existe el concepto; dedup por DB

    def _fetch_message(self, message_id: str, *, strict: bool = False) -> dict | None:
        """`strict`: 404 → None (no existe), red/bridge caído →
        MentionRefetchError. Igual que en Discord: el modo laxo es para
        contexto best-effort, no para decidir descartes."""
        if message_id in self._msgs:
            return self._msgs[message_id]
        try:
            msg = self._get(f"/messages/{message_id}")
        except Exception as e:
            if _http_status(e) == 404:
                return None
            if strict:
                raise MentionRefetchError(f"{type(e).__name__}: {e}") from e
            log.debug("WhatsApp: no pude releer el mensaje %s", message_id, exc_info=True)
            return None
        if msg:
            self._guardar_msg(msg)
        return msg or None

    def get_thread_info(self, uri: str, cid: str) -> tuple[str, str, str, str]:
        """Contexto = la conversación reciente del chat, no el hilo.

        WhatsApp no tiene hilos: hay citas de un nivel, y en un grupo la gente
        contesta sin citar. Reconstruir "el hilo" daba casi siempre una sola
        línea —el mensaje mismo— y el bot terminaba contestando sin saber de qué
        se estaba hablando. Lo que ve un humano cuando entra al grupo son los
        últimos mensajes, así que eso es lo que se le pasa.

        La cita no se pierde: se marca en la línea que la tiene, y si el mensaje
        citado quedó fuera de la ventana se agrega igual (es lo más específico
        que hay sobre qué le están contestando)."""
        chat_id, _, message_id = uri.partition("/")
        actual = self._fetch_message(message_id)

        # Raíz = la cabeza de la cadena de citas (lo que el motor usa para
        # agrupar la conversación en su DB). Sigue valiendo aunque el contexto
        # que se le pase al LLM sea la ventana.
        cadena: list[dict] = []
        cursor, saltos = actual, 0
        while cursor and saltos < self.THREAD_HOPS:
            cadena.append(cursor)
            quoted = cursor.get("quoted_id")
            if not quoted:
                break
            cursor = self._fetch_message(str(quoted))
            saltos += 1
        raiz = cadena[-1] if cadena else None

        ventana = list(self._recientes.get(chat_id, ()))[-self._context_messages:]
        if actual and not any(str(m.get("id")) == message_id for m in ventana):
            ventana.append(actual)          # el mensaje que se contesta, siempre
        citado_id = str((actual or {}).get("quoted_id") or "")
        citado = self._fetch_message(citado_id) if citado_id else None
        if citado and not any(str(m.get("id")) == citado_id for m in ventana):
            ventana.insert(0, citado)

        lineas = [self._linea(m) for m in ventana] if self._context_messages else []
        root_uri = f"{raiz['chat_id']}/{raiz['id']}" if raiz else uri
        root_cid = str(raiz["id"]) if raiz else cid
        return "\n".join(lineas), root_uri, root_cid, self._media_a_mirar(actual, citado)

    def _media_a_mirar(self, actual: dict | None, citado: dict | None) -> str:
        """Qué imágenes MIRA de verdad (vision), no solo anota.

        Dos mensajes y no más: el que le hablan y el que ese mensaje cita. En
        WhatsApp el gesto natural es citar una foto y preguntar por ella —el
        mensaje que menciona al bot es solo texto, la imagen está en el citado—,
        así que describir únicamente la hoja dejaba al bot diciendo que no puede
        ver imágenes con la imagen ahí al lado. El resto de la ventana queda con
        la anotación barata: el costo es una llamada de vision por mensaje.
        """
        partes = []
        if actual:
            partes.append(self._media_note(actual, describe=True))
        if citado is not None:
            nota = self._media_note(citado, describe=True)
            if nota:
                partes.append(f"lo que cita: {nota}")
        return " ".join(p for p in partes if p).strip()

    def _linea(self, msg: dict) -> str:
        """Una línea de conversación. La cita se anota inline: sin eso, en un
        grupo no se entiende a quién le está contestando cada uno."""
        quien = msg.get("author_name") or wa_handle(msg.get("author_id")) or "alguien"
        if msg.get("from_me"):
            quien = self.handle
        cita = ""
        if msg.get("quoted_id"):
            citado = self._msgs.get(str(msg["quoted_id"]))
            a_quien = (citado or {}).get("author_name") or "alguien"
            frag = (self._msg_text(citado) if citado else "")[:40]
            cita = f" [le contesta a {a_quien}" + (f": «{frag}»]" if frag else "]")
        return f"{quien}{cita}: {self._full_text(msg)}"

    def get_mention_by_uri(self, uri: str) -> dict | None:
        _, _, message_id = uri.partition("/")
        msg = self._fetch_message(message_id, strict=True)
        if msg is None:
            return None
        context, root_uri, root_cid, _ = self.get_thread_info(uri, str(msg["id"]))
        out = self._to_mention(msg)
        out["text"] = self._full_text(msg, describe=True)
        out.update(thread_context=context, thread_root_uri=root_uri,
                   thread_root_cid=root_cid)
        return out

    # ── escritura ────────────────────────────────────────────────────────
    def _send(self, chat_id: str, text: str, *, reply_to: str | None = None,
              media_path: str | None = None) -> str:
        payload: dict = {"chat_id": str(chat_id), "text": text}
        if reply_to:
            payload["reply_to"] = str(reply_to)
        if media_path:
            payload["media_path"] = str(media_path)
        out = self._request("POST", "/send", json=payload) or {}
        if not out.get("id"):
            # Sin id la URI queda "{chat}/" — una key degenerada que después
            # envenena el dedup y el refetch en la DB. Mejor explotar acá: el
            # caller ya trata el envío fallido (la mención queda 'failed').
            raise RuntimeError(f"el bridge respondió sin id de mensaje: {out!r}")
        return f"{chat_id}/{out['id']}"

    def reply(self, text: str, parent_uri: str, parent_cid: str,
              root_uri: str, root_cid: str, media_path: str | None = None) -> str:
        chat_id, _, message_id = parent_uri.partition("/")
        return self._send(chat_id, strip_fake_media(text),
                          reply_to=message_id, media_path=media_path)

    # WhatsApp banca mucho más, pero un bot que manda biblias a un grupo es
    # peor que uno que trunca: tope generoso y a otra cosa.
    MAX_POST_LEN = 4000

    def post(self, text: str, limit: int | None = None, media_path: str | None = None,
             target: str | None = None) -> str:
        """Sin target → el primer chat; con target (rutinas) → ese chat."""
        chat_id = str(target) if target else (self._chat_ids[0] if self._chat_ids else None)
        if not chat_id:
            raise RuntimeError("WHATSAPP_CHAT_IDS vacío: no hay chat donde postear")
        return self._send(chat_id,
                          truncate_post(strip_fake_media(text), limit or self.MAX_POST_LEN),
                          media_path=media_path)

    def like_post(self, uri: str, cid: str = "") -> bool:
        """"Me gusta" = reacción al mensaje (WhatsApp no tiene otra cosa).
        El bridge necesita también el autor del mensaje: una reacción se firma
        contra (chat, autor, id), no solo contra el id."""
        chat_id, _, message_id = uri.partition("/")
        original = self._msgs.get(message_id) or {}
        try:
            self._request("POST", "/react", json={
                "chat_id"  : chat_id,
                "message_id": message_id,
                "author_id": str(original.get("author_id") or ""),
                "emoji"    : LIKE_EMOJI,
            })
            return True
        except Exception as e:
            log.warning("like_post (WhatsApp) falló para %s: %s "
                        "(¿el bridge es viejo y no tiene /react?)", uri, e)
            return False

    # ── lo que WhatsApp no tiene ─────────────────────────────────────────
    def set_bio(self, text: str) -> bool:
        try:
            self._request("POST", "/profile", json={"status": text[:139]})
            return True
        except Exception as e:
            log.warning("set_bio en WhatsApp falló (¿el bridge no lo soporta?): %s", e)
            return False

    def get_profile(self, handle: str):
        """El nombre que la persona muestra en el grupo, y nada más.

        No hay perfil que traer: el "info" de WhatsApp es una frase de estado
        que no dice quién es nadie (decisión del admin, 2026-07-31). Lo que el
        bot sabe de alguien lo sabe por su memoria, no por su perfil."""
        num = wa_handle(handle)
        visto = next((m for m in self._msgs.values()
                      if wa_handle(m.get("author_id")) == num), None)
        nombre = (visto or {}).get("author_name") or num
        # `did` va aunque no exista el concepto: el motor lo lee para cachearlo
        # al conocer a alguien, y sin el campo eso explotaba con AttributeError
        # en CADA mensaje de esa persona (se tragaba en el except, así que la
        # fila nunca se completaba y el intento se repetía para siempre). Acá el
        # id estable es el teléfono.
        return SimpleNamespace(handle=num, did=num, display_name=nombre, description="")

    def resolve_did(self, handle: str) -> str | None:
        return wa_handle(handle) or None   # el teléfono ya ES el id estable

    # Igual que Discord: sin bloqueo útil → block_me no se registra acá.
    CAN_BLOCK = False

    def block_user(self, handle: str) -> bool:
        """No aplica (decisión del admin, 2026-07-31): en un grupo bloquear no
        hace nada útil — el bloqueado sigue en el grupo y sus mensajes siguen
        llegando. Lo que corresponde ahí lo hace un admin del grupo, no el bot.
        Devuelve False para que quien la pida se entere de que no pasó nada."""
        log.info("block_user: en WhatsApp no aplica (bloquear no saca a nadie "
                 "de un grupo) — pedido sobre %s ignorado", wa_handle(handle))
        return False

    def set_media_describer(self, fn) -> None:
        self._media_describer = fn

    def get_feed_posts(self, source_type: str, identifier: str | None,
                       since: datetime | None = None, limit: int = 40) -> list[dict]:
        """`chat` → la conversación reciente del grupo, como si fuera un feed.

        No hay timeline que pedir: lo que hay es lo que el bot fue viendo pasar
        (`_recientes`). Alcanza para lo que el pase de feed hace de verdad —
        leer cada tanto de qué se está hablando y aprender de la gente — que es
        la misma idea que en Bluesky aunque la fuente sea otra.

        Límite honesto: no hay historial anterior a este proceso. Un pase que
        corre cada 6 horas ve las últimas ~300 charlas, no el pasado del grupo.
        """
        if source_type != "chat":
            log.warning("fuente de feed no soportada en WhatsApp: %s (usar 'chat')",
                        source_type)
            return []
        chat_id = str(identifier) if identifier else (
            self._chat_ids[0] if self._chat_ids else "")
        if chat_id not in self._chat_ids:
            # Mismo criterio que el poll: fuera del allowlist no se lee nada.
            log.warning("feed de WhatsApp: '%s' no está en WHATSAPP_CHAT_IDS", chat_id)
            return []
        corte = since.timestamp() if since else None
        posts = []
        for msg in list(self._recientes.get(chat_id, ()))[-limit:]:
            if msg.get("from_me"):
                continue                      # lo suyo no es "lo que dice el grupo"
            if corte is not None and float(msg.get("ts") or 0) <= corte:
                continue
            texto = self._full_text(msg)
            if not texto:
                continue
            quoted = msg.get("quoted_id")
            posts.append({
                # El teléfono, igual que en las menciones: si acá fuera el
                # nombre visible, el bot aprendería de "Fulano" y contestaría a
                # "5491111111111" — dos personas distintas para su memoria.
                "handle"    : wa_handle(msg.get("author_id")),
                "text"      : texto,
                "uri"       : f"{chat_id}/{msg.get('id')}",
                "indexed_at": datetime.fromtimestamp(
                    float(msg.get("ts") or 0), tz=timezone.utc).isoformat(),
                "reply_to"  : f"{chat_id}/{quoted}" if quoted else None,
            })
        log.info("chat %s: %d mensajes para el pase de feed", chat_id, len(posts))
        return posts

    def get_list_members(self, list_id: str) -> list[str]:
        return []

    def get_follows(self) -> list[str]:
        log.warning("get_follows: WhatsApp no tiene follows (usar USER_GROUPS)")
        return []
