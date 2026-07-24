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
    .post(text, limit=295, media_path=None) -> uri
    .get_profile(handle) -> obj(.did, .display_name, .description) | None
    .block_user(handle) -> bool
    .set_media_describer(fn) -> None          # vision inyectada (puede ignorarse)
    .get_feed_posts(source_type, identifier, since, limit) -> list[dict]
                                              # {handle, text, uri, indexed_at, reply_to}
    .get_list_members(uri) -> list[str] · .get_follows() -> list[str]

Identificadores: cada canal usa los suyos (Bluesky: at:// URIs + cid; Mastodon:
ids de status). El resto del sistema los trata como strings opacos — la DB
(`replied_posts`, `bot_posts`) ya es agnóstica. En Mastodon `cid` == id del
status (no existe el concepto; se duplica para no tocar el grafo).

MastodonChannel usa Mastodon.py (import lazy: los deploys Bluesky no lo pagan).
"""
from __future__ import annotations

import logging
import re
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
        text = text[:490]
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

    def post(self, text: str, limit: int = 295, media_path: str | None = None) -> str:
        text = truncate_post(text, limit)
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
