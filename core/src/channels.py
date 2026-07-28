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
    .post(text, limit=295, media_path=None, target=None) -> uri
                                              # target: destino opcional (rutinas con channel) —
                                              # Discord = id de canal; Bluesky y
                                              # Mastodon lo ignoran (un solo timeline)
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
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger("botata.channels")

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


def strip_fake_media(text: str) -> str:
    """Saca las anotaciones de media que el LLM haya escrito en el texto.

    Las anotaciones son ENTRADA (lo que el bot ve), nunca salida. Adjuntar de
    verdad es cosa del pipeline (`media_path`), no del texto.
    """
    limpio = _MEDIA_FAKE_RE.sub("", text or "")
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
    return cut[: cut.rfind(" ")] if " " in cut else cut


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
            log.warning("get_mention_by_uri: fetch falló para %s: %s", uri, e)
            return None
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
        text = strip_fake_media(text)[:490]
        try:
            parent = self._api.status(parent_uri)
            author = parent["account"]["acct"]
            if f"@{author}".lower() not in text.lower():
                text = f"@{author} {text}"
        except Exception:
            log.debug("no pude traer el parent %s para mencionar al autor", parent_uri)
        media_ids = self._upload_media(media_path) if media_path else None
        status = self._api.status_post(text, in_reply_to_id=parent_uri,
                                       media_ids=media_ids)
        return str(status["id"])

    def post(self, text: str, limit: int = 295, media_path: str | None = None,
             target: str | None = None) -> str:
        if target:
            log.debug("post: Mastodon no tiene canales — target %r ignorado", target)
        text = truncate_post(strip_fake_media(text), limit)
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
        log.debug("set_media_describer: MastodonChannel usa alt-text (vision pendiente)")

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
    HISTORY_LIMIT = 50        # primer poll de un canal (sin cursor): cuánto mirar atrás
    CATCHUP_PAGE = 100        # máximo por request que acepta la API
    MAX_CATCHUP_PAGES = 5     # tope duro por canal y ciclo (500 msgs)
    THREAD_HOPS = 12          # tope de la cadena de replies al reconstruir contexto

    def __init__(self, bot_token: str, channel_ids: list[str], http=None):
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
        if last is None:                      # primer poll del canal: ventana corta
            msgs = self._get(f"/channels/{ch}/messages", limit=self.HISTORY_LIMIT) or []
            if msgs:
                self._last_seen[ch] = str(max(int(m["id"]) for m in msgs))
            return msgs

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
                if self._is_for_me(msg):
                    mentions.append(self._to_mention(ch, msg))
        return mentions

    def mark_all_read(self) -> None:
        pass  # no existe el concepto; dedup por DB

    def _fetch_message(self, channel_id: str, message_id: str) -> dict | None:
        try:
            msg = self._get(f"/channels/{channel_id}/messages/{message_id}")
            if msg:
                self._remember_users(msg)
            return msg
        except Exception as e:
            log.warning("fetch de %s/%s falló: %s", channel_id, message_id, e)
            return None

    def get_thread_info(self, uri: str, cid: str) -> tuple[str, str, str, str]:
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
        chain.reverse()  # raíz primero
        lines = [f"{m['author']['username']}: {self._full_text(m)}" for m in chain]
        root = chain[0] if chain else leaf
        root_uri = f"{channel_id}/{root['id']}"
        # Vision solo sobre la hoja: es el mensaje que el bot está por contestar.
        # La cadena de contexto queda con la anotación barata (costo acotado).
        return "\n".join(lines), root_uri, str(root["id"]), self._media_note(leaf, describe=True)

    def get_mention_by_uri(self, uri: str) -> dict | None:
        channel_id, _, message_id = uri.partition("/")
        msg = self._fetch_message(channel_id, message_id)
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

    def post(self, text: str, limit: int = 295, media_path: str | None = None,
             target: str | None = None) -> str:
        """Sin target → canal principal (el primero); con target (rutinas) → ese canal."""
        text = strip_fake_media(text)
        channel_id = str(target) if target else \
            (self._channel_ids[0] if self._channel_ids else None)
        if not channel_id:
            raise RuntimeError("DISCORD_CHANNEL_IDS vacío: no hay canal donde postear")
        payload = {"content": truncate_post(text, limit)}
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

    def set_bio(self, text: str) -> bool:
        """Actualiza el "About Me" del bot (description de la application):
        único campo tipo bio que la API REST le deja tocar a un bot."""
        try:
            self._request("PATCH", "/applications/@me", json={"description": text})
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
            created_dt = datetime.fromisoformat(created) if created else None
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
