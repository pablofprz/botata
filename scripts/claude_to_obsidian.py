"""Importa conversaciones de Claude al vault de Obsidian de butterbot.

Dos fuentes:
  - code: transcripts .jsonl de Claude Code
      (~/.claude/projects/C--Users-pablo-Documents-butterbot/*.jsonl) -> chats/code/
  - web:  exports .md manuales de claude.ai (~/claude-exports/web/*.md) -> chats/web/

Cada nota resultante lleva frontmatter YAML, tags auto-generados por keywords y
wikilinks a notas existentes del vault. Idempotente: usa un manifest para saltear
lo ya procesado. Solo stdlib.

Uso:
    python scripts/claude_to_obsidian.py            # ambas fuentes
    python scripts/claude_to_obsidian.py --source code
    python scripts/claude_to_obsidian.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# ─── Rutas (Windows dev). Ajustar si se corre en prod Linux. ───────────────
HOME = Path.home()
PROJECT_SLUG = "C--Users-pablo-Documents-proyecto-botata-core"
CODE_TRANSCRIPTS = HOME / ".claude" / "projects" / PROJECT_SLUG
WEB_EXPORTS = HOME / "claude-exports" / "web"
# vault FUERA del repo desde la reorg 2026-07-24: proyecto-botata/{core (repo), vault}
VAULT = Path(__file__).resolve().parent.parent.parent / "vault"
CHATS_CODE = VAULT / "chats" / "code"
CHATS_WEB = VAULT / "chats" / "web"
MANIFEST = VAULT / "chats" / ".manifest.json"

# keyword -> tag. Rescata handles/temas propios que el semántico no atrapa.
KEYWORD_TAGS: dict[str, str] = {
    "langgraph": "langgraph", "pydantic": "pydantic", "sqlite": "sqlite",
    "sqlite-vec": "sqlite-vec", "fts5": "fts5", "embedding": "embeddings",
    "bge-m3": "embeddings", "router": "router", "tool": "tools",
    "bluesky": "bluesky", "atproto": "bluesky", "rclone": "ops",
    "roadmap": "roadmap", "graphify": "graphify", "obsidian": "vault",
    "bug": "bug", "deploy": "deploy", "test": "testing", "prompt": "prompts",
}

# nota del vault -> patrones que disparan un wikilink hacia ella
WIKILINK_HINTS: dict[str, tuple[str, ...]] = {
    "router-de-modelos": ("router", "fallback", "endpoint", "alias"),
    "framework-de-tools": ("tool registry", "scope", "enable/disable", "tools.py"),
    "persistencia-sqlite-vec": ("sqlite", "sqlite-vec", "fts5", "bge-m3", "embedding"),
    "sync-credenciales-y-db": ("rclone", "secrets", "credencial", "backup"),
    "butterbot-moc": ("butterbot",),
}


def _slugify(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_]+", "-", s)
    return s[:maxlen].strip("-") or "chat"


def _tags_for(text: str) -> list[str]:
    low = text.lower()
    found = {tag for kw, tag in KEYWORD_TAGS.items() if kw in low}
    return sorted(found)


def _wikilinks_for(text: str) -> list[str]:
    low = text.lower()
    return sorted(
        note for note, pats in WIKILINK_HINTS.items()
        if any(p in low for p in pats)
    )


def _frontmatter(title: str, created: str, source: str, tags: list[str]) -> str:
    taglist = ", ".join(["chat-import", f"source-{source}", *tags])
    return (
        "---\n"
        f"title: {title}\n"
        f"tags: [{taglist}]\n"
        f"created: {created}\n"
        f"type: chat\n"
        f"source: {source}\n"
        "---\n\n"
    )


def _load_manifest() -> dict[str, str]:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def _save_manifest(m: dict[str, str]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def _extract_text(content) -> str:
    """Content de un mensaje jsonl puede ser str o lista de bloques."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _render_transcript(path: Path) -> tuple[str, str] | None:
    """Devuelve (created_iso, cuerpo_markdown) de un .jsonl, o None si vacío."""
    turns: list[tuple[str, str]] = []
    created: str | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if created is None and obj.get("timestamp"):
            created = obj["timestamp"][:10]
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(msg.get("content")).strip()
        if text:
            turns.append((role, text))
    if not turns:
        return None
    body = "\n\n".join(
        f"**{'Polci' if r == 'user' else 'Claude'}:**\n\n{t}" for r, t in turns
    )
    return created or datetime.now(timezone.utc).strftime("%Y-%m-%d"), body


def _process_code(dry: bool, manifest: dict[str, str]) -> int:
    if not CODE_TRANSCRIPTS.exists():
        return 0
    CHATS_CODE.mkdir(parents=True, exist_ok=True)
    n = 0
    for jsonl in sorted(CODE_TRANSCRIPTS.glob("*.jsonl")):
        key = f"code:{jsonl.stem}"
        digest = hashlib.sha256(jsonl.read_bytes()).hexdigest()[:16]
        if manifest.get(key) == digest:
            continue  # sin cambios desde el último import
        rendered = _render_transcript(jsonl)
        if rendered is None:
            continue
        created, body = rendered
        title = f"Claude Code {created} ({jsonl.stem[:8]})"
        out = CHATS_CODE / f"{created}-{jsonl.stem[:8]}.md"
        content = (
            _frontmatter(title, created, "code", _tags_for(body))
            + f"# {title}\n\n"
            + "".join(f"[[{w}]] " for w in _wikilinks_for(body))
            + ("\n\n" if _wikilinks_for(body) else "")
            + body
            + "\n"
        )
        if dry:
            print(f"[dry] code -> {out.name} ({len(body)} chars)")
        else:
            out.write_text(content, encoding="utf-8")
            manifest[key] = digest
        n += 1
    return n


def _process_web(dry: bool, manifest: dict[str, str]) -> int:
    if not WEB_EXPORTS.exists():
        return 0
    CHATS_WEB.mkdir(parents=True, exist_ok=True)
    n = 0
    for md in sorted(WEB_EXPORTS.glob("*.md")):
        key = f"web:{md.name}"
        digest = hashlib.sha256(md.read_bytes()).hexdigest()[:16]
        if manifest.get(key) == digest:
            continue
        body = md.read_text(encoding="utf-8", errors="replace")
        created = datetime.fromtimestamp(md.stat().st_mtime).strftime("%Y-%m-%d")
        title = f"Claude Web — {md.stem}"
        out = CHATS_WEB / f"{created}-{_slugify(md.stem)}.md"
        content = (
            _frontmatter(title, created, "web", _tags_for(body))
            + "".join(f"[[{w}]] " for w in _wikilinks_for(body))
            + ("\n\n" if _wikilinks_for(body) else "")
            + body
            + "\n"
        )
        if dry:
            print(f"[dry] web -> {out.name}")
        else:
            out.write_text(content, encoding="utf-8")
            manifest[key] = digest
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Importa chats de Claude al vault.")
    ap.add_argument("--source", choices=("code", "web", "both"), default="both")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest = _load_manifest()
    total = 0
    if args.source in ("code", "both"):
        total += _process_code(args.dry_run, manifest)
    if args.source in ("web", "both"):
        total += _process_web(args.dry_run, manifest)
    if not args.dry_run:
        _save_manifest(manifest)
    print(f"[claude_to_obsidian] {total} chat(s) {'a importar' if args.dry_run else 'importados'}.")


if __name__ == "__main__":
    main()
