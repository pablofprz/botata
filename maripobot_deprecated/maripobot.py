"""
maripobot -- Bluesky bot for the Argentine community.
"""

from __future__ import annotations
from dotenv import load_dotenv
import json
import os
import random
import re
import time
import mimetypes
from collections import Counter
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Optional

import instructor
from instructor import Mode
from pydantic import BaseModel, Field

import feedparser
import frontmatter  # noqa: F401  (kept in case downstream prompts use frontmatter)
import requests
import spotipy
from atproto import Client, models
from openai import OpenAI
from spotipy.oauth2 import SpotifyOAuth
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)
from youtube_transcript_api.proxies import WebshareProxyConfig


# All paths are resolved relative to the script location so the bot runs the
# same on Windows (dev) and Raspberry Pi OS (prod) without os.chdir() hacks.

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
CONTEXT_DIR = SCRIPT_DIR / "context"
USERS_DIR = CONTEXT_DIR / "users"
POSTED_DIR = SCRIPT_DIR / "posted"
PROMPTS_DIR = SCRIPT_DIR / "prompts"
STATE_DIR = SCRIPT_DIR / "state"  # auto-created at runtime

# Make sure runtime-writable directories exist on first run
for d in (USERS_DIR, POSTED_DIR, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)


load_dotenv(SCRIPT_DIR / ".env")


def _load_json(path: Path, default=None, allow_trailing_commas: bool = False):
    """
    Load a JSON file. Optionally tolerate trailing commas (some configs are
    hand-edited and may have them).
    """
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"Config file not found: {path}")

    text = path.read_text(encoding="utf-8")
    if allow_trailing_commas:
        # Strip trailing commas before } or ]
        text = re.sub(r",(\s*[}\]])", r"\1", text)

    return json.loads(text)


# Secrets from environment
BSKY_PASSWORD = os.environ["BSKY_PASSWORD"]
OPENAI_KEY = os.environ["OPENROUTER_API_KEY"]
SPOTIFY_CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
# Optional: Webshare proxy credentials for YouTube transcript API (avoids IP bans)
WEBSHARE_USER = os.environ.get("WEBSHARE_USER")
WEBSHARE_PASSWORD = os.environ.get("WEBSHARE_PASSWORD")


# Non-sensitive config from JSON
SETTINGS = _load_json(CONFIG_DIR / "settings.json")

# Bluesky
BOT_HANDLE: str = SETTINGS["BOT_HANDLE"]
ADMIN_HANDLE = SETTINGS["ADMIN_HANDLE"]

# OpenRouter
OPENAI_ENDPOINT: str = SETTINGS.get("OPENAI_ENDPOINT", "https://openrouter.ai/api/v1")

# Models
REASONING_MODEL = SETTINGS["REASONING_MODEL"]
IMAGE_MODEL = SETTINGS["IMAGE_MODEL"]
LITE_MODEL = SETTINGS["LITE_MODEL"]

# Spotify (only the redirect URI; client_id/secret are secrets)
SPOTIFY_REDIRECT_URI: str = SETTINGS.get(
    "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
)

# Daily reply cap (max USD spent on OpenRouter per calendar day before
# the bot stops replying to users)
OPENROUTER_DAILY_BUDGET_USD: float = float(
    SETTINGS.get("OPENROUTER_DAILY_BUDGET_USD", 1.00)
)
CREDIT_CHECK_INTERVAL_S: int = int(SETTINGS.get("CREDIT_CHECK_INTERVAL_S", 300))

# Whether the history-update pipeline always writes to history.md regardless of
# whether the LLM finds new lessons (True = compulsive daily update, False = only
# if the model decides there's something worth adding).
HISTORY_UPDATE_COMPULSIVE: bool = bool(SETTINGS.get("HISTORY_UPDATE_COMPULSIVE", True))

# Whether the reflection pipeline requires the update_history pipeline to have
# successfully run today before it posts. Set False to fall back to the old
# behaviour (reflect on full history at any time, even without a fresh update).
HISTORY_REFLECTION_REQUIRES_UPDATE: bool = bool(
    SETTINGS.get("HISTORY_REFLECTION_REQUIRES_UPDATE", True)
)


# External content sources
IMAGE_SUBREDDITS: list[str] = _load_json(
    CONFIG_DIR / "image_subreddits.json", allow_trailing_commas=True
)
VIDEO_SUBREDDITS: list[str] = _load_json(
    CONFIG_DIR / "video_subreddits.json", allow_trailing_commas=True
)
NEWS_SITES: list[str] = _load_json(
    CONFIG_DIR / "news_sites.json", allow_trailing_commas=True
)
SPOTIFY_PLAYLISTS: list[str] = _load_json(
    CONFIG_DIR / "spotify_playlists.json",
    default=[],
    allow_trailing_commas=True,
)

# Persistence paths
RESPONDIDO_PATH = POSTED_DIR / "respondido.json"
POSTED_IMAGES_PATH = POSTED_DIR / "posted_images.json"
POSTED_VIDEOS_PATH = POSTED_DIR / "posted_videos.json"
HISTORY_PATH = CONTEXT_DIR / "history.md"
PERSONALITY_PATH = CONTEXT_DIR / "personalidad.md"
PROFILES_JSON_PATH = CONTEXT_DIR / "perfiles.json"
TIMELINE_PATH = CONTEXT_DIR / "timeline.md"
SPOTIFY_CACHE_PATH = CONTEXT_DIR / ".spotify_cache"
SCHEDULER_STATE_PATH = STATE_DIR / "scheduler_state.json"

# Announcement message pools -- loaded once at startup.
# Files live in prompts/; each is a JSON array of strings.
_HELLO_MESSAGES: list[str] = _load_json(
    PROMPTS_DIR / "hello.json",
    default=["Bip bip, estoy activo de vuelta"],
    allow_trailing_commas=True,
)
_BURNED_MESSAGES: list[str] = _load_json(
    PROMPTS_DIR / "burned.json",
    default=["Limite alcanzado por hoy, chau"],
    allow_trailing_commas=True,
)


# ── Prompt loader ──────────────────────────────────────────────────────────────

class _SafeDict(dict):
    """dict subclass that returns {key} unchanged on missing keys.

    Lets us call str.format_map() on prompt templates without crashing if a
    placeholder isn't supplied -- the literal '{key}' stays in the output."""

    def __missing__(self, key):
        return "{" + key + "}"


_PROMPT_CACHE: dict[str, str] = {}


def load_prompt(name: str, **kwargs) -> str:
    """
    Read a prompt template from /prompts/<name>.md and apply optional
    placeholder substitution via str.format_map.

    Args:
        name: Filename without extension. e.g. "reflect_video".
        **kwargs: Values for any {placeholder} in the template.

    Returns:
        The prompt text, with placeholders substituted.
    """
    if name not in _PROMPT_CACHE:
        path = PROMPTS_DIR / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        _PROMPT_CACHE[name] = path.read_text(encoding="utf-8")

    text = _PROMPT_CACHE[name]
    if kwargs:
        # SafeDict avoids KeyError when the template happens to contain
        # unrelated curly braces.
        text = text.format_map(_SafeDict(**kwargs))
    return text


# ── Structured output models ──────────────────────────────────────────────────
#
# These models replace fragile "NADA" / bullet-string parsing in extraction
# functions. Creative / generative functions (generate_reply, summarize_news,
# etc.) intentionally stay as free text -- wrapping them in JSON would add
# overhead with no benefit.


class BioInterpretation(BaseModel):
    """Concrete facts extracted from a Bluesky user's bio.

    Empty list means the bio contained nothing worth persisting.
    """
    facts: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete, durable facts about the user in third person. "
            "Max 3 items. Empty list if nothing valid was found."
        ),
    )


class ProfileUpdate(BaseModel):
    """New self-disclosed facts found in a conversation thread.

    Empty list means the thread revealed nothing new about the user.
    """
    facts: list[str] = Field(
        default_factory=list,
        description=(
            "New facts about the user extracted from the thread. "
            "Third person. Max 3 items. Empty list if nothing new."
        ),
    )


class MemoryExtraction(BaseModel):
    """Data extracted from a /remember command.

    Empty list means the command was invalid, spam, or contained no
    persistable information.
    """
    facts: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete facts to remember: dates, names, places, preferences. "
            "Third person. Max 3 items. Empty list if the input is invalid."
        ),
    )


class LessonsUpdate(BaseModel):
    """New behavioral lessons derived from recent bot interactions.

    Empty list means no new patterns worth crystallizing were found.
    """
    lessons: list[str] = Field(
        default_factory=list,
        description=(
            "New behavioral lessons: repeated patterns, dynamics with specific "
            "users, concrete errors to avoid. Max 3 items. "
            "Empty list if nothing new beyond existing lessons."
        ),
    )


# ── OpenRouter credit helpers ──────────────────────────────────────────────────

def fetch_credits_data() -> Optional[dict]:
    """
    Query OpenRouter for the current credit/usage figures.

    Returns:
        Dict with keys 'total_credits' and 'total_usage' (both float USD),
        or None on network/auth failure.
    """
    try:
        r = requests.get(
            f"{OPENAI_ENDPOINT.rstrip('/')}/credits",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", {}) or {}
        return {
            "total_credits": float(data.get("total_credits", 0) or 0),
            "total_usage": float(data.get("total_usage", 0) or 0),
        }
    except Exception as e:
        print(f"[credits] no se pudo consultar OpenRouter: {e}")
        return None


def get_total_usage_usd() -> Optional[float]:
    """Return current cumulative usage in USD (no cache -- always fresh)."""
    data = fetch_credits_data()
    return data["total_usage"] if data else None


# ── Pipeline scheduler ─────────────────────────────────────────────────────────
#
# Tracks per-pipeline daily completion + retry backoff and the global "burn"
# state for the daily reply cap. Persisted to disk so the bot survives restarts.

PIPELINES = ("news", "video", "update_history", "reflection", "timeline",
             "timeline_reflection", "music", "image")

# Hour of day (24h, local time) at which each pipeline is allowed to start.
# Earlier than this, the scheduler will not attempt to run it. Once that hour
# passes, the scheduler will retry on backoff until it succeeds or the day ends.
# NOTE: update_history (16h) must always precede reflection (17h) since
# reflection reads today's history-update result from the scheduler state.
PIPELINE_HOURS = {
    "news": 14,
    "video": 13,
    "update_history": 16,    # must run before reflection
    "reflection": 17,
    "timeline": 19,
    "timeline_reflection": 20,
    "music": 22,
    "image": 15,
}

# How many times each pipeline runs per day.
# 0 = disabled for the day. N = run N times, auto-spaced across the available window.
# Loaded from settings.json["PIPELINE_DAILY_RUNS"]; defaults to 1 for every pipeline.
_PIPELINE_DAILY_RUNS_RAW: dict = SETTINGS.get("PIPELINE_DAILY_RUNS", {})
PIPELINE_DAILY_RUNS: dict[str, int] = {
    p: int(_PIPELINE_DAILY_RUNS_RAW.get(p, 1)) for p in PIPELINES
}

# Minimum gap between runs of the same pipeline (seconds).
# Prevents a misconfigured high target from firing in rapid succession.
_MIN_SPACING_S: int = 300  # 5 minutes

# Exponential backoff schedule (in seconds) for failed pipeline attempts.
# After exhausting the list, the last value is reused indefinitely until midnight.
BACKOFF_SCHEDULE_S = [60, 5 * 60, 15 * 60, 45 * 60, 2 * 3600]


def _default_pipeline_state() -> dict:
    """Return a clean per-day state skeleton for all pipelines."""
    return {
        p: {
            "done": False,
            "runs_today": 0,        # how many successful runs completed today
            "attempts": 0,          # failed attempts since last success (for backoff)
            "next_attempt_ts": 0.0, # earliest timestamp for next attempt (backoff or spacing)
            "last_error": None,
        }
        for p in PIPELINES
    }


def load_scheduler_state() -> dict:
    """Load persistent scheduler state, returning a fresh skeleton if missing."""
    _default = {
        "today": str(date.today()),
        "pipelines": _default_pipeline_state(),
        "burn": {"state": "active", "since": None, "announced_today": False,
                 "wake_announced_today": False,
                 "usage_snapshot_at_day_start": None},
        # Tracks the result of the last successful history-update run so that
        # the reflection pipeline knows (a) whether it ran today and (b) what
        # new lessons were generated (avoids re-reflecting on stale content).
        "last_history_update": {"date": None, "new_lessons": None},
    }

    if not SCHEDULER_STATE_PATH.exists():
        return _default

    try:
        state = json.loads(SCHEDULER_STATE_PATH.read_text(encoding="utf-8"))
        # Migrate older state files that predate last_history_update
        state.setdefault("last_history_update", {"date": None, "new_lessons": None})
        # Migrate older state files: ensure every known pipeline has a full entry
        for p in PIPELINES:
            state["pipelines"].setdefault(
                p, {"done": False, "runs_today": 0, "attempts": 0,
                    "next_attempt_ts": 0.0, "last_error": None}
            )
            # Migrate entries that predate runs_today
            state["pipelines"][p].setdefault("runs_today", 0)
        return state
    except Exception:
        # Corrupted state file -- start clean, don't crash the bot
        print("[state] scheduler_state.json corrupto, regenerando")
        return _default


def save_scheduler_state(state: dict) -> None:
    SCHEDULER_STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def maybe_roll_day(state: dict) -> bool:
    """
    Reset per-day fields if the calendar date changed.

    On a new day we also snapshot OpenRouter's cumulative total_usage so
    that today's spend can be measured as usage_now - snapshot. If the
    credits endpoint is unreachable at the moment of rollover we leave the
    snapshot as None and ensure_usage_snapshot() will retry on later loops.

    Returns:
        True if the day changed (caller may want to trigger a wake-up post).
    """
    today_str = str(date.today())
    if state.get("today") != today_str:
        state["today"] = today_str
        state["pipelines"] = _default_pipeline_state()
        # Reset daily flags but keep burn.state -- wake-up depends on whether
        # the new daily budget has been spent, evaluated on next is_burned()
        state["burn"]["announced_today"] = False
        state["burn"]["wake_announced_today"] = False
        # Fresh usage snapshot for the new day. If this fails (None) we'll
        # retry on every subsequent loop until it succeeds.
        state["burn"]["usage_snapshot_at_day_start"] = get_total_usage_usd()
        return True
    return False


def ensure_usage_snapshot(state: dict) -> None:
    """
    Make sure we have a usage snapshot for today. Called on every loop so
    that if the credits endpoint was down at midnight, we eventually get
    a baseline once it recovers.
    """
    if state["burn"].get("usage_snapshot_at_day_start") is None:
        snapshot = get_total_usage_usd()
        if snapshot is not None:
            state["burn"]["usage_snapshot_at_day_start"] = snapshot
            save_scheduler_state(state)
            print(f"[burn] snapshot inicial del dia: ${snapshot:.4f}")


def next_backoff_seconds(attempts: int) -> int:
    idx = min(attempts, len(BACKOFF_SCHEDULE_S) - 1)
    return BACKOFF_SCHEDULE_S[idx]


def try_run_pipeline(name: str, state: dict, client: Client, or_client: OpenAI) -> None:
    """
    Attempt to run a pipeline if it's due.

    Supports multiple runs per day via PIPELINE_DAILY_RUNS:
      - 0  -> pipeline disabled for the day (marked done immediately)
      - 1  -> classic single-run behaviour
      - N  -> run N times, auto-spacing remaining runs across the rest of the day

    Spacing logic: after each successful run, the remaining window until midnight
    is divided evenly among the runs still pending. Minimum gap is _MIN_SPACING_S.

    On failure, schedules an exponential backoff retry (independent of spacing).
    Persists state on every attempt.
    """
    pstate = state["pipelines"][name]
    target_runs = PIPELINE_DAILY_RUNS.get(name, 1)

    # Pipeline disabled for today -- mark done and bail out silently
    if target_runs == 0:
        if not pstate["done"]:
            pstate["done"] = True
            save_scheduler_state(state)
        return

    # All scheduled runs for today already completed
    if pstate["done"]:
        return

    now = datetime.now()

    # Don't start before the configured hour-of-day
    if now.hour < PIPELINE_HOURS[name]:
        return

    # Respect backoff (after failures) or inter-run spacing (after successes)
    if pstate["next_attempt_ts"] and time.time() < pstate["next_attempt_ts"]:
        return

    func = PIPELINE_FUNCS[name]
    try:
        run_number = pstate["runs_today"] + 1
        print(f"[scheduler] corriendo {name} (run {run_number}/{target_runs})")
        func(client, or_client)

        # Success: increment counter and reset backoff
        pstate["runs_today"] += 1
        pstate["attempts"] = 0
        pstate["last_error"] = None

        if pstate["runs_today"] >= target_runs:
            # All runs for today completed
            pstate["done"] = True
            pstate["next_attempt_ts"] = 0.0
            print(f"[scheduler] {name} completado ({pstate['runs_today']}/{target_runs} runs)")
        else:
            # Schedule next run: divide remaining window evenly among pending runs
            runs_left = target_runs - pstate["runs_today"]
            midnight = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            secs_until_midnight = max(0.0, (midnight - datetime.now()).total_seconds())
            spacing = max(float(_MIN_SPACING_S), secs_until_midnight / runs_left)
            pstate["next_attempt_ts"] = time.time() + spacing
            print(
                f"[scheduler] {name} run {pstate['runs_today']}/{target_runs} OK. "
                f"Proximo en {spacing / 60:.0f} min"
            )

    except Exception as e:
        pstate["attempts"] += 1
        pstate["last_error"] = str(e)
        backoff = next_backoff_seconds(pstate["attempts"] - 1)
        pstate["next_attempt_ts"] = time.time() + backoff
        print(
            f"[scheduler] {name} fallo (intento {pstate['attempts']}): {e}. "
            f"Reintento en {backoff}s"
        )
    finally:
        save_scheduler_state(state)


# ── Spotify ────────────────────────────────────────────────────────────────────

def get_random_track_from_playlist(
    playlist_id: str,
    sp_client_id: str = SPOTIFY_CLIENT_ID,
    sp_client_secret: str = SPOTIFY_CLIENT_SECRET,
) -> dict | None:
    """
    Pick a random track from a Spotify playlist.
    Uses OAuth user auth to support both public and private playlists.
    """
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=sp_client_id,
        client_secret=sp_client_secret,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="playlist-read-private playlist-read-collaborative",
        # Cache prevents re-prompting login on every run
        cache_path=str(SPOTIFY_CACHE_PATH),
    ))

    tracks = []
    offset = 0
    while True:
        result = sp.playlist_items(
            playlist_id,
            offset=offset,
            limit=100,
            additional_types=["track"],
        )
        items = result.get("items", [])
        if not items:
            break
        tracks.extend([
            item["item"] for item in items
            if item.get("item") and item["item"].get("id")
        ])
        if not result.get("next"):
            break
        offset += 100

    if not tracks:
        return None

    track = random.choice(tracks)
    artist_id = track["artists"][0]["id"]
    artist_name = track["artists"][0]["name"]

    # Genres live on the artist, not the track
    artist_data = sp.artist(artist_id)
    genres = artist_data.get("genres", [])

    return {
        "title": track["name"],
        "artist": artist_name,
        "album": track["album"]["name"],
        "genres": genres[:3],
        "url": track["external_urls"]["spotify"],
    }


# ── Bluesky feed helpers ───────────────────────────────────────────────────────

def get_all_my_posts(client: Client, handle: str, limit: int = 100) -> list[dict]:
    """Fetch all posts and replies from a Bluesky profile (paginated)."""
    posts = []
    cursor = None

    while True:
        params = {"actor": handle, "limit": limit, "filter": "posts_with_replies"}
        if cursor:
            params["cursor"] = cursor

        response = client.app.bsky.feed.get_author_feed(params)

        for item in response.feed:
            reply_to = None
            if item.reply and item.reply.parent:
                parent = item.reply.parent
                # Parent can be NotFoundPost / BlockedPost / DeletedPost --
                # those objects lack .author and .record, so guard before access.
                if hasattr(parent, "author") and hasattr(parent, "record"):
                    reply_to = {
                        "author": parent.author.handle,
                        "text": parent.record.text,
                    }

            posts.append({
                "uri": item.post.uri,
                "cid": item.post.cid,
                "text": item.post.record.text,
                "indexed_at": item.post.indexed_at,
                "reply_count": item.post.reply_count,
                "like_count": item.post.like_count,
                "repost_count": item.post.repost_count,
                "reply_to": reply_to,
            })

        cursor = getattr(response, "cursor", None)
        if not cursor:
            break

    return posts


def get_latest_mentions(client: Client, limit: int = 20) -> list[dict]:
    """Pull recent mention/reply/quote notifications from Bluesky."""
    response = client.app.bsky.notification.list_notifications({"limit": limit})

    results = []
    for notif in response.notifications:
        if notif.reason not in ("mention", "reply", "quote"):
            continue

        record = notif.record

        # Extract thread root reference if this post is itself a reply
        reply_ref = getattr(record, "reply", None)
        root_uri = notif.uri
        root_cid = notif.cid
        if reply_ref and getattr(reply_ref, "root", None):
            root_uri = reply_ref.root.uri
            root_cid = reply_ref.root.cid

        results.append({
            "reason": notif.reason,
            "author": notif.author.handle,
            "author_did": notif.author.did,
            "display_name": notif.author.display_name,
            "text": getattr(record, "text", ""),
            "embed": getattr(record, "embed", None),
            "indexed_at": notif.indexed_at,
            "is_read": notif.is_read,
            "uri": notif.uri,
            "cid": notif.cid,
            "root_uri": root_uri,
            "root_cid": root_cid,
        })

    return results


def has_image_embed(embed) -> bool:
    """Detect whether a post embed contains images."""
    if embed is None:
        return False
    embed_type = (
        getattr(embed, 'py_type', None)
        or getattr(embed, '$type', None)
        or ''
    )
    return 'image' in str(embed_type).lower()


def get_image_urls_from_embed(embed, author_did: str) -> list[str]:
    """Extract CDN image URLs from a Bluesky image embed."""
    if embed is None:
        return []

    embed_type = getattr(embed, 'py_type', '') or getattr(embed, '$type', '')

    # Direct image embeds
    if 'images' in str(embed_type).lower():
        images = getattr(embed, 'images', []) or []
        urls = []
        for img in images:
            blob = getattr(img, 'image', None)
            if blob:
                cid = getattr(getattr(blob, 'ref', None), 'link', None)
                if cid:
                    urls.append(
                        f"https://cdn.bsky.app/img/feed_fullsize/plain/{author_did}/{cid}@jpeg"
                    )
        return urls

    # recordWithMedia (quote post with image)
    media = getattr(embed, 'media', None)
    if media:
        return get_image_urls_from_embed(media, author_did)

    return []


def describe_image(
    or_client: OpenAI,
    image_urls: list[str],
    model: str = IMAGE_MODEL,
) -> str:
    """Use a vision model to describe images from a Bluesky post."""
    # Short instruction kept inline because it's a simple, fixed system message.
    # If you ever want to externalize it, drop a prompts/describe_image.md file.
    instruction = "Describi brevemente que ves en esta imagen. Se conciso y objetivo."

    content = [{"type": "text", "text": instruction}]
    for url in image_urls[:4]:  # Bluesky allows max 4 images per post
        content.append({"type": "image_url", "image_url": {"url": url}})

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": content}]
    )
    return response.choices[0].message.content or "imagen sin descripcion"


def get_thread(client: Client, uri: str) -> list[dict]:
    """Fetch all posts in a conversation thread given a post URI."""
    response = client.app.bsky.feed.get_post_thread({"uri": uri})
    thread = response.thread

    posts = []

    def walk(node):
        if not hasattr(node, "post"):
            return
        posts.append({
            "author": node.post.author.handle,
            "display_name": node.post.author.display_name,
            "text": node.post.record.text,
            "indexed_at": node.post.indexed_at,
            "uri": node.post.uri,
        })
        for reply in getattr(node, "replies", []) or []:
            walk(reply)

    walk(thread)
    return posts


def get_parent_chain(client: Client, uri: str) -> list[dict]:
    """Fetch the chain of parent posts leading to a given post (root first)."""
    response = client.app.bsky.feed.get_post_thread({"uri": uri, "parentHeight": 100})
    thread = response.thread

    chain = []

    def walk_up(node):
        parent = getattr(node, "parent", None)
        if parent and hasattr(parent, "post"):
            walk_up(parent)
        if not hasattr(node, "post"):
            return
        chain.append({
            "author": node.post.author.handle,
            "text": node.post.record.text,
        })

    walk_up(thread)
    return chain


def chain_to_string(chain: list[dict]) -> str:
    """Flatten a parent chain into 'author: text' lines."""
    return "\n".join(f"{post['author']}: {post['text']}" for post in chain)


def reply_to_post(
    client: Client, uri: str, cid: str, text: str,
    root_uri: Optional[str] = None, root_cid: Optional[str] = None,
) -> None:
    """Send a reply, falling back to self-root when none provided."""
    actual_root_uri = root_uri or uri
    actual_root_cid = root_cid or cid

    client.send_post(
        text=text,
        reply_to={
            "root": {"uri": actual_root_uri, "cid": actual_root_cid},
            "parent": {"uri": uri, "cid": cid},
        }
    )


# ── LLM helpers ────────────────────────────────────────────────────────────────

def debugger_choice(response):
    """Log when the model returns empty content (helps diagnose finish reasons)."""
    choice = response.choices[0]
    if not choice.message.content:
        print(f"devolvio None -- finish_reason: {choice.finish_reason}")
        return None


def generate_reply(
    or_client: OpenAI,
    system_prompt: str,
    thread_str: str,
    model: str = REASONING_MODEL,
) -> str | None:
    """Generate a reply using the LLM given a system prompt and thread context."""
    response = or_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": thread_str},
        ],
    )
    debugger_choice(response)
    return response.choices[0].message.content or None


# ── Profile management ─────────────────────────────────────────────────────────

def get_user_bio(client: Client, handle: str) -> str | None:
    """Fetch the bio/description from a Bluesky user's public profile."""
    try:
        profile = client.get_profile(actor=handle)
        return getattr(profile, 'description', None)
    except Exception as e:
        print(f"error al obtener bio de {handle}: {e}")
        return None


def interpret_bio(
    or_client,
    handle: str,
    bio: str,
    model: str = LITE_MODEL,
) -> str | None:
    """Extract concrete facts from a Bluesky bio using structured output.

    Returns a markdown bullet string ready to append to the profile,
    or None if no valid facts were found.
    """
    prompt = load_prompt("interpret_bio_prompt", handle=handle)

    try:
        result: BioInterpretation = structured_client.chat.completions.create(
            model=model,
            max_tokens=150,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": bio},
            ],
            response_model=BioInterpretation,
        )
        if not result.facts:
            return None
        # Reconstruct bullet markdown so callers receive the same format as before
        return "\n".join(f"- {fact}" for fact in result.facts)
    except Exception as e:
        print(f"error interpretando bio de {handle}: {e}")
        return None


def ensure_user_profile_exists(
    bsky_client: Client,
    or_client: OpenAI,
    author_handle: str,
    model: str = REASONING_MODEL,
) -> None:
    """Create a baseline .md profile for the user if one doesn't already exist."""
    profiles = json.loads(PROFILES_JSON_PATH.read_text(encoding="utf-8"))

    if author_handle in profiles:
        profile_path = USERS_DIR / profiles[author_handle]
        if profile_path.exists():
            return

    bio = get_user_bio(bsky_client, author_handle)

    bio_section = ""
    if bio and bio.strip():
        bio_interp = interpret_bio(or_client, author_handle, bio, model)
        bio_section = f"\n\n## Bio (Bluesky)\n\n> {bio}"
        if bio_interp:
            bio_section += f"\n\n{bio_interp}"

    filename = f"{author_handle.replace('.', '_')}.md"
    profile_path = USERS_DIR / filename

    base_profile = (
        f"# Perfil Bluesky: @{author_handle}\n\n"
        f"## Datos basicos\n\n"
        f"| Campo | Detalle |\n|---|---|\n"
        f"| Handle | @{author_handle} |"
        f"{bio_section}\n\n"
        f"---\n\n"
        f"## Notas\n\n"
        f"> Perfil generado automaticamente."
    )

    profile_path.write_text(base_profile, encoding="utf-8")

    profiles[author_handle] = filename
    PROFILES_JSON_PATH.write_text(
        json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"perfil base creado para {author_handle}")


def load_user_profile(author_handle: str) -> str | None:
    """Load a user's profile .md content."""
    profiles = json.loads(PROFILES_JSON_PATH.read_text(encoding="utf-8"))

    filename = profiles.get(author_handle)
    if not filename:
        return None

    profile_path = USERS_DIR / filename
    if not profile_path.exists():
        return None

    return profile_path.read_text(encoding="utf-8")


def load_system_prompt() -> str:
    """Load the personality file used as the base system prompt for replies."""
    return PERSONALITY_PATH.read_text(encoding="utf-8")


def update_user_profile(
    or_client,
    author_handle: str,
    thread_str: str,
    reply_text: str,
    model: str = LITE_MODEL,
) -> None:
    """Extract new self-disclosed facts from a thread and append to the user's profile."""
    profiles = json.loads(PROFILES_JSON_PATH.read_text(encoding="utf-8"))
    current_profile = load_user_profile(author_handle)

    extraction_prompt = load_prompt("update_user_prompt", author_handle=author_handle)

    user_content = (
        f"## Perfil actual\n{current_profile or 'Sin perfil previo.'}\n\n"
        f"## Hilo (solo presta atencion a lo que dice @{author_handle})\n{thread_str}\n\n"
        f"## Respuesta del bot (IGNORAR para extraccion)\nbot: {reply_text}"
    )

    result: ProfileUpdate = structured_client.chat.completions.create(
        model=model,
        max_tokens=200,
        messages=[
            {"role": "system", "content": extraction_prompt},
            {"role": "user", "content": user_content},
        ],
        response_model=ProfileUpdate,
    )

    if not result.facts:
        print(f"update_user_profile: nada nuevo para {author_handle}")
        return

    new_facts_md = "\n".join(f"- {fact}" for fact in result.facts)

    filename = profiles[author_handle]
    profile_path = USERS_DIR / filename
    updated = current_profile or ""

    if "## Memoria" in updated:
        updated += "\n" + new_facts_md
    else:
        updated += "\n\n## Memoria\n" + new_facts_md

    profile_path.write_text(updated, encoding="utf-8")
    print(f"perfil de {author_handle} actualizado con: {new_facts_md}")


# ── Persistence ────────────────────────────────────────────────────────────────

def save_respondido(respondido: list[str]) -> None:
    RESPONDIDO_PATH.write_text(
        json.dumps(respondido, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_respondido() -> list[str]:
    if not RESPONDIDO_PATH.exists():
        return []
    return json.loads(RESPONDIDO_PATH.read_text(encoding="utf-8"))


# ── Reflection / lessons ───────────────────────────────────────────────────────

def reflect_on_history(
    or_client: OpenAI,
    new_lessons: str | None = None,
    model: str = REASONING_MODEL,
) -> str | None:
    """
    Generate a first-person reflection on the bot's own history.

    If new_lessons is provided (the text added by today's update_history run),
    the reflection is focused on those fresh additions rather than the full file.
    This prevents the bot from posting identical reflections on days when nothing
    changed in history.md.

    If new_lessons is None, falls back to reading the complete history.md
    (backward-compatible behaviour for manual / forced calls).
    """
    personality = PERSONALITY_PATH.read_text(encoding="utf-8")
    extraction_prompt = load_prompt("auto_reflect_prompt")

    # Prefer the specific new lessons; fall back to full history only when needed
    content = new_lessons if new_lessons else HISTORY_PATH.read_text(encoding="utf-8")

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=600,
        messages=[
            {"role": "system", "content": personality + "\n\n" + extraction_prompt},
            {"role": "user", "content": content},
        ],
    )
    debugger_choice(response)
    return response.choices[0].message.content


def maybe_update_lessons(
    or_client,
    model: str = REASONING_MODEL,
) -> str | None:
    """Analyze recent history and append new behavioral lessons to history.md.

    Returns:
        Markdown bullet string of the new lessons if any were added,
        or None if the model found nothing worth crystallizing.
    """
    history_file = HISTORY_PATH
    history = history_file.read_text(encoding="utf-8")
    personality = PERSONALITY_PATH.read_text(encoding="utf-8")

    # Extract existing lessons so the LLM doesn't repeat them
    existing_lessons = ""
    if "## Lecciones destiladas" in history:
        start = history.index("## Lecciones destiladas") + len("## Lecciones destiladas")
        rest = history[start:]
        end = rest.find("\n## ")
        existing_lessons = rest[:end].strip() if end != -1 else rest.strip()

    prompt = load_prompt(
        "update_lessons_prompt",
        existing_lessons=existing_lessons or "Ninguna aun.",
    )

    try:
        result: LessonsUpdate = structured_client.chat.completions.create(
            model=model,
            max_tokens=300,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": f"## Personalidad actual\n{personality}\n\n## Historial completo\n{history}",
                },
            ],
            response_model=LessonsUpdate,
        )

        if not result.lessons:
            print("lecciones: sin cambios")
            return None

        new_lessons_md = "\n".join(f"- {lesson}" for lesson in result.lessons)

        # Inject new lessons into the existing section, or create it
        if "## Lecciones destiladas" in history:
            section_start = history.index("## Lecciones destiladas")
            after_header = section_start + len("## Lecciones destiladas\n")
            rest = history[after_header:]
            next_section = rest.find("\n## ")

            if next_section != -1:
                insert_pos = after_header + next_section
                updated = history[:insert_pos] + "\n" + new_lessons_md + history[insert_pos:]
            else:
                updated = history + "\n" + new_lessons_md
        else:
            # Section doesn't exist yet -- insert after the title line
            lines = history.split("\n")
            insert_after = 0
            for i, line in enumerate(lines):
                if line.startswith("# Historial"):
                    insert_after = i + 1
                    break
            lines.insert(
                insert_after + 1,
                "\n## Lecciones destiladas\n\n" + new_lessons_md + "\n"
            )
            updated = "\n".join(lines)

        history_file.write_text(updated, encoding="utf-8")
        print(f"lecciones actualizadas: {new_lessons_md}")
        return new_lessons_md

    except Exception as e:
        print(f"error al actualizar lecciones: {e}")
        return None


def truncate_to_graphemes(text: str, limit: int = 300) -> str:
    """Truncate a string to a maximum number of graphemes (Bluesky's post limit)."""
    graphemes = list(text)
    if len(graphemes) <= limit:
        return text
    return "".join(graphemes[:limit])


# ── News pipeline ──────────────────────────────────────────────────────────────

def fetch_rss(url: str, max_items: int = 15) -> list[dict]:
    """Fetch titles and descriptions from an RSS feed."""
    feed = feedparser.parse(url)
    return [
        {
            "title": entry.get("title", "").strip(),
            "description": entry.get("summary", "").strip(),
        }
        for entry in feed.entries[:max_items]
    ]


def fetch_news() -> list[dict]:
    """Fetch latest news from configured news sources."""
    all_items: list[dict] = []
    for url in NEWS_SITES:
        # Tag each item with the host as a lightweight source label
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.replace("www.", "")
        except Exception:
            host = url
        try:
            items = fetch_rss(url)
        except Exception as e:
            print(f"[news] error fetching {url}: {e}")
            continue
        for it in items:
            it["source"] = host
        all_items.extend(items)
    return all_items


def summarize_news(
    or_client: OpenAI,
    news: list[dict],
    model: str = LITE_MODEL,
) -> str | None:
    """Use an LLM to comment on today's news in the bot's voice."""
    personality = PERSONALITY_PATH.read_text(encoding="utf-8")

    news_text = "\n\n".join(
        f"[{item['source']}] {item['title']}\n{item['description']}"
        for item in news
    )

    prompt = load_prompt("summarize_news_prompt")

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": personality + "\n\n" + prompt},
            {"role": "user", "content": news_text},
        ],
    )

    debugger_choice(response)
    return response.choices[0].message.content


def run_news_pipeline(client: Client, or_client: OpenAI) -> None:
    """Fetch news, generate a comment in bot voice, and post it to Bluesky."""
    news = fetch_news()
    if not news:
        raise RuntimeError("fetch_news devolvio vacio")
    comentario = summarize_news(or_client, news)
    if not comentario:
        raise RuntimeError("summarize_news devolvio None")
    comentario = "El pais: " + truncate_to_graphemes(comentario)
    client.send_post(comentario)
    print(f"noticias posteadas: {comentario}")


# ── History-update pipeline ────────────────────────────────────────────────────

def run_history_update_pipeline(client: Client, or_client: OpenAI) -> None:
    """
    Run maybe_update_lessons and persist the result in the scheduler state so
    that run_reflection_pipeline can later reflect only on what changed today.

    In HISTORY_UPDATE_COMPULSIVE mode (default) the pipeline always succeeds and
    saves its result (even when the LLM finds nothing new -- that's still useful
    information for reflection: "nothing to reflect on today").

    In non-compulsive mode the pipeline also always completes successfully;
    the distinction is only whether new_lessons is non-None in the state.
    """
    new_lessons = maybe_update_lessons(or_client)

    # Persist the result so reflection (and any other pipeline that wants it)
    # can read it from state without re-running the LLM.
    scheduler_state["last_history_update"] = {
        "date": str(date.today()),
        "new_lessons": new_lessons,   # None means "nothing new today"
    }
    save_scheduler_state(scheduler_state)

    if new_lessons:
        print(f"[history] lecciones del día guardadas en state ({len(new_lessons)} chars)")
    else:
        if HISTORY_UPDATE_COMPULSIVE:
            print("[history] compulsivo: sin lecciones nuevas, state actualizado de todas formas")
        else:
            print("[history] no-compulsivo: el modelo no encontró lecciones nuevas")


# ── Self-reflection pipeline ───────────────────────────────────────────────────

def run_reflection_pipeline(client: Client, or_client: OpenAI) -> None:
    """
    Post a personal reflection based on today's history update.

    If HISTORY_REFLECTION_REQUIRES_UPDATE is True (default), the pipeline will
    raise RuntimeError when update_history hasn't run yet today -- the scheduler
    backoff will keep retrying until it has. Once it does, the reflection is
    focused on the new lessons rather than the full stale history.

    If HISTORY_REFLECTION_REQUIRES_UPDATE is False, falls back to reflecting on
    the full history.md (old behaviour, useful for quick testing).
    """
    today_str = str(date.today())
    history_update = scheduler_state.get("last_history_update", {})
    update_date = history_update.get("date")
    new_lessons = history_update.get("new_lessons")

    if HISTORY_REFLECTION_REQUIRES_UPDATE and update_date != today_str:
        # update_history hasn't completed today yet -- defer until it does
        raise RuntimeError(
            f"update_history no completó hoy (último: {update_date}). "
            "Postergando reflection hasta que esté disponible."
        )

    # If update ran today but found nothing new, skip posting (nothing to say)
    if update_date == today_str and new_lessons is None and HISTORY_REFLECTION_REQUIRES_UPDATE:
        print("[reflection] update_history no encontró lecciones nuevas hoy, saltando post")
        return

    # Pass today's new lessons if available; otherwise fall back to full history
    lessons_to_reflect = new_lessons if update_date == today_str else None
    reflexion = reflect_on_history(or_client, new_lessons=lessons_to_reflect)
    if not reflexion:
        raise RuntimeError("reflect_on_history devolvio None")

    reflexion = truncate_to_graphemes(reflexion)
    client.send_post(reflexion)
    print(f"reflexion posteada: {reflexion}")


# ── History rebuild (manual, not scheduled) ────────────────────────────────────

# Embedded prompt so the rebuild command works without a prompts/ file.
# It instructs the model to produce history.md in the exact canonical format.
_REBUILD_HISTORY_PROMPT = """\
Sos maripobot reconstruyendo tu propio historial desde tus posts en Bluesky.

Te van a dar un log cronológico de todo lo que posteaste, con contexto de a quién
respondías cuando corresponda, y las métricas de reacción.

Tu tarea es sintetizar ese log en un archivo history.md con el siguiente formato EXACTO.
No inventes eventos que no estén en el log. No omitas nada significativo.

---

# Historial de Maripobot

---

## Lecciones destiladas

> Esta sección es la más importante. Resume lo que aprendí de mis propias interacciones.
> Se actualiza en cada reflexión diaria.

[bullets de lecciones de comportamiento extraídas de los eventos. Máximo 10.
Solo patrones que se repitieron o errores con implicancia real, no eventos aislados.]

---

## Eventos por etapa

[Para cada grupo de días o etapa, un sub-bloque así:]

### YYYY-MM-DD — [título corto que describa la etapa]

[eventos en bullets, uno por línea, con tag entre corchetes:]
*[IDENTIDAD]* descripción breve
*[COMUNIDAD]* descripción breve
*[TÉCNICO]* descripción breve
*[LÍMITES]* descripción breve
*[LORE]* descripción breve
*[MEMORIA]* descripción breve

[Usá el tag que corresponda. Un evento puede combinar dos tags: *[IDENTIDAD - FUNDACIONAL]*]
[Priorizá eventos con muchos likes o replies — tuvieron impacto real.]
[Agrupá días sin eventos significativos en un solo bloque.]

---

## Interacciones fallidas o problemáticas

> Para no repetir errores.

[bullets de errores concretos, malentendidos, o patrones que no funcionaron]

---

## Cumpleaños registrados

| Usuario | Fecha |
|---|---|
[filas de la tabla con los cumpleaños que se mencionan explícitamente en el log]

---

IMPORTANTE:
- Escribí en primera persona cuando sea necesario, pero el historial es objetivo.
- No incluyas posts proactivos del bot (imágenes, noticias, videos, música) como "eventos" —
  esos son outputs automáticos, no interacciones. Solo incluí respuestas a usuarios y posts
  propios que generen interacción notable.
- El archivo debe ser funcional como memoria operativa: conciso, sin paja.
"""


def rebuild_history(
    client: Client,
    or_client: OpenAI,
    model: str = REASONING_MODEL,
    dry_run: bool = False,
    batch_days: int = 0,
) -> None:
    """
    Reconstruct history.md from scratch using all the bot's Bluesky posts.

    Steps:
      1. Fetch all bot posts via get_all_my_posts (paginated).
      2. Build a plain-text chronological log grouped by day, including reply
         context from reply_to data already embedded in each post.
      3. If batch_days > 0, call the LLM once per batch (useful when post count
         is large); otherwise send everything in one shot.
      4. Final LLM call synthesizes the canonical history.md format.
      5. Writes to history.md, backing up the current file first.

    Not part of the scheduler. Invoke via:
        python maripobot.py --rebuild-history
        python maripobot.py --rebuild-history --dry-run        # preview only
        python maripobot.py --rebuild-history --batch-days 7   # 7-day windows
    """
    print("[rebuild] buscando posts del bot en Bluesky...")
    posts = get_all_my_posts(client, BOT_HANDLE, limit=100)

    if not posts:
        print("[rebuild] no se encontraron posts del bot")
        return

    print(f"[rebuild] {len(posts)} posts encontrados")

    # ── 1. Build chronological log ────────────────────────────────────────────

    # Sort oldest-first so the narrative reads naturally
    posts_sorted = sorted(posts, key=lambda p: p["indexed_at"])

    # Group by calendar day
    by_date: dict[str, list[dict]] = {}
    for p in posts_sorted:
        day = p["indexed_at"][:10]
        by_date.setdefault(day, []).append(p)

    def _format_day_block(day: str, day_posts: list[dict]) -> str:
        """Format one day's posts as readable lines for the LLM."""
        lines = [f"\n### {day}"]
        for p in day_posts:
            # Reply context from the already-fetched reply_to data
            ctx = ""
            if p.get("reply_to"):
                author = p["reply_to"]["author"]
                snippet = p["reply_to"]["text"][:100].replace("\n", " ")
                ctx = f'[respuesta a @{author}: "{snippet}"] → '

            reactions = f"[❤{p['like_count']} 🔁{p['repost_count']} 💬{p['reply_count']}] "
            lines.append(f"- {reactions}{ctx}{p['text']}")
        return "\n".join(lines)

    personality = PERSONALITY_PATH.read_text(encoding="utf-8")

    # ── 2. Decide whether to batch or send all at once ────────────────────────

    sorted_days = sorted(by_date.keys())

    if batch_days > 0:
        # Chunk days into windows and get a per-batch summary first
        day_chunks: list[list[str]] = []
        chunk: list[str] = []
        for day in sorted_days:
            chunk.append(day)
            if len(chunk) >= batch_days:
                day_chunks.append(chunk)
                chunk = []
        if chunk:
            day_chunks.append(chunk)

        batch_summaries: list[str] = []
        for i, chunk_days in enumerate(day_chunks):
            raw = "\n".join(_format_day_block(d, by_date[d]) for d in chunk_days)
            print(f"[rebuild] batch {i+1}/{len(day_chunks)} "
                  f"({chunk_days[0]} → {chunk_days[-1]}, {len(raw)} chars)")

            resp = or_client.chat.completions.create(
                model=model,
                max_tokens=1500,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            personality + "\n\n"
                            "Sos maripobot. Resumí brevemente los eventos más significativos "
                            "de estos días en bullets etiquetados ([IDENTIDAD], [COMUNIDAD], "
                            "[TÉCNICO], [LÍMITES], [LORE], [MEMORIA]). Solo lo que vale recordar."
                        ),
                    },
                    {"role": "user", "content": raw},
                ],
            )
            summary = resp.choices[0].message.content or ""
            batch_summaries.append(f"### {chunk_days[0]} → {chunk_days[-1]}\n{summary}")

        # Final synthesis from batch summaries
        synthesis_input = "\n\n".join(batch_summaries)
        print(f"[rebuild] síntesis final desde {len(batch_summaries)} batches...")
    else:
        # Single-shot: send the full log
        synthesis_input = "\n".join(
            _format_day_block(d, by_date[d]) for d in sorted_days
        )
        print(f"[rebuild] log completo: {len(synthesis_input)} chars — enviando al modelo...")

    # ── 3. Final synthesis → canonical history.md ─────────────────────────────

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=4000,
        messages=[
            {"role": "system", "content": personality + "\n\n" + _REBUILD_HISTORY_PROMPT},
            {"role": "user", "content": synthesis_input},
        ],
    )

    new_history = response.choices[0].message.content
    if not new_history:
        print("[rebuild] el modelo devolvió contenido vacío")
        return

    # ── 4. Write output ───────────────────────────────────────────────────────

    if dry_run:
        print("\n" + "─" * 60)
        print("[rebuild] DRY RUN — resultado (no se escribe nada):")
        print("─" * 60)
        print(new_history)
        print("─" * 60)
        return

    # Backup current history before overwriting
    if HISTORY_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = HISTORY_PATH.with_name(f"history_backup_{ts}.md")
        backup.write_text(HISTORY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[rebuild] backup guardado: {backup.name}")

    HISTORY_PATH.write_text(new_history, encoding="utf-8")
    print(f"[rebuild] history.md reconstruido ({len(new_history)} chars, "
          f"{len(new_history.splitlines())} líneas)")


# ── /remember command ──────────────────────────────────────────────────────────

def handle_remember_command(
    or_client,
    author_handle: str,
    text: str,
    model: str = LITE_MODEL,
) -> bool:
    """Process a /remember command; persist valid facts to the user's profile."""
    raw = re.sub(r'/remember', '', text, flags=re.IGNORECASE).strip()
    if not raw:
        return False

    sanitize_prompt = load_prompt("reminder_prompt")

    result: MemoryExtraction = structured_client.chat.completions.create(
        model=model,
        max_tokens=150,
        messages=[
            {"role": "system", "content": sanitize_prompt},
            {"role": "user", "content": raw},
        ],
        response_model=MemoryExtraction,
    )

    if not result.facts:
        return False

    facts_md = "\n".join(f"- {fact}" for fact in result.facts)

    current_profile = load_user_profile(author_handle)
    profiles = json.loads(PROFILES_JSON_PATH.read_text(encoding="utf-8"))
    profile_path = USERS_DIR / profiles[author_handle]

    updated = current_profile or ""
    if "## Memoria" in updated:
        updated += "\n" + facts_md
    else:
        updated += "\n\n## Memoria\n" + facts_md

    profile_path.write_text(updated, encoding="utf-8")
    print(f"/remember de {author_handle} guardado: {facts_md}")
    return True


# ── Timeline pipelines ─────────────────────────────────────────────────────────

def fetch_following_timeline(
    client: Client,
    limit: int = 100,
) -> list[dict]:
    """Fetch recent posts from accounts the bot follows."""
    posts = []
    cursor = None  # noqa: F841 (reserved for future pagination)

    params = {"limit": limit, "algorithm": "reverse-chronological"}
    response = client.app.bsky.feed.get_timeline(params)

    for item in response.feed:
        # Skip reposts and replies to keep signal clean
        if item.reason or (item.reply and item.post.author.handle != client.me.handle):
            continue
        posts.append({
            "author": item.post.author.handle,
            "text": item.post.record.text,
            "indexed_at": item.post.indexed_at,
        })

    return posts


def summarize_timeline(
    or_client: OpenAI,
    posts: list[dict],
    model: str = LITE_MODEL,
) -> str | None:
    """Generate a narrative summary of the timeline in the bot's voice."""
    if not posts:
        return None

    timeline_text = "\n\n".join(f"@{p['author']}: {p['text']}" for p in posts)
    prompt = load_prompt("summarize_timeline_prompt")

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": timeline_text},
        ],
    )

    return response.choices[0].message.content or None


def run_timeline_pipeline(client: Client, or_client: OpenAI) -> None:
    """Fetch timeline, summarize and prepend to timeline.md as a dated entry."""
    posts = fetch_following_timeline(client)
    if not posts:
        raise RuntimeError("timeline vacio")

    summary = summarize_timeline(or_client, posts)
    if not summary:
        raise RuntimeError("summarize_timeline devolvio None")

    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"## {today_str}\n\n{summary}\n\n---\n\n"

    p = TIMELINE_PATH
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    p.write_text(entry + existing, encoding="utf-8")

    print(f"timeline guardado: {len(posts)} posts procesados")


def reflect_on_timeline(
    or_client: OpenAI,
    model: str = REASONING_MODEL,
) -> str | None:
    """Read today's timeline summary and generate a personal take."""
    if not TIMELINE_PATH.exists():
        return None

    full = TIMELINE_PATH.read_text(encoding="utf-8")
    latest = full.split("---")[0].strip()
    if not latest:
        return None

    personality = PERSONALITY_PATH.read_text(encoding="utf-8")
    prompt = load_prompt("reflect_timeline_prompt")

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=2500,
        messages=[
            {"role": "system", "content": personality + "\n\n" + prompt},
            {"role": "user", "content": latest},
        ],
    )

    return response.choices[0].message.content or None


def run_timeline_reflection_pipeline(client: Client, or_client: OpenAI) -> None:
    """Generate and post a personal take on today's timeline summary."""
    opinion = reflect_on_timeline(or_client)
    if not opinion:
        raise RuntimeError("reflect_on_timeline devolvio None")

    opinion = truncate_to_graphemes(opinion)
    client.send_post(opinion)
    print(f"opinion de timeline posteada: {opinion}")


# ── Music pipeline ─────────────────────────────────────────────────────────────

def generate_track_opinion(
    or_client: OpenAI,
    track: dict,
    model: str = LITE_MODEL,
) -> str | None:
    """Generate a personal opinion on a track based on its metadata."""
    personality = PERSONALITY_PATH.read_text(encoding="utf-8")
    genres_str = ", ".join(track["genres"]) if track["genres"] else "genero desconocido"
    prompt = load_prompt("reflect_music")

    track_info = (
        f"cancion: {track['title']}\n"
        f"artista: {track['artist']}\n"
        f"album: {track['album']}\n"
        f"generos: {genres_str}"
    )

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=600,
        messages=[
            {"role": "system", "content": personality + "\n\n" + prompt},
            {"role": "user", "content": track_info},
        ],
    )

    return response.choices[0].message.content or None


def run_music_pipeline(client: Client, or_client: OpenAI) -> None:
    """Pick a random track from a configured playlist and post an opinion."""
    if not SPOTIFY_PLAYLISTS:
        raise RuntimeError("spotify_playlists.json vacio")

    playlist_id = random.choice(SPOTIFY_PLAYLISTS)
    track = get_random_track_from_playlist(playlist_id)
    if not track:
        raise RuntimeError("no se pudo obtener track")

    opinion = generate_track_opinion(or_client, track)
    if not opinion:
        raise RuntimeError("generate_track_opinion devolvio None")

    spotify_url = track["url"]
    body = truncate_to_graphemes(f"{opinion}\n{spotify_url}")
    send_post_with_url_facet(client, body, spotify_url)
    print(f"musica posteada -- {track['artist']} - {track['title']}: {opinion}")


# ── /music command ─────────────────────────────────────────────────────────────

def generate_track_opinion_for_user(
    or_client: OpenAI,
    track: dict,
    user_profile: str | None = None,
    model: str = LITE_MODEL,
) -> str | None:
    """Generate a track opinion personalized to a specific user.

    Same as generate_track_opinion but appends the user profile to the user
    message so the LLM can tailor tone/context without changing the system prompt.
    """
    personality = PERSONALITY_PATH.read_text(encoding="utf-8")
    genres_str = ", ".join(track["genres"]) if track["genres"] else "genero desconocido"
    prompt = load_prompt("reflect_music")

    track_info = (
        f"cancion: {track['title']}\n"
        f"artista: {track['artist']}\n"
        f"album: {track['album']}\n"
        f"generos: {genres_str}"
    )

    # Append user context so the LLM can personalize without polluting the prompt
    if user_profile:
        track_info += (
            f"\n\n## Perfil del usuario que pidio la recomendacion\n"
            f"{user_profile}\n"
            f"(tene en cuenta sus gustos si los conoces para elegir el tono, "
            f"pero no menciones que leiste su perfil)"
        )

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=600,
        messages=[
            {"role": "system", "content": personality + "\n\n" + prompt},
            {"role": "user", "content": track_info},
        ],
    )
    return response.choices[0].message.content or None


def handle_music_command(
    bsky_client: Client,
    or_client: OpenAI,
    author_handle: str,
    mention_uri: str,
    mention_cid: str,
    root_uri: Optional[str] = None,
    root_cid: Optional[str] = None,
) -> None:
    """Handle a /music mention: pick a random track and reply with an opinion + Spotify link."""
    if not SPOTIFY_PLAYLISTS:
        reply_to_post(bsky_client, mention_uri, mention_cid,
                      "no tengo playlists configuradas todavia",
                      root_uri=root_uri, root_cid=root_cid)
        return

    playlist_id = random.choice(SPOTIFY_PLAYLISTS)
    track = get_random_track_from_playlist(playlist_id)

    if not track:
        reply_to_post(bsky_client, mention_uri, mention_cid,
                      "no pude traer un track de spotify, probá de nuevo",
                      root_uri=root_uri, root_cid=root_cid)
        return

    user_profile = load_user_profile(author_handle)
    opinion = generate_track_opinion_for_user(or_client, track, user_profile)

    spotify_url = track["url"]

    # Build reply body: opinion + URL, or just URL as fallback
    if opinion:
        body = truncate_to_graphemes(f"{opinion}\n{spotify_url}")
    else:
        body = truncate_to_graphemes(
            f"{track['artist']} - {track['title']}\n{spotify_url}"
        )

    # reply_to_post doesn't support facets; build it manually so the URL is clickable
    text_bytes = body.encode("utf-8")
    url_bytes = spotify_url.encode("utf-8")
    start = text_bytes.find(url_bytes)

    actual_root_uri = root_uri or mention_uri
    actual_root_cid = root_cid or mention_cid

    if start != -1:
        end = start + len(url_bytes)
        facet = models.AppBskyRichtextFacet.Main(
            features=[models.AppBskyRichtextFacet.Link(uri=spotify_url)],
            index=models.AppBskyRichtextFacet.ByteSlice(
                byte_start=start, byte_end=end,
            ),
        )
        bsky_client.send_post(
            text=body,
            facets=[facet],
            reply_to={
                "root": {"uri": actual_root_uri, "cid": actual_root_cid},
                "parent": {"uri": mention_uri, "cid": mention_cid},
            },
        )
    else:
        reply_to_post(bsky_client, mention_uri, mention_cid, body,
                      root_uri=root_uri, root_cid=root_cid)

    print(f"/music respondido a {author_handle}: {track['artist']} - {track['title']}")


# ── Video pipeline ─────────────────────────────────────────────────────────────

# Reddit blocks feedparser's default UA; use a browser-like string for all RSS requests.
REDDIT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def fetch_top_mealtime_video() -> dict | None:
    """Pick a top-of-week video from one of the configured video subreddits."""
    p = POSTED_VIDEOS_PATH
    posted = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    if not VIDEO_SUBREDDITS:
        return None

    # Pick a random subreddit for today
    chosen = random.choice(VIDEO_SUBREDDITS)
    print(f"subreddit elegido: r/{chosen}")

    feed = feedparser.parse(
        f"https://www.reddit.com/r/{chosen}/top.rss?t=week",
        agent=REDDIT_UA,
    )

    YT_RE = re.compile(
        r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]+"
    )

    for entry in feed.entries:
        yt_url = None

        # Reddit link posts expose the external URL directly in entry.link --
        # this is the most reliable source and must be checked first.
        link = entry.get("link", "")
        m = YT_RE.search(link)
        if m:
            yt_url = m.group(0)

        # Fall back to HTML summary/content (cross-posts, text posts with embeds)
        if not yt_url:
            html = entry.get("summary", "")
            if hasattr(entry, "content") and entry.content:
                html += entry.content[0].get("value", "")
            m = YT_RE.search(html)
            if m:
                yt_url = m.group(0)

        if not yt_url:
            continue

        if yt_url in posted:
            continue

        return {"title": entry.get("title", ""), "url": yt_url}

    return None


# Module-level YouTubeTranscriptApi instance.
# If Webshare credentials are present, route requests through the proxy to
# avoid YouTube IP bans (common on residential/cloud IPs after heavy use).
# Falls back to a plain unauthenticated instance when credentials are missing.
if WEBSHARE_USER and WEBSHARE_PASSWORD:
    _YTT_API = YouTubeTranscriptApi(
        proxy_config=WebshareProxyConfig(
            proxy_username=WEBSHARE_USER,
            proxy_password=WEBSHARE_PASSWORD,
        )
    )
    print("[youtube] usando proxy Webshare para transcripts")
else:
    _YTT_API = YouTubeTranscriptApi()
    print("[youtube] sin proxy configurado (WEBSHARE_USER/WEBSHARE_PASSWORD no encontrados en .env)")


def get_youtube_transcript(video_url: str, languages: list[str] = ["es", "en"]) -> str | None:
    """Fetch auto-generated or manual transcript from a YouTube video.

    Uses the module-level _YTT_API instance, which is configured with a
    Webshare proxy when credentials are available in the environment.
    """
    match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", video_url)
    if not match:
        return None

    video_id = match.group(1)

    try:
        transcript = _YTT_API.fetch(video_id, languages=languages)
        return " ".join(entry.text for entry in transcript)
    except (NoTranscriptFound, TranscriptsDisabled):
        return None
    except Exception as e:
        print(f"error obteniendo transcript de {video_id}: {e}")
        return None


def generate_video_comment(
    or_client: OpenAI,
    video_url: str,
    transcript: str,
    model: str = REASONING_MODEL,
) -> str:
    personality = PERSONALITY_PATH.read_text(encoding="utf-8")
    prompt = load_prompt("reflect_video")

    response = or_client.chat.completions.create(
        model=model,
        max_tokens=300,
        messages=[
            {"role": "system", "content": personality + "\n\n" + prompt},
            # Transcripts can be very long -- truncate to ~3000 chars
            {"role": "user", "content": transcript[:3000]},
        ],
    )

    return response.choices[0].message.content or ""


def send_post_with_url_facet(client: Client, text: str, url: str) -> None:
    """Post text to Bluesky with a clickable URL facet (byte-level offsets)."""
    text_bytes = text.encode("utf-8")
    url_bytes = url.encode("utf-8")

    start = text_bytes.find(url_bytes)
    if start == -1:
        client.send_post(text)
        return

    end = start + len(url_bytes)

    facet = models.AppBskyRichtextFacet.Main(
        features=[models.AppBskyRichtextFacet.Link(uri=url)],
        index=models.AppBskyRichtextFacet.ByteSlice(
            byte_start=start,
            byte_end=end,
        ),
    )

    client.send_post(text=text, facets=[facet])


def run_video_pipeline(client: Client, or_client: OpenAI) -> None:
    """Find a fresh video, comment on it and post."""
    video = None
    for _ in range(10):
        video = fetch_top_mealtime_video()
        if video:
            break

    if not video:
        raise RuntimeError("no hay videos nuevos para postear")

    comment = ""
    transcript = get_youtube_transcript(video["url"])
    if transcript:
        comment = generate_video_comment(or_client, video["url"], transcript)

    body = f"{comment}\n{video['url']}".strip() if comment else video["url"]
    body = truncate_to_graphemes(body)

    send_post_with_url_facet(client, body, video["url"])

    p = POSTED_VIDEOS_PATH
    posted = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    posted.append(video["url"])
    p.write_text(json.dumps(posted, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"video posteado: {video['url']}")


# ── Image pipeline ─────────────────────────────────────────────────────────────

VALID_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def fetch_random_reddit_image(
    posted_urls_path: Path = POSTED_IMAGES_PATH,
    subreddits: list[str] = None,
) -> dict | None:
    """Pick a random top-of-week image post via RSS feed, skipping seen ones."""
    if subreddits is None:
        subreddits = IMAGE_SUBREDDITS

    p = Path(posted_urls_path)
    posted: list[str] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    candidates = subreddits.copy()
    random.shuffle(candidates)

    for subreddit in candidates:
        try:
            feed = feedparser.parse(
                f"https://www.reddit.com/r/{subreddit}/top.rss?t=month&limit=20",
                agent=REDDIT_UA,
            )
        except Exception as e:
            print(f"error fetching r/{subreddit}: {e}")
            continue

        if not feed.entries:
            continue

        entries = feed.entries.copy()
        random.shuffle(entries)

        for entry in entries:
            html = ""
            if hasattr(entry, "content") and entry.content:
                html = entry.content[0].value
            elif hasattr(entry, "summary"):
                html = entry.summary

            match = re.search(r'<img[^>]+src="([^"]+)"', html)
            if not match:
                continue

            url = match.group(1)

            # Filter Reddit thumbnail previews -- keep i.redd.it / i.imgur.com / preview.redd.it
            if not any(domain in url for domain in ["i.redd.it", "i.imgur.com", "preview.redd.it"]):
                continue

            ext = Path(url.split("?")[0]).suffix.lower()
            if ext not in VALID_IMAGE_EXTENSIONS:
                continue

            if url in posted:
                continue

            if "preview.redd.it" in url:
                url = url.split("?")[0].replace("preview.redd.it", "i.redd.it")

            return {
                "url": url,
                "title": entry.get("title", ""),
                "subreddit": subreddit,
            }

    return None


def download_image(url: str) -> tuple[bytes, str] | None:
    """Download an image and return (bytes, mime_type)."""
    try:
        response = requests.get(url, timeout=35, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36"
        })
        response.raise_for_status()

        # Prefer Content-Type header, fall back to extension sniffing
        mime = response.headers.get("Content-Type", "").split(";")[0].strip()
        if not mime or "octet-stream" in mime:
            mime, _ = mimetypes.guess_type(url)
        if not mime:
            mime = "image/jpeg"

        return response.content, mime
    except Exception as e:
        print(f"error descargando imagen {url}: {e}")
        return None


def generate_image_comment(
    or_client: OpenAI,
    title: str,
    subreddit: str,
    model: str = IMAGE_MODEL,
) -> str | None:
    """Generate a short comment about a Reddit image post in the bot's voice.
    
    Returns None if the model flags the content as abusive toward women or
    gender minorities -- run_image_pipeline treats this as a skip signal.
    """
    personality = PERSONALITY_PATH.read_text(encoding="utf-8")
    prompt = load_prompt("reflect_image")

    try:
        response = or_client.chat.completions.create(
            model=model,
            max_tokens=200,
            messages=[
                {"role": "system", "content": personality + "\n\n" + prompt},
                {"role": "user", "content": f"titulo: {title}\nsubreddit: r/{subreddit}"},
            ],
        )
        result = response.choices[0].message.content or None

        # Content moderation gate: model returns SKIP_ABUSIVE if flagged
        if result and result.strip().startswith("SKIP_ABUSIVE"):
            print(f"[image] contenido abusivo detectado, saltando: '{title}' (r/{subreddit})")
            return None

        return result
    except Exception as e:
        print(f"error generando comentario de imagen: {e}")
        return None


def run_image_pipeline(client: Client, or_client: OpenAI) -> None:
    """Fetch a random Reddit image, generate a comment, upload and post to Bluesky.
    
    Raises RuntimeError on content moderation rejection so the scheduler
    retries with a different image on backoff.
    """
    image_data = fetch_random_reddit_image()
    if not image_data:
        raise RuntimeError("no se encontro imagen valida")

    downloaded = download_image(image_data["url"])
    if not downloaded:
        raise RuntimeError("no se pudo descargar la imagen")

    image_bytes, _mime_type = downloaded

    # Generate comment -- also acts as content moderation gate.
    # None means the image was flagged; raise so the scheduler retries.
    comment = generate_image_comment(or_client, image_data["title"], image_data["subreddit"])
    if comment is None:
        raise RuntimeError(
            f"imagen rechazada por moderacion o fallo el comentario: {image_data['url']}"
        )

    blob_response = client.upload_blob(image_bytes)
    blob = blob_response.blob

    text = truncate_to_graphemes(comment)

    embed = models.AppBskyEmbedImages.Main(
        images=[
            models.AppBskyEmbedImages.Image(
                image=blob,
                alt=image_data["title"][:1000],  # Bluesky alt text limit
            )
        ]
    )

    client.send_post(text=text, embed=embed)
    print(f"imagen posteada desde r/{image_data['subreddit']}: {image_data['url']}")

    p = POSTED_IMAGES_PATH
    posted: list[str] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    posted.append(image_data["url"])
    p.write_text(json.dumps(posted, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Lessons / system prompt assembly ──────────────────────────────────────────

def load_lessons() -> str | None:
    """Extract only the 'Lecciones destiladas' section from history.md."""
    text = HISTORY_PATH.read_text(encoding="utf-8")
    if "## Lecciones destiladas" not in text:
        return None
    start = text.index("## Lecciones destiladas")
    rest = text[start:]
    end = rest.find("\n## ")
    return rest[:end].strip() if end != -1 else rest.strip()


def build_system_prompt(
    base_prompt: str,
    user_profile: str | None,
    lessons: str | None = None,
) -> str:
    prompt = base_prompt
    if lessons:
        prompt += "\n\n" + lessons
    if user_profile:
        prompt += "\n\n## Perfil del usuario:\n" + user_profile
    return prompt


# ── Pipeline registry ──────────────────────────────────────────────────────────
#
# Must be defined after all run_* functions so the dict references are valid.

PIPELINE_FUNCS = {
    "news": run_news_pipeline,
    "video": run_video_pipeline,
    "update_history": run_history_update_pipeline,
    "reflection": run_reflection_pipeline,
    "timeline": run_timeline_pipeline,
    "timeline_reflection": run_timeline_reflection_pipeline,
    "music": run_music_pipeline,
    "image": run_image_pipeline,
}


# ── Burn (daily reply cap) ─────────────────────────────────────────────────────

# In-memory cache so we don't hit the credits endpoint on every loop iteration.
_credit_cache = {"total_usage": None, "checked_at": 0.0}


def get_total_usage_cached() -> Optional[float]:
    """Return cached cumulative usage, refreshing at most every CREDIT_CHECK_INTERVAL_S."""
    now = time.time()
    if (_credit_cache["total_usage"] is None
            or now - _credit_cache["checked_at"] > CREDIT_CHECK_INTERVAL_S):
        _credit_cache["total_usage"] = get_total_usage_usd()
        _credit_cache["checked_at"] = now
    return _credit_cache["total_usage"]


def get_usage_today(state: dict) -> Optional[float]:
    """
    Compute how many USD have been spent on OpenRouter since the start of
    the current day. Returns None if either the snapshot or the current
    reading is unavailable (in which case callers should fail open).
    """
    snapshot = state["burn"].get("usage_snapshot_at_day_start")
    if snapshot is None:
        return None
    current = get_total_usage_cached()
    if current is None:
        return None
    # Clamp to 0: defends against the (rare) case where total_usage reading
    # appears to go down due to provider corrections / refunds.
    return max(0.0, current - snapshot)


def is_burned(state: dict) -> bool:
    """
    Decide whether the bot is over its daily reply budget. Transitions
    (active <-> sleeping) are handled in announce_burn_transitions().
    """
    spent = get_usage_today(state)
    # Inability to compute today's spend is treated as the prior burn state
    # (sticky). This avoids false flips when the credits endpoint hiccups.
    if spent is None:
        return state["burn"]["state"] == "sleeping"

    return spent >= OPENROUTER_DAILY_BUDGET_USD


def announce_burn_transitions(state: dict, client: Client) -> None:
    """
    Post a sleep/wake message on burn-state transitions and update state.

    Messages are drawn at random from prompts/burned.json (sleep) and
    prompts/hello.json (wake), falling back to a hardcoded string if either
    file is missing or empty.

    Idempotent within the same day: each announcement fires at most once.
    """
    burned_now = is_burned(state)
    burn = state["burn"]

    # Transition: active -> sleeping (daily budget exhausted)
    if burned_now and burn["state"] == "active":
        if not burn["announced_today"]:
            msg = random.choice(_BURNED_MESSAGES) if _BURNED_MESSAGES else (
                "Limite alcanzado por hoy, chau"
            )
            try:
                client.send_post(msg)
                print("[burn] anuncio de siesta posteado")
            except Exception as e:
                print(f"[burn] no pude postear siesta: {e}")
        burn["state"] = "sleeping"
        burn["since"] = datetime.now(timezone.utc).isoformat()
        burn["announced_today"] = True
        save_scheduler_state(state)

    # Transition: sleeping -> active (new day, budget reset)
    elif not burned_now and burn["state"] == "sleeping":
        if not burn["wake_announced_today"]:
            msg = random.choice(_HELLO_MESSAGES) if _HELLO_MESSAGES else (
                "Bip bip, estoy activo de vuelta"
            )
            try:
                client.send_post(msg)
                print("[burn] anuncio de despertar posteado")
            except Exception as e:
                print(f"[burn] no pude postear despertar: {e}")
        burn["state"] = "active"
        burn["since"] = None
        burn["wake_announced_today"] = True
        save_scheduler_state(state)


# ── Setup ──────────────────────────────────────────────────────────────────────

# atproto client
client = Client()
client.login(BOT_HANDLE, BSKY_PASSWORD)

# Plain OpenAI-compatible client -- used by all free-text generation functions
# (generate_reply, summarize_news, reflect_on_history, etc.).
or_client = OpenAI(
    api_key=OPENAI_KEY,
    base_url=OPENAI_ENDPOINT,
)

# Instructor-wrapped client -- used ONLY by extraction functions that return
# Pydantic models (interpret_bio, update_user_profile, maybe_update_lessons,
# handle_remember_command). Keeps structured and generative calls fully separate.
structured_client = instructor.from_openai(
    OpenAI(
        api_key=OPENAI_KEY,
        base_url=OPENAI_ENDPOINT,
    ),
    mode=Mode.JSON,
)

# System prompt is loaded once at boot -- it's stable across the run
SYSTEM_PROMPT = load_system_prompt()

# Persistent state
respondido = load_respondido()
scheduler_state = load_scheduler_state()
maybe_roll_day(scheduler_state)
# Snapshot the OpenRouter usage baseline if we don't have one yet (fresh
# install, or previous run never managed to hit the credits endpoint).
ensure_usage_snapshot(scheduler_state)
save_scheduler_state(scheduler_state)

# Set TESTING=True via env var to restrict replies to admin only
TESTING = os.environ.get("MARIPOBOT_TESTING", "").lower() in ("1", "true", "yes")


# ── Main loop ──────────────────────────────────────────────────────────────────

def main_loop() -> None:
    global respondido, scheduler_state

    while True:
        try:
            # 1) Daily roll-over: reset per-day flags at midnight (and
            #    snapshot today's starting OpenRouter usage)
            day_changed = maybe_roll_day(scheduler_state)
            if day_changed:
                save_scheduler_state(scheduler_state)
                print(f"[scheduler] nuevo dia: {scheduler_state['today']}")

            # If the snapshot couldn't be taken (e.g. /credits was down at
            # rollover), keep retrying until it succeeds.
            ensure_usage_snapshot(scheduler_state)

            # 2) Check burn state and post sleep/wake transitions when needed
            announce_burn_transitions(scheduler_state, client)

            # 3) Run scheduled pipelines (each with its own retry/backoff).
            #    Pipelines are NOT gated by burn state -- they keep running
            #    while OpenRouter still has any credit left.
            for pname in PIPELINES:
                try_run_pipeline(pname, scheduler_state, client, or_client)

            # 4) Handle incoming mentions
            try:
                menciones = get_latest_mentions(client)
            except Exception as e:
                print(f"error al traer menciones: {e}")
                menciones = []

            burned = is_burned(scheduler_state)

            for m in menciones:
                # Skip already-handled posts
                if m['uri'] in respondido:
                    continue

                # In testing mode, only the admin gets replies
                if TESTING and m['author'] != ADMIN_HANDLE:
                    print(f"[testing] ignorando mencion de {m['author']}")
                    continue

                # Always ensure a profile exists -- silent operation, runs
                # even when burned (it's tiny and helps keep memory consistent)
                try:
                    ensure_user_profile_exists(client, or_client, m['author'])
                except Exception as e:
                    print(f"error al crear perfil de {m['author']}: {e}")

                # /remember is a SILENT command (no reply) -- keep handling it
                # even when the bot is burned for user replies.
                if '/remember' in m['text'].lower():
                    try:
                        handle_remember_command(or_client, m['author'], m['text'])
                    except Exception as e:
                        print(f"error en /remember de {m['author']}: {e}")
                    respondido.append(m['uri'])
                    save_respondido(respondido)
                    continue

                # If we're over the daily reply cap, mark the post as handled
                # (so we don't spam-retry it later) and skip the LLM reply.
                if burned:
                    print(f"[burn] saltando respuesta a {m['author']} -- bot dormido")
                    respondido.append(m['uri'])
                    save_respondido(respondido)
                    continue

                # /music command: reply with a personalized track recommendation.
                # Placed after burn check because it costs tokens (unlike /remember).
                if '/music' in m['text'].lower():
                    try:
                        handle_music_command(
                            client, or_client, m['author'],
                            m['uri'], m['cid'],
                            root_uri=m.get('root_uri'),
                            root_cid=m.get('root_cid'),
                        )
                    except Exception as e:
                        print(f"error en /music de {m['author']}: {e}")
                    respondido.append(m['uri'])
                    save_respondido(respondido)
                    continue

                # Build conversation context for the reply
                try:
                    cadena = chain_to_string(get_parent_chain(client, m['uri']))
                except Exception as e:
                    print(f"no pude armar cadena de {m['author']}: {e}")
                    continue

                try:
                    perfil_usuario = load_user_profile(m['author'])
                except Exception as e:
                    print(f"NO pude cargar el perfil de {m['author']} - ERROR: \n {e}")
                    perfil_usuario = None

                try:
                    lessons = load_lessons()
                    context_system_prompt = build_system_prompt(
                        SYSTEM_PROMPT, perfil_usuario, lessons
                    )

                    # If the mention has an image, describe it and append to the chain
                    try:
                        if has_image_embed(m.get('embed')):
                            image_urls = get_image_urls_from_embed(
                                m.get('embed'), m['author_did']
                            )
                            if image_urls:
                                cadena += (
                                    f"\n[imagen adjunta: "
                                    f"{describe_image(or_client, image_urls)}]"
                                )
                    except Exception as e:
                        print(f"no pude interpretar imagen de {m['author']}: {e}")
                        reply_to_post(
                            client, m["uri"], m["cid"],
                            "no pude interpretar la imagen, soy un bot tontito",
                            root_uri=m.get("root_uri"),
                            root_cid=m.get("root_cid"),
                        )
                        respondido.append(m["uri"])
                        save_respondido(respondido)
                        continue

                    reply_text = generate_reply(or_client, context_system_prompt, cadena)
                    if not reply_text:
                        print(f"reply vacia para {m['author']}, marcando como respondido")
                        respondido.append(m["uri"])
                        save_respondido(respondido)
                        continue

                    reply_to_post(
                        client, m["uri"], m["cid"], reply_text,
                        root_uri=m.get("root_uri"),
                        root_cid=m.get("root_cid"),
                    )
                    print(f"respondi a {m['author']} con {reply_text}")

                    # Update memory after replying (silent function)
                    try:
                        update_user_profile(or_client, m["author"], cadena, reply_text)
                        print(f"actualice la memoria de {m['author']}")
                    except Exception as e:
                        print(f"NO pude actualizar la memoria de {m['author']} - ERROR: \n {e}")

                    respondido.append(m["uri"])
                    save_respondido(respondido)
                except Exception as e:
                    print(f"falle al responder a {m['author']} - ERROR: \n {e}")

                # Be polite to Bluesky's rate limiter
                time.sleep(2)

            # Sleep before next iteration -- keeps CPU low and avoids API rate limits
            time.sleep(3)

        except KeyboardInterrupt:
            print("interrumpido por el usuario, saliendo")
            break
        except Exception as e:
            # Defensive: never let the main loop die -- log and continue.
            print(f"[main_loop] error no manejado: {e}")
            time.sleep(10)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="maripobot runner")
    parser.add_argument(
        "--pipeline",
        choices=list(PIPELINE_FUNCS.keys()),
        default=None,
        help="Run a single pipeline and exit (for testing)",
    )
    parser.add_argument(
        "--rebuild-history",
        action="store_true",
        help="Reconstruct history.md from scratch using bot's Bluesky posts and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --rebuild-history: print the result without writing to disk",
    )
    parser.add_argument(
        "--batch-days",
        type=int,
        default=0,
        metavar="N",
        help="With --rebuild-history: process posts in N-day batches before final synthesis "
             "(useful when post history is very long; 0 = single shot, default)",
    )
    args = parser.parse_args()

    if args.rebuild_history:
        rebuild_history(
            client, or_client,
            dry_run=args.dry_run,
            batch_days=args.batch_days,
        )
    elif args.pipeline:
        # Run one pipeline directly, bypassing the scheduler and hour checks
        print(f"[test] corriendo pipeline '{args.pipeline}' directamente...")
        PIPELINE_FUNCS[args.pipeline](client, or_client)
        print(f"[test] pipeline '{args.pipeline}' terminado")
    else:
        main_loop()