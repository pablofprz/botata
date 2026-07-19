"""config_ui.py — panel local de configuración de botata (T22).

UI web servida SOLO en 127.0.0.1 para editar settings.json, credenciales (.env),
fuentes de noticias y toggles de skills sin tocar JSON a mano.

    python config_ui.py [--port 8787] [--no-browser]

Decisiones (ver docs/ARQUITECTURA.md §15):
- stdlib puro (http.server + ui/config.html con vanilla JS). Sin deps.
- Escrituras atómicas (tmp + os.replace) con backup `.bak` del estado anterior.
- Validación server-side ANTES de escribir; error → 400 con detalle, no toca nada.
- Credenciales write-only: la API jamás devuelve valores del .env, solo qué claves
  están seteadas. Input vacío = no tocar.
- Los cambios en settings/.env/news requieren REINICIAR el bot (se leen al import).
  Skills es hot-reload.

Esta es la UI del NÚCLEO; las futuras UIs por canal (Discord, etc.) serán
interfaces separadas (decisión del admin, 2026-07-12).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools import ALL_SCOPES
from skills import load_skills, _parse_frontmatter

BASE_DIR = Path(__file__).parent
UI_HTML = BASE_DIR / "ui" / "config.html"

ENV_KEYS = [
    "BSKY_PASSWORD", "OPENROUTER_API_KEY", "BRAVE_API_KEY",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "YOUTUBE_API_KEY",
    "IG_USERNAME", "IG_PASSWORD", "GOOGLE_OAUTH_ID", "GOOGLE_OAUTH_SECRET",
]

_FEED_TYPES = {"list", "feed", "following"}
_POLICIES = {"conservative", "balanced", "active"}
_NEWS_MODES = {"comment", "post"}
_MCP_TRANSPORTS = {"stdio", "http"}


# ─── Validadores (puros: devuelven lista de errores) ─────────────────────────
def validate_settings(s: dict) -> list[str]:
    errs: list[str] = []
    for key in ("BOT_HANDLE", "ADMIN_HANDLE"):
        if not (isinstance(s.get(key), str) and s[key].strip()):
            errs.append(f"{key} es obligatorio")
    if not isinstance(s.get("POLL_INTERVAL_SECONDS", 60), (int, float)):
        errs.append("POLL_INTERVAL_SECONDS debe ser numérico")

    for i, feed in enumerate(s.get("FEEDS", [])):
        tag = f"FEEDS[{i}]"
        if not feed.get("name"):
            errs.append(f"{tag}: falta name")
        if feed.get("type", "list") not in _FEED_TYPES:
            errs.append(f"{tag}: type inválido '{feed.get('type')}' (usar {sorted(_FEED_TYPES)})")
        if feed.get("type", "list") != "following" and not feed.get("uri"):
            errs.append(f"{tag}: falta uri (obligatoria salvo type=following)")
        if feed.get("posting_policy", "balanced") not in _POLICIES:
            errs.append(f"{tag}: posting_policy inválida '{feed.get('posting_policy')}'")

    for name, cfg in s.get("TOOLS", {}).items():
        bad = set(cfg.get("scopes", [])) - ALL_SCOPES
        if bad:
            errs.append(f"TOOLS.{name}: scope(s) inválido(s) {sorted(bad)}")

    for name, cfg in s.get("TASKS", {}).items():
        if "interval_hours" in cfg and not isinstance(cfg["interval_hours"], (int, float)):
            errs.append(f"TASKS.{name}: interval_hours debe ser numérico")

    budget = s.get("BUDGET")
    if budget is not None:
        daily = budget.get("daily_usd", 1.0)
        if not isinstance(daily, (int, float)) or isinstance(daily, bool) or daily <= 0:
            errs.append("BUDGET.daily_usd debe ser un número > 0")
        for flag in ("enabled", "announce"):
            if flag in budget and not isinstance(budget[flag], bool):
                errs.append(f"BUDGET.{flag} debe ser booleano")

    errs += validate_mcp(s.get("MCP", {}))

    models = s.get("MODELS")
    if models:
        endpoints = set(models.get("endpoints", {}))
        aliases = models.get("aliases", {})
        for alias, chain in aliases.items():
            if not chain:
                errs.append(f"MODELS.aliases.{alias}: cadena vacía")
            for j, hop in enumerate(chain or []):
                if hop.get("endpoint") not in endpoints:
                    errs.append(f"MODELS.aliases.{alias}[{j}]: endpoint desconocido "
                                f"'{hop.get('endpoint')}'")
                if not hop.get("model"):
                    errs.append(f"MODELS.aliases.{alias}[{j}]: falta model")
        for role, alias in models.get("roles", {}).items():
            if alias not in aliases:
                errs.append(f"MODELS.roles.{role}: alias desconocido '{alias}'")
    return errs


def validate_mcp(mcp: dict) -> list[str]:
    errs = []
    for name, cfg in (mcp or {}).items():
        transport = cfg.get("transport", "stdio")
        if transport not in _MCP_TRANSPORTS:
            errs.append(f"MCP.{name}: transport inválido '{transport}'")
        elif transport == "stdio" and not cfg.get("command"):
            errs.append(f"MCP.{name}: transport stdio requiere command")
        elif transport == "http" and not cfg.get("url"):
            errs.append(f"MCP.{name}: transport http requiere url")
        bad = set(cfg.get("scopes", [])) - ALL_SCOPES
        if bad:
            errs.append(f"MCP.{name}: scope(s) inválido(s) {sorted(bad)}")
    return errs


def validate_news(news: list) -> list[str]:
    errs = []
    for i, src in enumerate(news or []):
        tag = f"news[{i}]"
        if not (src.get("url") or "").startswith(("http://", "https://")):
            errs.append(f"{tag}: url inválida")
        if not src.get("title"):
            errs.append(f"{tag}: falta title")
        if src.get("mode", "post") not in _NEWS_MODES:
            errs.append(f"{tag}: mode inválido '{src.get('mode')}' (comment|post)")
    return errs


# ─── Store: lectura/escritura atómica de los archivos de config ──────────────
class ConfigStore:
    """Paths inyectables (tests usan un tmp_path con fixtures)."""

    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.settings_path = self.base / "config" / "settings.json"
        self.news_path = self.base / "config" / "news_sites.json"
        self.env_path = self.base / ".env"
        self.skills_dir = self.base / "skills"

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    # -- lectura ---------------------------------------------------------------
    def read_all(self) -> dict:
        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        news = (json.loads(self.news_path.read_text(encoding="utf-8"))
                if self.news_path.exists() else [])
        return {
            "settings": settings,
            "news": news,
            "env_keys": self._env_status(),
            "skills": self._skills_info(),
        }

    def _env_status(self) -> dict[str, bool]:
        """Qué claves están seteadas. NUNCA los valores."""
        present: set[str] = set()
        if self.env_path.exists():
            for line in self.env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key = line.split("=", 1)[0].strip()
                    if line.split("=", 1)[1].strip():
                        present.add(key)
        return {k: (k in present) for k in ENV_KEYS}

    def _skills_info(self) -> list[dict]:
        out = []
        if not self.skills_dir.is_dir():
            return out
        for path in sorted(self.skills_dir.glob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if parsed is None:
                continue
            meta, _ = parsed
            if not meta.get("name"):
                continue
            out.append({
                "name": meta["name"],
                "description": meta.get("description", ""),
                "scopes": meta.get("scopes", ""),
                "enabled": meta.get("enabled", "true").lower() != "false",
                "inline": meta.get("inline", "false").lower() == "true",
                "file": path.name,
            })
        return out

    # -- escritura -------------------------------------------------------------
    def write_settings(self, settings: dict) -> list[str]:
        errs = validate_settings(settings)
        if errs:
            return errs
        self._atomic_write(self.settings_path,
                           json.dumps(settings, ensure_ascii=False, indent="\t") + "\n")
        return []

    def write_news(self, news: list) -> list[str]:
        errs = validate_news(news)
        if errs:
            return errs
        self._atomic_write(self.news_path,
                           json.dumps(news, ensure_ascii=False, indent=2) + "\n")
        return []

    def update_env(self, updates: dict[str, str]) -> list[str]:
        """Actualiza SOLO las claves con valor no vacío; preserva el resto tal cual."""
        updates = {k: v for k, v in updates.items() if k in ENV_KEYS and (v or "").strip()}
        if not updates:
            return []
        lines = (self.env_path.read_text(encoding="utf-8").splitlines()
                 if self.env_path.exists() else [])
        seen: set[str] = set()
        out = []
        for line in lines:
            if "=" in line and not line.lstrip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                if key in updates:
                    out.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            out.append(line)
        for key, value in updates.items():
            if key not in seen:
                out.append(f"{key}={value}")
        self._atomic_write(self.env_path, "\n".join(out) + "\n")
        return []

    def set_skill_enabled(self, name: str, enabled: bool) -> list[str]:
        """Reescribe solo la línea `enabled:` del frontmatter (o la inserta)."""
        for path in sorted(self.skills_dir.glob("*.md")):
            parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if parsed is None or parsed[0].get("name") != name:
                continue
            text = path.read_text(encoding="utf-8")
            value = "true" if enabled else "false"
            if re.search(r"(?m)^enabled\s*:", text):
                new = re.sub(r"(?m)^enabled\s*:.*$", f"enabled: {value}", text, count=1)
            else:  # insertar antes del cierre del frontmatter (segundo ---)
                new = re.sub(r"(?m)^---\s*$", f"enabled: {value}\n---", text.split("\n", 1)[1],
                             count=1)
                new = text.split("\n", 1)[0] + "\n" + new
            self._atomic_write(path, new)
            return []
        return [f"skill desconocida: {name}"]


# ─── HTTP ────────────────────────────────────────────────────────────────────
def make_handler(store: ConfigStore):

    class ConfigHandler(BaseHTTPRequestHandler):

        def log_message(self, fmt, *args):  # silenciar el log por request
            pass

        def _send(self, code: int, body: dict | bytes, ctype="application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, UI_HTML.read_bytes(), ctype="text/html")
            elif self.path == "/api/config":
                self._send(200, store.read_all())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                body = self._body()
                if self.path == "/api/settings":
                    errs = store.write_settings(body)
                elif self.path == "/api/news":
                    errs = store.write_news(body if isinstance(body, list) else body.get("news", []))
                elif self.path == "/api/env":
                    errs = store.update_env(body)
                elif self.path == "/api/skills":
                    errs = store.set_skill_enabled(body.get("name", ""), bool(body.get("enabled")))
                else:
                    self._send(404, {"error": "not found"})
                    return
                if errs:
                    self._send(400, {"ok": False, "errors": errs})
                else:
                    self._send(200, {"ok": True})
            except json.JSONDecodeError:
                self._send(400, {"ok": False, "errors": ["JSON inválido"]})
            except Exception as e:  # nunca tirar el server por un request
                self._send(500, {"ok": False, "errors": [f"{type(e).__name__}: {e}"]})

    return ConfigHandler


def serve(base_dir: Path, port: int = 8787) -> ThreadingHTTPServer:
    """Crea el server (sin loop). El caller decide serve_forever/thread."""
    store = ConfigStore(base_dir)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))


def main() -> None:
    parser = argparse.ArgumentParser(description="Panel de configuración de botata (localhost)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    httpd = serve(BASE_DIR, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"botata config UI → {url}  (Ctrl+C para salir)")
    print("Los cambios en settings/.env/noticias requieren reiniciar el bot.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nchau")


if __name__ == "__main__":
    main()
