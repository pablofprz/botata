"""
butterbot.py — Phase 1 (LangGraph mention flow) + Feed reader

Modes:
  --mode admin_only   only responds to ADMIN_HANDLE (default)
  --mode open         responds to any mention

Usage:
  python butterbot.py --mode admin_only
  python butterbot.py --mode open
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from atproto import Client
from atproto.exceptions import NetworkError as BskyNetworkError, RequestException as BskyRequestException
from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from pydantic import AliasChoices, BaseModel, Field
from typing_extensions import TypedDict

import db as dbmod  # módulo de persistencia local (la var `db` es la conexión sqlite)
import clearsky as clearsky_mod  # proxy de la API pública de ClearSky ("quién me bloquea")
from tools import ToolRegistry, ToolContext, ToolResult, Scope  # framework de tools

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("butterbot")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).parent
CONFIG_DIR  = BASE_DIR / "config"
CONTEXT_DIR = BASE_DIR / "context"
PROMPTS_DIR = BASE_DIR / "prompts"
POSTED_DIR  = BASE_DIR / "posted"
FEEDS_DIR   = CONTEXT_DIR / "feeds"

POSTED_DIR.mkdir(exist_ok=True)
FEEDS_DIR.mkdir(exist_ok=True)

DB_PATH       = POSTED_DIR  / "butterbot.db"
SETTINGS_PATH = CONFIG_DIR  / "settings.json"
MEMORY_PATH   = CONTEXT_DIR / "MEMORY.md"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(BASE_DIR / ".env")


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_text(path: Path, default: str = "") -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return default


settings = load_json(SETTINGS_PATH)

BSKY_HANDLE        : str  = settings["BOT_HANDLE"]
BSKY_PASSWORD      : str  = os.environ["BSKY_PASSWORD"]
OPENROUTER_API_KEY : str  = os.environ["OPENROUTER_API_KEY"]
ADMIN_HANDLE       : str  = settings["ADMIN_HANDLE"]
REASONING_MODEL    : str  = settings.get("REASONING_MODEL", "deepseek/deepseek-r1")
LITE_MODEL         : str  = settings.get("LITE_MODEL") or REASONING_MODEL
IMAGE_MODEL        : str  = settings.get("IMAGE_MODEL", "google/gemini-2.5-flash")
OPENAI_ENDPOINT    : str  = settings.get("OPENAI_ENDPOINT", "https://openrouter.ai/api/v1")
POLL_INTERVAL      : int  = settings.get("POLL_INTERVAL_SECONDS", 60)
FEEDS_CONFIG       : list = settings.get("FEEDS", [])
TOOLS_CONFIG       : dict = settings.get("TOOLS", {})

# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    """Conexión a butterbot.db con sqlite-vec cargado y esquema completo (delegado a dbmod)."""
    return dbmod.init_db(DB_PATH)


def has_replied(conn: sqlite3.Connection, uri: str) -> bool:
    """
    True = skip this post.
    'replied', 'pending', 'ignored' → skip.
    'failed' → retry.
    missing  → process.
    """
    row = conn.execute(
        "SELECT status FROM replied_posts WHERE uri = ?", (uri,)
    ).fetchone()
    if row is None:
        return False
    return row[0] in ("replied", "pending", "ignored")


def mark_pending(conn: sqlite3.Connection, uri: str, cid: str, author: str, mode: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO replied_posts (uri, cid, author, status, replied_at, mode) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (uri, cid, author, datetime.now(timezone.utc).isoformat(), mode),
    )
    conn.commit()


def update_status(conn: sqlite3.Connection, uri: str, status: str) -> None:
    conn.execute("UPDATE replied_posts SET status = ? WHERE uri = ?", (status, uri))
    conn.commit()


def log_bot_post(
    conn: sqlite3.Connection,
    uri: str,
    in_reply_to: str | None,
    reply_to_handle: str | None,
    text: str,
) -> None:
    """Registra un post publicado por el bot en bot_posts (log de salida)."""
    conn.execute(
        "INSERT OR IGNORE INTO bot_posts (uri, in_reply_to, reply_to_handle, text) "
        "VALUES (?, ?, ?, ?)",
        (uri, in_reply_to, reply_to_handle, text),
    )
    conn.commit()


def get_feed_last_run(conn: sqlite3.Connection, feed_name: str) -> str | None:
    """Returns the ISO timestamp of the last time this feed was processed, or None."""
    row = conn.execute(
        "SELECT last_run FROM feed_cursors WHERE feed_name = ?", (feed_name,)
    ).fetchone()
    return row[0] if row else None


def save_feed_last_run(conn: sqlite3.Connection, feed_name: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO feed_cursors (feed_name, last_run) VALUES (?, ?)",
        (feed_name, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Profile index
# ---------------------------------------------------------------------------

# Pydantic models
# ---------------------------------------------------------------------------

class MentionClassification(BaseModel):
    model_config = {"populate_by_name": True}

    is_admin_command: bool = Field(
        alias="is_command",
        default=False,
        description="True if the message is a bot command starting with / (e.g. /remember, /debug, /help)",
    )
    command: str | None = Field(
        default=None,
        description="Command name without the slash, e.g. 'remember', 'debug', 'help'. None if not a command.",
    )
    skip: bool = Field(
        default=False,
        description="True if the bot should not reply (spam, self-mention, nonsensical)",
    )
    is_block_query: bool = Field(
        default=False,
        description=(
            "True if the user is asking who blocks them or who they block: "
            "'/bloques', '/blocks', 'quién me bloquea', 'me tienen bloqueado', "
            "'who blocks me'. Conservative: when in doubt, false."
        ),
    )


class BotReply(BaseModel):
    text: str = Field(
        default="",
        validation_alias=AliasChoices("text", "message"),
        description="Reply text. Max 300 chars. No hashtags. Rioplatense casual.",
    )
    should_update_profile: bool = Field(
        default=False,
        description="True if something notable about the user was revealed in this interaction",
    )
    image_search_query: str | None = Field(
        default=None,
        description=(
            "If the user asked for an image (meme, foto, etc.), the search query "
            "to find a matching image in the catalog. None if no image is needed. "
            "Use keywords in Spanish, e.g. 'gato enojado', 'meme programación'."
        ),
    )


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class MentionState(TypedDict):
    # Set before the graph runs — never modified by nodes
    mention_uri      : str
    mention_cid      : str
    mention_text     : str
    author_handle    : str
    thread_context   : str
    thread_root_uri  : str
    thread_root_cid  : str
    mode             : str
    is_admin         : bool

    # Filled in by nodes
    classification   : MentionClassification | None
    reply_text       : str | None
    should_update_profile : bool
    image_path       : str | None   # path de imagen a adjuntar al reply
    posted_reply_uri : str | None
    error            : str | None


# ---------------------------------------------------------------------------
# Bluesky client
# ---------------------------------------------------------------------------

class BskyClient:

    def __init__(self, handle: str, password: str):
        self._client = Client()
        self._client.login(handle, password)
        log.info("Logged in as %s", handle)

    def get_mentions(self) -> list[dict]:
        """Fetch recent mention and reply notifications.

        Dedup is delegated to the DB (`has_replied`) — the single source of
        truth. We intentionally do NOT filter on `is_read`: Bluesky's seen
        marker is global and coarse, and relying on it silently dropped
        mentions that failed mid-processing (their DB 'failed' → retry path
        was unreachable). Returning read notifications too lets `has_replied`
        decide, so a transient failure gets retried instead of lost.
        """
        notifs = self._client.app.bsky.notification.list_notifications(params={"limit": 25})
        mentions = []
        for n in notifs.notifications:
            if n.reason in ("mention", "reply"):
                mentions.append({
                    "uri"           : n.uri,
                    "cid"           : n.cid,
                    "author_handle" : n.author.handle,
                    "text"          : self._extract_text(n.record),
                })
        return mentions

    def mark_all_read(self) -> None:
        self._client.app.bsky.notification.update_seen(
            {"seenAt": datetime.now(timezone.utc).isoformat()}
        )

    def get_profile(self, handle: str):
        """Fetch the full Bluesky profile for a handle. Returns None on error."""
        try:
            return self._client.app.bsky.actor.get_profile({"actor": handle})
        except Exception as e:
            log.warning("Could not fetch profile for %s: %s", handle, e)
            return None

    def resolve_did(self, handle: str) -> str | None:
        """Resolve a handle to its Bluesky DID (stable across handle changes)."""
        try:
            return self._client.app.bsky.actor.get_profile({"actor": handle}).did
        except Exception as e:
            log.warning("Could not resolve DID for %s: %s", handle, e)
            return None

    def get_profile_handles(self, dids: list[str]) -> dict[str, str]:
        """Map DIDs → handles in batches of 25 (AppView getProfiles limit).
        DIDs that don't resolve (deleted/deactivated accounts) are omitted."""
        out: dict[str, str] = {}
        for i in range(0, len(dids), 25):
            batch = dids[i : i + 25]
            try:
                resp = self._client.app.bsky.actor.get_profiles({"actors": batch})
                for p in resp.profiles:
                    out[p.did] = p.handle
            except Exception as e:
                log.warning("get_profiles failed for batch of %d: %s", len(batch), e)
        return out

    def _thread_context(self, leaf) -> tuple[str, str, str]:
        """Parent chain of `leaf` as chronological context (leaf excluded),
        plus (root_uri, root_cid). Shared by get_thread_info and get_mention_by_uri.

        The leaf (the current mention) is intentionally excluded: GenerateReplyNode
        appends it separately as `current`. Including it here doubled the user's
        message in the LLM query (token waste + the bot perceived "publicaciones repetidas").
        """
        lines: list[str] = []
        root_uri = leaf.post.uri
        root_cid = leaf.post.cid
        node = getattr(leaf, "parent", None)
        while node is not None:
            if hasattr(node, "post"):
                lines.append(f"{node.post.author.handle}: {self._extract_text(node.post.record)}")
                root_uri = node.post.uri
                root_cid = node.post.cid
            node = getattr(node, "parent", None)
        lines.reverse()
        return "\n".join(lines), root_uri, root_cid

    def get_thread_info(self, uri: str, cid: str) -> tuple[str, str, str]:
        """
        Returns (context_text, root_uri, root_cid).
        context_text: chronological PRIOR conversation for the LLM (parent chain only).
        root_uri/cid: the original post that started the thread — required by Bluesky
                      to maintain thread structure when replying.
        """
        try:
            resp = self._client.app.bsky.feed.get_post_thread({"uri": uri})
        except Exception as e:
            log.warning("Could not fetch thread for %s: %s", uri, e)
            return "", uri, cid
        leaf = resp.thread
        if not hasattr(leaf, "post"):
            return "", uri, cid
        return self._thread_context(leaf)

    def get_mention_by_uri(self, uri: str) -> dict | None:
        """Reconstruct a mention dict (with thread context) from a single post URI.
        Used to retry failed/stale mentions on startup — the poll only sees the latest
        25 notifications, so older failed mentions must be refetched by URI. Returns
        None if the post is deleted/gone (caller should mark it 'ignored')."""
        try:
            resp = self._client.app.bsky.feed.get_post_thread({"uri": uri})
        except Exception as e:
            log.warning("get_mention_by_uri: fetch failed for %s: %s", uri, e)
            return None
        leaf = resp.thread
        if not hasattr(leaf, "post"):
            return None
        post = leaf.post
        context, root_uri, root_cid = self._thread_context(leaf)
        return {
            "uri"            : post.uri,
            "cid"            : post.cid,
            "author_handle"  : post.author.handle,
            "text"           : self._extract_text(post.record),
            "thread_context" : context,
            "thread_root_uri": root_uri,
            "thread_root_cid": root_cid,
        }

    def resolve_list_uri(self, uri: str) -> str:
        """
        Convert at://handle/app.bsky.graph.list/xxx to at://did:plc:.../app.bsky.graph.list/xxx.
        get_list_feed requires a DID-based URI — handles don't resolve in that endpoint.
        If the URI already contains a DID, returns it unchanged.
        """
        # at://did:plc:xxx/... — already resolved
        if uri.startswith("at://did:"):
            return uri

        # at://handle.bsky.social/app.bsky.graph.list/xxx
        parts = uri.removeprefix("at://").split("/", 1)
        if len(parts) != 2:
            log.warning("resolve_list_uri: unexpected URI format %s", uri)
            return uri

        handle, rest = parts
        try:
            profile = self._client.app.bsky.actor.get_profile({"actor": handle})
            resolved = f"at://{profile.did}/{rest}"
            log.info("Resolved %s → %s", uri, resolved)
            return resolved
        except Exception as e:
            log.error("Could not resolve handle %s: %s", handle, e)
            return uri

    def get_list_feed(self, list_uri: str, since: datetime | None, limit: int = 50) -> list[dict]:
        """
        Fetch posts from a Bluesky list feed.
        Returns posts newer than `since` (if provided) as plain dicts.
        Paginates automatically until it hits older posts or runs out.
        """
        list_uri  = self.resolve_list_uri(list_uri)
        posts     = []
        cursor    = None
        max_pages = 5  # safety cap — avoids infinite loops on huge feeds

        for page in range(max_pages):
            params = {"list": list_uri, "limit": limit}
            if cursor:
                params["cursor"] = cursor

            try:
                resp = self._client.app.bsky.feed.get_list_feed(params)
            except Exception as e:
                log.error("get_list_feed failed for %s: %s", list_uri, e)
                break

            if not resp.feed:
                log.debug("get_list_feed: empty page %d for %s", page, list_uri)
                break

            log.debug("get_list_feed: page %d — %d items", page, len(resp.feed))

            for item in resp.feed:
                post   = item.post
                raw_ts = post.indexed_at

                # indexed_at can be a datetime object or ISO string depending on SDK version
                if isinstance(raw_ts, datetime):
                    indexed_at = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc)
                else:
                    indexed_at = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))

                # Stop when we reach posts we already processed
                if since and indexed_at <= since:
                    log.debug("get_list_feed: reached already-seen posts, stopping")
                    return posts

                text = self._extract_text(post.record)
                if text:
                    posts.append({
                        "handle"     : post.author.handle,
                        "text"       : text,
                        "indexed_at" : indexed_at.isoformat(),
                    })

            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break

        log.info("get_list_feed: total %d posts fetched from %s", len(posts), list_uri)
        return posts

    def reply(self, text: str, parent_uri: str, parent_cid: str,
              root_uri: str, root_cid: str, image_path: str | None = None) -> str:
        """
        Post a reply, optionally with an attached image.
        parent = immediate post being replied to.
        root   = original post that started the thread.
        """
        reply_to = {
            "root"  : {"uri": root_uri,   "cid": root_cid},
            "parent": {"uri": parent_uri, "cid": parent_cid},
        }
        if image_path:
            img_bytes = Path(image_path).read_bytes()
            resp = self._client.send_image(
                text=text[:300],
                image=img_bytes,
                image_alt=text[:100],
                reply_to=reply_to,
            )
        else:
            resp = self._client.send_post(text=text[:300], reply_to=reply_to)
        return resp.uri


    def post(self, text: str, limit: int = 295) -> str:
        """
        Post a standalone skeet (no reply). Returns the new post URI.
        Truncates at the last sentence-ending punctuation before `limit`
        to avoid cutting mid-word.
        """
        if len(text) > limit:
            cut = text[:limit]
            # Walk back to the last sentence boundary
            for i in range(len(cut) - 1, max(len(cut) - 60, 0), -1):
                if cut[i] in ".!?":
                    text = cut[: i + 1]
                    break
            else:
                # No punctuation found — cut at last space
                text = cut[: cut.rfind(" ")] if " " in cut else cut
        resp = self._client.send_post(text=text)
        return resp.uri

    def _extract_text(self, record) -> str:
        if hasattr(record, "text"):
            return record.text or ""
        if isinstance(record, dict):
            return record.get("text", "")
        return ""


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

class LLMClient:
    """OpenAI-compatible client pointing at OpenRouter."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model  = model

    def complete(self, system: str, user: str, response_model: type[BaseModel]) -> BaseModel:
        """Call LLM → parse response into a Pydantic model."""
        schema   = response_model.model_json_schema()
        response = self._client.chat.completions.create(
            model          = self._model,
            messages       = [{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format = {"type": "json_object"},
            extra_body     = {"guided_json": schema},
        )
        raw = response.choices[0].message.content
        try:
            return response_model.model_validate_json(raw)
        except Exception:
            log.error("Validation failed for %s. Raw: %r", response_model.__name__, raw)
            raise

    def call_with_tools(self, system: str, user: str, tools: list[dict]) -> tuple[str | None, list]:
        """
        Call LLM with tool definitions.
        Returns (text_reply, tool_calls).
        text_reply  = direct text if the model answered without calling tools.
        tool_calls  = list of tool_call objects if the model wants to use tools.
        Exactly one of them will be non-empty.
        """
        response = self._client.chat.completions.create(
            model    = self._model,
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools    = tools,
            tool_choice = "auto",
        )
        msg = response.choices[0].message
        if msg.tool_calls:
            return None, msg.tool_calls
        return msg.content or "", []


# ---------------------------------------------------------------------------
# Feed processor
# ---------------------------------------------------------------------------

class FeedProcessor:
    """
    Reads a Bluesky list feed, summarizes new posts with the LLM,
    and appends the summary to context/feeds/{feed_name}.md.

    Runs at most once per `interval_hours` per feed (tracked in SQLite).
    """

    SUMMARIZE_SYSTEM = (
        "Sos un asistente que resume feeds de Bluesky para un bot de una comunidad argentina. "
        "Te llega una lista de posts recientes. Tu tarea es identificar los temas principales, "
        "debates, eventos o links que se están compartiendo. "
        "Evitá hacer resúmenes de temas sensibles"
        "Respondé en español rioplatense. Máximo 150 palabras. "
        "Formato: texto corrido con guiones para separar temas si hay más de uno. "
        "Sin hashtags. Sin markdown complejo. Si no hay nada relevante, respondé exactamente: NADA"
    )

    def __init__(self, bsky: BskyClient, llm: LLMClient, db: sqlite3.Connection):
        self.bsky = bsky
        self.llm  = llm
        self.db   = db

    def should_run(self, feed_name: str, interval_hours: int) -> bool:
        """Returns True if enough time has passed since the last run."""
        last_run = get_feed_last_run(self.db, feed_name)
        if last_run is None:
            return True
        last = datetime.fromisoformat(last_run)
        elapsed_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return elapsed_hours >= interval_hours

    def _summarize(self, posts: list[dict], feed_name: str) -> str | None:
        """Ask the LLM to summarize the posts. Returns None if nothing notable."""
        lines = [f"@{p['handle']}: {p['text']}" for p in posts[:50]]  # cap at 50 posts
        user  = f"Feed: {feed_name}\n\n" + "\n".join(lines)

        try:
            resp = self.llm._client.chat.completions.create(
                model    = self.llm._model,
                messages = [
                    {"role": "system", "content": self.SUMMARIZE_SYSTEM},
                    {"role": "user",   "content": user},
                ],
                max_tokens=400,
            )
            result = resp.choices[0].message.content.strip()
            return None if result.upper() == "NADA" else result
        except Exception as e:
            log.error("FeedProcessor: summarize failed for %s: %s", feed_name, e)
            return None

    def _append_to_file(self, feed_name: str, summary: str) -> None:
        """Append a timestamped summary entry to context/feeds/{feed_name}.md."""
        path      = FEEDS_DIR / f"{feed_name}.md"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if path.exists():
            existing = path.read_text(encoding="utf-8").rstrip()
        else:
            existing = f"# {feed_name}"

        updated = existing + f"\n\n## {timestamp}\n{summary}\n"
        path.write_text(updated, encoding="utf-8")
        log.info("Feed %s: summary saved to %s", feed_name, path.name)

    def process(self, feed_name: str, list_uri: str, interval_hours: int, full_backfill: bool = False) -> None:
        """
        Main entry point. Runs the full fetch → summarize → save pipeline.
        full_backfill=True ignores last_run and fetches as far back as the pagination cap allows.
        """
        if not self.should_run(feed_name, interval_hours):
            log.debug("Feed %s: skipping (not due yet)", feed_name)
            return

        log.info("Feed %s: fetching%s", feed_name, " (full backfill)" if full_backfill else "")

        # full_backfill → since=None fetches everything up to max_pages
        last_run_str = get_feed_last_run(self.db, feed_name)
        since        = None if full_backfill else (
            datetime.fromisoformat(last_run_str) if last_run_str else None
        )

        posts = self.bsky.get_list_feed(list_uri, since=since)
        log.info("Feed %s: %d new posts fetched", feed_name, len(posts))

        # Always update last_run, even if there's nothing to summarize
        save_feed_last_run(self.db, feed_name)

        if not posts:
            log.info("Feed %s: nothing new", feed_name)
            return

        summary = self._summarize(posts, feed_name)
        if summary:
            self._append_to_file(feed_name, summary)
        else:
            log.info("Feed %s: LLM found nothing notable", feed_name)

    def post_opinion(self, feed_name: str) -> None:
        """
        Read the latest summary block from context/feeds/{feed_name}.md,
        generate a short personal opinion using reflect_feed_prompt.md,
        and post it as a standalone skeet.
        """
        feed_path = FEEDS_DIR / f"{feed_name}.md"
        if not feed_path.exists():
            log.error("post_opinion: no feed file found for %s", feed_name)
            return

        # Extract the most recent summary block (last ## section)
        content = feed_path.read_text(encoding="utf-8")
        blocks  = content.split("\n## ")
        if len(blocks) < 2:
            log.error("post_opinion: feed file for %s has no summary entries yet", feed_name)
            return
        latest_block = blocks[-1].strip()

        prompt = load_text(PROMPTS_DIR / "reflect_feed_prompt.md")
        if not prompt:
            log.error("post_opinion: reflect_feed_prompt.md not found")
            return

        soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
        # Reinforce the character limit explicitly — models tend to ignore it otherwise
        system = (
            f"{soul}\n\n---\n{prompt}\n\n"
            "IMPORTANTE: tu respuesta tiene que tener MENOS DE 250 CARACTERES en total. "
            "Contá los caracteres antes de responder. Si te pasás, cortá."
        )

        try:
            resp = self.llm._client.chat.completions.create(
                model    = self.llm._model,
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": f"Feed: {feed_name}\n\n{latest_block}"},
                ],
                max_tokens=200,
            )
            raw     = resp.choices[0].message.content
            if not raw:
                log.error("post_opinion: LLM returned empty content for %s", feed_name)
                return
            opinion = raw.strip()
        except Exception as e:
            log.error("post_opinion: LLM failed for %s: %s", feed_name, e)
            return

        log.info("post_opinion [%s]: %s", feed_name, opinion[:80])
        uri = self.bsky.post(opinion)
        log_bot_post(self.db, uri=uri, in_reply_to=None, reply_to_handle=None, text=opinion)
        log.info("post_opinion: posted %s", uri)


# ---------------------------------------------------------------------------
# Tools concretas
# Los handlers reciben (args, ctx) y devuelven un ToolResult. ctx.state es el
# MentionState y ctx.conn la conexión sqlite. Se cierran sobre los globals del
# módulo (dbmod, MEMORY_PATH, load_text) para no acoplar tools.py a la app.
# El schema + scopes de cada tool se declara en build_tool_registry().
# ---------------------------------------------------------------------------

def _tool_save_to_user_profile(args: dict, ctx: ToolContext) -> ToolResult:
    content = args["content"]
    dbmod.upsert_user_fact(ctx.conn, ctx.state["author_handle"], content, source_uri="/remember")
    log.info("/remember → user_facts @%s: %s", ctx.state["author_handle"], content)
    return ToolResult(text=f"anotado en tu perfil: {content}")


def _tool_save_to_memory(args: dict, ctx: ToolContext) -> ToolResult:
    content   = args["content"]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing  = load_text(MEMORY_PATH)
    updated   = existing.rstrip() + f"\n\n## {timestamp}\n- {content}\n"
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_PATH.write_text(updated, encoding="utf-8")
    log.info("/remember → MEMORY.md: %s", content)
    return ToolResult(text=f"anotado en mi memoria: {content}")


def _tool_get_debug_info(args: dict, ctx: ToolContext) -> ToolResult:
    s = ctx.state
    return ToolResult(text=(
        f"[debug] mode={s['mode']} | author={s['author_handle']} | text={s['mention_text'][:80]}"
    ))


def _tool_get_help(args: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(text=(
        "comandos: /remember <texto>, /bloques (quién te bloquea, vía ClearSky), "
        "/imagen <búsqueda>, /debug, /help"
    ))


def _tool_search_images(args: dict, ctx: ToolContext) -> ToolResult:
    query    = args.get("query", "")
    category = args.get("category")
    results  = dbmod.hybrid_search_image_catalog(ctx.conn, query, category=category, limit=5)
    if not results:
        return ToolResult(text=f"no encontré imágenes para '{query}'")
    best = results[0]
    dbmod.mark_image_used(ctx.conn, best["id"])
    summary = ", ".join(f"{r['description'][:50]}… [{r['category']}]" for r in results[:3])
    return ToolResult(text=f"encontré {len(results)} imágenes: {summary}", image_path=best["file_path"])


def build_tool_registry(config: dict | None = None) -> ToolRegistry:
    """Registra las tools concretas y aplica overrides de settings.json → sección TOOLS.

    Scopes actuales: todas en ADMIN (paridad de comportamiento con el ADMIN_TOOLS
    previo). Los scopes REPLY / FEED_REFLECTION ya existen en el framework para las
    tools que vienen (búsqueda web, calendar, música) sin tocar esta base.
    """
    reg = ToolRegistry()
    reg.register(
        "save_to_user_profile",
        "Save a fact about the user who is speaking to their profile file. "
        "Use for personal data: age, pronouns, location, job, teams, preferences, events.",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to save, written as a short plain sentence."}
            },
            "required": ["content"],
        },
        _tool_save_to_user_profile,
        {Scope.ADMIN},
    )
    reg.register(
        "save_to_memory",
        "Save a general fact to the bot's own MEMORY.md. "
        "Use for community context, standing instructions for the bot, or world-facts.",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to save, written as a short plain sentence."}
            },
            "required": ["content"],
        },
        _tool_save_to_memory,
        {Scope.ADMIN},
    )
    reg.register(
        "get_debug_info",
        "Return current runtime debug information about the bot.",
        {"type": "object", "properties": {}, "required": []},
        _tool_get_debug_info,
        {Scope.ADMIN},
    )
    reg.register(
        "get_help",
        "Return the list of available commands.",
        {"type": "object", "properties": {}, "required": []},
        _tool_get_help,
        {Scope.ADMIN},
    )
    reg.register(
        "search_images",
        "Search the image catalog for images matching a query. "
        "Use when the admin asks for a meme, image, foto, or picture. "
        "Returns the path to the best matching image so it can be posted.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query in Spanish, e.g. 'meme de gato', 'perro enojado'."},
                "category": {"type": "string", "description": "Optional category filter: 'meme', 'foto', 'arte', 'captura', 'otro'."},
            },
            "required": ["query"],
        },
        _tool_search_images,
        {Scope.ADMIN},
    )
    if config:
        reg.apply_config(config)
    return reg


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

class ClassifyNode:
    """
    Node 1: Classify the mention.
    Uses LITE_MODEL — detecting a /command doesn't need reasoning.
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def run(self, state: MentionState) -> dict:
        system = (
            "You are a classifier for a Bluesky bot. "
            "Return JSON. A command starts with '/' (e.g. /remember, /debug, /help). "
            "For commands, set is_command=true and command=<name without slash, lowercase>. "
            "Set skip=true only for spam or self-mentions. "
            "Set is_block_query=true ONLY for explicit block-list questions: '/bloques', "
            "'/blocks', 'quién me bloquea', 'quien me bloquea', 'me tienen bloqueado', "
            "'a quién bloqueo', 'who blocks me'. Be conservative — when in doubt, false. "
            "Respond ONLY with valid JSON."
        )
        try:
            result = self.llm.complete(system, f"Mention: {state['mention_text']}", MentionClassification)
        except Exception as e:
            log.error("ClassifyNode failed: %s", e)
            result = MentionClassification(is_admin_command=False, command=None, skip=False)

        log.info("Classified: is_command=%s command=%r skip=%s", result.is_admin_command, result.command, result.skip)
        return {"classification": result}


class LoadContextNode:
    """
    Node 2: Ensure the user exists in the DB. For first-time users, fetch the
    Bluesky bio, interpret it, and ingest the extracted facts into user_facts.
    No usa archivos .md — la recuperación por relevancia la hace GenerateReplyNode
    vía dbmod.hybrid_search_user_facts.
    """

    def __init__(self, llm: LLMClient, bsky: BskyClient, conn: sqlite3.Connection):
        self.llm  = llm
        self.bsky = bsky
        self.conn = conn

    def run(self, state: MentionState) -> dict:
        handle = state["author_handle"]
        self.conn.execute(
            "INSERT INTO users(handle) VALUES (?) ON CONFLICT(handle) DO NOTHING",
            (handle,),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT bio_raw, bio_interp FROM users WHERE handle = ?", (handle,)
        ).fetchone()
        if row["bio_raw"] is None and row["bio_interp"] is None:
            self._ingest_bio(handle)

        return {}  # el contexto relevante se recupera en GenerateReplyNode

    def _ingest_bio(self, handle: str) -> None:
        """Trae el profile de Bluesky, interpreta la bio y guarda todo + facts en la DB.
        También cachea `did` y `display_name` (el DID es estable ante cambios de handle
        y lo usa HandleBlockQueryNode sin re-fetchear)."""
        profile = self.bsky.get_profile(handle)
        if not profile:
            return
        bio = profile.description or ""
        interp = self._extract_from_bio(handle, bio) if bio else ""
        bio_interp = None if (not interp or interp.upper() == "NADA") else interp
        self.conn.execute(
            "UPDATE users SET did = ?, display_name = ?, bio_raw = ?, bio_interp = ?, "
            "updated_at = datetime('now') WHERE handle = ?",
            (profile.did, profile.display_name or None, bio or None, bio_interp, handle),
        )
        self.conn.commit()
        if bio_interp:
            for line in bio_interp.splitlines():
                fact = line.strip().lstrip("-").strip()
                if fact:
                    dbmod.upsert_user_fact(self.conn, handle, fact, source_uri="bio")

    def _extract_from_bio(self, handle: str, bio: str) -> str:
        if not bio:
            return ""
        prompt = load_text(PROMPTS_DIR / "interpret_bio_prompt.md")
        if not prompt:
            log.warning("interpret_bio_prompt.md not found")
            return ""
        try:
            response = self.llm._client.chat.completions.create(
                model    = self.llm._model,
                messages = [
                    {"role": "system", "content": prompt.format(handle=handle)},
                    {"role": "user",   "content": f"Bio de @{handle}: {bio}"},
                ],
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            log.error("Bio extraction failed for @%s: %s", handle, e)
            return ""


class GenerateReplyNode:
    """
    Node 3: Generate reply.
    Context = SOUL.md + MEMORY.md + user profile.
    """

    def __init__(self, llm: LLMClient, conn: sqlite3.Connection):
        self.llm  = llm
        self.conn = conn

    def run(self, state: MentionState) -> dict:
        soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
        memory = load_text(MEMORY_PATH)
        handle = state["author_handle"]

        thread  = state.get("thread_context", "")
        current = f"{handle}: {state['mention_text']}"
        query   = f"{thread}\n{current}" if thread else current

        facts   = dbmod.hybrid_search_user_facts(self.conn, handle, query, k=5)
        lessons = dbmod.hybrid_search_lessons(self.conn, query, k=5)

        parts = [soul]
        if memory:
            parts.append(f"\n---\nTu memoria general:\n{memory}")
        if facts:
            parts.append("\n---\nHechos que sabés del usuario:\n" + "\n".join(f"- {t}" for _, t in facts))
        if lessons:
            parts.append("\n---\nLecciones de comportamiento:\n" + "\n".join(f"- {t}" for _, t in lessons))

        # Resumen del catálogo de imágenes
        cat_stats = dbmod.get_image_catalog_stats(self.conn)
        if cat_stats["total"] > 0:
            cat_lines = [f"Total: {cat_stats['total']} imágenes disponibles"]
            for entry in cat_stats["by_category"]:
                cat_lines.append(
                    f"  [{entry['category']}] {entry['platform']}/{entry['source_name']}: {entry['count']}"
                )
            parts.append("\n---\nCatálogo de imágenes disponibles:\n" + "\n".join(cat_lines))
            parts.append(
                "Si el usuario te pide una imagen, meme o foto, seteá el campo "
                "'image_search_query' con palabras clave de búsqueda en español."
            )
        else:
            parts.append("\n---\nNo hay imágenes disponibles en el catálogo.")

        parts.append(
            "\n---\nRespondé SOLO con JSON válido. "
            "Campo 'text': tu respuesta (máx 300 chars). "
            "Campo 'should_update_profile': true si aprendiste algo notable del usuario. "
            "Campo 'image_search_query': null. Solo poné un valor si el usuario "
            "explícitamente pide una imagen, meme o foto (ej. 'meme de gato')."
        )
        system = "\n".join(parts)
        user   = query

        try:
            result = self.llm.complete(system, user, BotReply)
        except Exception as e:
            log.error("GenerateReplyNode failed: %s", e)
            return {"error": str(e)}

        return {
            "reply_text": result.text,
            "should_update_profile": result.should_update_profile,
            "image_path": self._resolve_image(result.image_search_query),
        }

    def _resolve_image(self, search_query: str | None) -> str | None:
        """Busca una imagen en el catálogo y devuelve su path, o None."""
        if not search_query:
            return None
        results = dbmod.hybrid_search_image_catalog(self.conn, search_query, limit=1)
        if results:
            image = results[0]
            dbmod.mark_image_used(self.conn, image["id"])
            log.info("Image matched: %s → %s", search_query, image["file_path"])
            return image["file_path"]
        log.info("No image found for query: %s", search_query)
        return None


class HandleAdminCommandNode:
    """
    Node 3b: Handle admin commands using tool calling.
    The LLM receives the command text and a set of tools.
    It decides which tool to call — we execute it and use the result as reply.
    Tools: save_to_user_profile, save_to_memory, get_debug_info, get_help.
    """

    def __init__(self, llm: LLMClient, conn: sqlite3.Connection, registry: ToolRegistry):
        self.llm      = llm
        self.conn     = conn
        self.registry = registry

    def run(self, state: MentionState) -> dict:
        system = (
            "Sos el sistema de comandos de un bot de Bluesky. "
            "El admin te mandó un comando. Usá la tool apropiada. "
            "Para /remember: si el dato es sobre el usuario → save_to_user_profile. "
            "Si es general o sobre la comunidad → save_to_memory. "
            "Para /debug → get_debug_info. Para /help → get_help. "
            "Llamá exactamente UNA tool."
        )
        user = (
            f"Admin: @{state['author_handle']}\n"
            f"Comando recibido: {state['mention_text']}"
        )

        admin_tools = self.registry.openai_schemas(Scope.ADMIN)
        text_reply, tool_calls = self.llm.call_with_tools(system, user, admin_tools)

        # No tool called — use direct text response as fallback
        if not tool_calls:
            log.warning("HandleAdminCommandNode: no tool called, using direct reply")
            return {"reply_text": text_reply or "comando no reconocido"}

        # Execute the first tool call (we always expect exactly one)
        call      = tool_calls[0]
        tool_name = call.function.name
        tool_args = json.loads(call.function.arguments)
        log.info("Tool called: %s(%s)", tool_name, tool_args)

        outcome = self.registry.execute(tool_name, tool_args, ToolContext(state=state, conn=self.conn))
        result: dict = {"reply_text": outcome.text}
        if outcome.image_path:
            result["image_path"] = outcome.image_path
        return result


class HandleBlockQueryNode:
    """
    Node 3c: Responde "quién me bloquea" consultando ClearSky (proxy + cache).
    Sin LLM — la reply se arma deterministamente. Abierto a cualquier usuario:
    route_after_classify lo deriva acá antes del gate admin, así /bloques funciona
    en --mode open para todos y en admin_only para Polci (el filtro de process_mention
    ya descartó a los no-admin antes de llegar al grafo).

    Flujo: resolver DID del autor → cache TTL 1h o fetch ClearSky → resolver handles
    de los bloqueadores → formatear reply ≤ 300 chars. Fallback graceful si ClearSky
    cae o no se puede resolver el DID.
    """

    CLEARSKY_UI_URL = "https://clearsky.app/bsky/{handle}"

    def __init__(self, bsky: BskyClient, conn: sqlite3.Connection):
        self.bsky = bsky
        self.conn = conn

    def run(self, state: MentionState) -> dict:
        handle = state["author_handle"]
        did = self._get_author_did(handle)
        if not did:
            return {"reply_text": "no pude resolver tu cuenta ahora, probá más tarde"}

        blockers = dbmod.get_cached_blocklist(self.conn, did)
        if blockers is None:
            try:
                blockers = clearsky_mod.get_blocked_by(did)
            except clearsky_mod.ClearSkyError as e:
                log.warning("ClearSky failed for %s: %s", did, e)
                return {"reply_text": "no pude consultar ClearSky ahora, probá en un rato"}
            dbmod.save_blocklist_cache(self.conn, did, blockers)

        if not blockers:
            return {"reply_text": "por ahora nadie te tiene bloqueado, según ClearSky. 🌤️"}

        dids = [b["did"] for b in blockers]
        handle_map = self.bsky.get_profile_handles(dids)
        return {"reply_text": self._format_reply(handle, blockers, handle_map)}

    def _get_author_did(self, handle: str) -> str | None:
        """DID del autor: cacheado en users.did, o fresco vía get_profile.
        Asegura la fila en users (el path de block-query saltea LoadContextNode)."""
        self.conn.execute(
            "INSERT INTO users(handle) VALUES (?) ON CONFLICT(handle) DO NOTHING",
            (handle,),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT did FROM users WHERE handle = ?", (handle,)
        ).fetchone()
        if row and row["did"]:
            return row["did"]
        did = self.bsky.resolve_did(handle)
        if did:
            self.conn.execute(
                "UPDATE users SET did = ?, updated_at = datetime('now') WHERE handle = ?",
                (did, handle),
            )
            self.conn.commit()
        return did

    def _format_reply(
        self,
        author_handle: str,
        blockers: list[dict],
        handle_map: dict[str, str],
    ) -> str:
        total = len(blockers)
        handles = [handle_map.get(b["did"]) for b in blockers]
        known = [h for h in handles if h]
        if total <= 8:
            if known:
                names = ", ".join(f"@{h}" for h in known)
                return f"Te bloquean {total}: {names}. (datos de ClearSky)"
            return f"Te bloquean {total} cuentas que no pude resolver por handle. (ClearSky)"
        sample = ", ".join(f"@{h}" for h in known[:5]) or "—"
        url = self.CLEARSKY_UI_URL.format(handle=author_handle)
        return f"Te bloquean {total} cuentas. Algunas: {sample}… Listado completo: {url}"


class PostReplyNode:
    """
    Node 4: Post reply to Bluesky and update SQLite status.
    Status flow: pending → replied (success) or failed (error).
    """

    def __init__(self, bsky: BskyClient, db: sqlite3.Connection):
        self.bsky = bsky
        self.db   = db

    def run(self, state: MentionState) -> dict:
        uri = state["mention_uri"]

        if not state.get("reply_text"):
            log.warning("No reply text for %s — marking failed", uri)
            update_status(self.db, uri, "failed")
            return {"posted_reply_uri": None}

        try:
            image_path = state.get("image_path")
            posted_uri = self.bsky.reply(
                text       = state["reply_text"],
                parent_uri = uri,
                parent_cid = state["mention_cid"],
                root_uri   = state["thread_root_uri"],
                root_cid   = state["thread_root_cid"],
                image_path = image_path,
            )
            log.info("Replied to @%s: %s", state["author_handle"], state["reply_text"][:60])
            update_status(self.db, uri, "replied")
            log_bot_post(
                self.db,
                uri             = posted_uri,
                in_reply_to     = uri,
                reply_to_handle = state["author_handle"],
                text            = state["reply_text"],
            )
        except Exception as e:
            log.error("Failed to post reply: %s", e)
            update_status(self.db, uri, "failed")
            posted_uri = None

        return {"posted_reply_uri": posted_uri}


class UpdateProfileNode:
    """
    Node 5: Post-reply, extrae hechos nuevos que el usuario reveló sobre sí mismo
    y los guarda en db.user_facts (con dedup semántico). El LLM decide — si no hay
    nada notable, devuelve NADA. Usa REASONING_MODEL.
    """

    def __init__(self, llm: LLMClient, conn: sqlite3.Connection):
        self.llm  = llm
        self.conn = conn

    def run(self, state: MentionState) -> dict:
        handle  = state["author_handle"]
        thread  = state.get("thread_context", "")
        current = f"{handle}: {state['mention_text']}"
        bot_reply = state.get("reply_text", "")
        conversation = f"{thread}\nbot: {bot_reply}\n{current}" if thread else f"bot: {bot_reply}\n{current}"

        prompt = load_text(PROMPTS_DIR / "update_user_prompt.md")
        if not prompt:
            return {}

        try:
            response = self.llm._client.chat.completions.create(
                model    = self.llm._model,
                messages = [
                    {"role": "system", "content": prompt.format(author_handle=handle)},
                    {"role": "user",   "content": conversation},
                ],
            )
            result = response.choices[0].message.content.strip()
        except Exception as e:
            log.error("UpdateProfileNode failed for @%s: %s", handle, e)
            return {}

        if result.upper() == "NADA":
            return {}

        new_facts = 0
        for line in result.splitlines():
            fact = line.strip().lstrip("-").strip()
            if fact and dbmod.upsert_user_fact(self.conn, handle, fact, source_uri="reply") is not None:
                new_facts += 1
        if new_facts:
            log.info("@%s: %d hecho(s) nuevo(s) guardado(s)", handle, new_facts)
        return {}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_after_classify(state: MentionState) -> str:
    cls = state.get("classification")
    log.info(
        "Routing: is_command=%s command=%r is_admin=%s skip=%s block_query=%s",
        cls.is_admin_command if cls else None,
        cls.command if cls else None,
        state.get("is_admin"),
        cls.skip if cls else None,
        cls.is_block_query if cls else None,
    )
    # Block query va antes del gate admin: /bloques es abierto a cualquier usuario.
    if cls and cls.is_block_query:
        return "handle_block_query"
    if cls and cls.skip:
        return "skip"
    if cls and cls.is_admin_command and state.get("is_admin"):
        return "handle_admin_command"
    return "load_context"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph(llm: LLMClient, bsky: BskyClient, db: sqlite3.Connection):
    """
    Flow:
        START → classify
            → skip                                          → END
            → handle_admin_command → post_reply → END
            → load_context → generate_reply → post_reply
                                                → update_profile → END
    """
    lite_llm = LLMClient(api_key=OPENROUTER_API_KEY, base_url=OPENAI_ENDPOINT, model=LITE_MODEL)
    registry = build_tool_registry(TOOLS_CONFIG)
    log.info("Tool registry: %s", ", ".join(registry.names()) or "(vacío)")

    classify       = ClassifyNode(lite_llm)
    load_context   = LoadContextNode(llm, bsky, db)
    generate_reply = GenerateReplyNode(llm, db)
    handle_admin   = HandleAdminCommandNode(llm, db, registry)
    handle_blocks  = HandleBlockQueryNode(bsky, db)
    post_reply     = PostReplyNode(bsky, db)
    update_profile = UpdateProfileNode(llm, db)

    g = StateGraph(MentionState)
    g.add_node("classify_mention",      classify.run)
    g.add_node("load_context",          load_context.run)
    g.add_node("generate_reply",        generate_reply.run)
    g.add_node("handle_admin_command",  handle_admin.run)
    g.add_node("handle_block_query",    handle_blocks.run)
    g.add_node("post_reply",            post_reply.run)
    g.add_node("update_profile",        update_profile.run)

    g.add_edge(START, "classify_mention")
    g.add_conditional_edges(
        "classify_mention",
        route_after_classify,
        {
            "handle_admin_command" : "handle_admin_command",
            "handle_block_query"   : "handle_block_query",
            "load_context"         : "load_context",
            "skip"                 : END,
        },
    )
    g.add_edge("load_context",         "generate_reply")
    g.add_edge("generate_reply",       "post_reply")
    g.add_edge("handle_admin_command", "post_reply")
    g.add_edge("handle_block_query",   "post_reply")
    g.add_edge("post_reply",           "update_profile")
    g.add_edge("update_profile",       END)

    return g.compile()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def process_mention(graph, db: sqlite3.Connection, mention: dict, mode: str) -> None:
    uri    = mention["uri"]
    author = mention["author_handle"]

    if has_replied(db, uri):
        log.debug("Already handled %s — skipping", uri)
        return

    if mode == "admin_only" and author != ADMIN_HANDLE:
        log.info("admin_only: ignorando a @%s (no es admin)", author)
        mark_pending(db, uri, mention["cid"], author, mode)
        update_status(db, uri, "ignored")
        return

    initial_state: MentionState = {
        "mention_uri"          : uri,
        "mention_cid"          : mention["cid"],
        "mention_text"         : mention["text"],
        "author_handle"        : author,
        "thread_context"       : mention.get("thread_context", ""),
        "thread_root_uri"      : mention.get("thread_root_uri", uri),
        "thread_root_cid"      : mention.get("thread_root_cid", mention["cid"]),
        "mode"                 : mode,
        "is_admin"             : author == ADMIN_HANDLE,
        "classification"       : None,
        "reply_text"           : None,
        "should_update_profile": False,
        "image_path"           : None,
        "posted_reply_uri"     : None,
        "error"                : None,
    }

    mark_pending(db, uri, mention["cid"], author, mode)
    log.info("Processing mention from @%s", author)
    graph.invoke(initial_state)


def retry_stuck_mentions(graph, db: sqlite3.Connection, bsky: BskyClient, mode: str) -> None:
    """Reprocesa al arranque mentions que quedaron trancados.

    - 'pending' stale (el bot murió antes de pasarlos a 'replied'/'failed') → se
      pasan a 'failed' porque has_replied saltea 'pending' y nunca se reintentarían.
    - 'failed' que cayeron fuera de la ventana de las últimas 25 notifs → la poll
      de Bluesky no los vuelve a traer, acá los refetcheamos por URI.

    Posts borrados (get_mention_by_uri → None) se marcan 'ignored' para no reintentarlos.
    """
    db.execute("UPDATE replied_posts SET status = 'failed' WHERE status = 'pending'")
    db.commit()
    rows = db.execute(
        "SELECT uri, cid, author, mode FROM replied_posts WHERE status = 'failed'"
    ).fetchall()
    if not rows:
        return
    log.info("Retrying %d stuck/failed mention(s) from last run", len(rows))
    for r in rows:
        mention = bsky.get_mention_by_uri(r["uri"])
        if mention is None:
            log.warning("Could not refetch %s (deleted?) — marking ignored", r["uri"])
            update_status(db, r["uri"], "ignored")
            continue
        process_mention(graph, db, mention, mode)


def run(mode: str) -> None:
    log.info("Starting butterbot — mode: %s", mode.upper())

    db   = init_db()
    bsky = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
    llm  = LLMClient(api_key=OPENROUTER_API_KEY, base_url=OPENAI_ENDPOINT, model=REASONING_MODEL)
    lite_llm = LLMClient(api_key=OPENROUTER_API_KEY, base_url=OPENAI_ENDPOINT, model=LITE_MODEL)

    graph          = build_graph(llm, bsky, db)
    feed_processor = FeedProcessor(bsky, lite_llm, db)

    log.info("Graph compiled. Polling every %ds. Admin: %s", POLL_INTERVAL, ADMIN_HANDLE)
    log.info("Feeds configured: %d", len(FEEDS_CONFIG))

    # Reprocesar mentions trancados del run anterior antes de entrar al loop.
    retry_stuck_mentions(graph, db, bsky, mode)

    while True:
        try:
            # --- Feed processing (runs only when interval is due) ---
            for feed in FEEDS_CONFIG:
                feed_processor.process(
                    feed_name     = feed["name"],
                    list_uri      = feed["uri"],
                    interval_hours= feed.get("interval_hours", 6),
                )

            # --- Mention polling ---
            mentions = [m for m in bsky.get_mentions() if not has_replied(db, m["uri"])]
            if mentions:
                log.info("Found %d mention(s) to process", len(mentions))
                for mention in mentions:
                    ctx, root_uri, root_cid = bsky.get_thread_info(mention["uri"], mention["cid"])
                    mention["thread_context"]  = ctx
                    mention["thread_root_uri"] = root_uri
                    mention["thread_root_cid"] = root_cid
                    process_mention(graph, db, mention, mode)
                # mark_all_read is UI hygiene only (keeps Polci's notif tab clean);
                # it no longer gates dedup — the DB does.
                bsky.mark_all_read()

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except (BskyNetworkError, BskyRequestException) as e:
            # Transient Bluesky/server hiccups (502/503/504, timeouts, connection
            # errors). Don't log a scary traceback — just warn and retry next cycle.
            # 400/401 (real bugs / auth) fall through to the generic handler below.
            log.warning("Transient Bluesky error (will retry next cycle): %s", e)
        except Exception as e:
            log.error("Unhandled error: %s", e, exc_info=True)

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="butterbot")
    parser.add_argument(
        "--mode",
        choices=["admin_only", "open"],
        default="admin_only",
    )
    parser.add_argument(
        "--fetch-feeds",
        action="store_true",
        help="Process all configured feeds once and exit (ignores interval check).",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Use with --fetch-feeds: ignore last_run and fetch everything up to the pagination cap.",
    )
    parser.add_argument(
        "--post-summary",
        metavar="FEED_NAME",
        help="Generate and post an opinion about the latest summary of a feed, then exit.",
    )
    args = parser.parse_args()

    if args.fetch_feeds:
        db        = init_db()
        bsky      = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
        lite_llm  = LLMClient(api_key=OPENROUTER_API_KEY, base_url=OPENAI_ENDPOINT, model=LITE_MODEL)
        processor = FeedProcessor(bsky, lite_llm, db)
        for feed in FEEDS_CONFIG:
            processor.process(
                feed_name      = feed["name"],
                list_uri       = feed["uri"],
                interval_hours = 0,
                full_backfill  = args.backfill,
            )
        log.info("Feed fetch complete.")

    elif args.post_summary:
        db        = init_db()
        bsky      = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
        llm       = LLMClient(api_key=OPENROUTER_API_KEY, base_url=OPENAI_ENDPOINT, model=LITE_MODEL)
        processor = FeedProcessor(bsky, llm, db)
        processor.post_opinion(feed_name=args.post_summary)

    else:
        run(mode=args.mode)