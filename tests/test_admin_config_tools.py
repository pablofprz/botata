"""Tests de las tools de config por comandos de admin (T30)."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import botata as b
from scheduler import PeriodicTask
from tools import Scope, ToolContext

_CTX = ToolContext(state={"author_handle": "ppolci.com", "is_admin": True}, conn=None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Sandbox: settings.json + globals de botata apuntando a tmp_path."""
    (tmp_path / "config").mkdir()
    settings = {
        "BOT_HANDLE": "bot.test", "ADMIN_HANDLE": "ppolci.com",
        "NEWS_ENABLED": False,
        "FEEDS": [{"name": "polcifeed", "type": "list", "uri": "at://x/l/1",
                   "interval_hours": 6, "enabled": True, "posting_policy": "active"}],
        "TOOLS": {},
        "TASKS": {"heartbeat": {"enabled": False, "interval_hours": 12}},
        "MCP": {"reddit": {"transport": "stdio", "command": "python", "enabled": False}},
    }
    (tmp_path / "config" / "settings.json").write_text(
        json.dumps(settings, indent="\t"), encoding="utf-8")
    monkeypatch.setattr(b, "BASE_DIR", tmp_path)
    monkeypatch.setattr(b, "FEEDS_CONFIG", json.loads(json.dumps(settings["FEEDS"])))
    monkeypatch.setattr(b, "MCP_CONFIG", json.loads(json.dumps(settings["MCP"])))
    monkeypatch.setattr(b, "NEWS_ENABLED", False)
    tasks = [PeriodicTask("feed", lambda: None),
             PeriodicTask("mentions", lambda: None),
             PeriodicTask("heartbeat", lambda: None, interval_hours=12, enabled=False)]
    monkeypatch.setattr(b, "_RUNTIME_TASKS", tasks)
    monkeypatch.setattr(b, "HEARTBEAT_OVERRIDE_PATH", tmp_path / "context" / "heartbeat_override.md")
    reg = b.build_tool_registry()
    return tmp_path, reg, tasks


def _disk(tmp_path):
    return json.loads((tmp_path / "config" / "settings.json").read_text(encoding="utf-8"))


# ─── set_tool_config ─────────────────────────────────────────────────────────
def test_set_tool_vivo_y_persistido(env):
    tmp, reg, _ = env
    out = reg.execute("set_tool_config", {"tool": "web_search", "enabled": False}, _CTX)
    assert "aplicado en vivo" in out.text
    assert reg.get("web_search").enabled is False                    # vivo
    assert _disk(tmp)["TOOLS"]["web_search"]["enabled"] is False     # disco


def test_set_tool_scopes(env):
    tmp, reg, _ = env
    reg.execute("set_tool_config", {"tool": "web_search", "scopes": ["admin"]}, _CTX)
    assert reg.get("web_search").scopes == frozenset({"admin"})
    assert _disk(tmp)["TOOLS"]["web_search"]["scopes"] == ["admin"]


def test_set_tool_desconocida(env):
    tmp, reg, _ = env
    out = reg.execute("set_tool_config", {"tool": "nope", "enabled": False}, _CTX)
    assert "desconocida" in out.text
    assert "TOOLS" not in _disk(tmp) or not _disk(tmp)["TOOLS"]      # disco intacto


def test_set_tool_scope_invalido_no_toca_runtime(env):
    tmp, reg, _ = env
    antes = reg.get("web_search").scopes
    out = reg.execute("set_tool_config", {"tool": "web_search", "scopes": ["banana"]}, _CTX)
    assert "no apliqué nada" in out.text
    assert reg.get("web_search").scopes == antes                     # rollback implícito
    assert not _disk(tmp)["TOOLS"]                                   # disco intacto


# ─── set_task_config ─────────────────────────────────────────────────────────
def test_set_task_vivo_y_persistido(env):
    tmp, reg, tasks = env
    out = reg.execute("set_task_config",
                      {"task": "heartbeat", "enabled": True, "interval_hours": 6}, _CTX)
    assert "aplicado en vivo" in out.text
    hb = next(t for t in tasks if t.name == "heartbeat")
    assert hb.enabled is True and hb.interval_hours == 6.0
    disk = _disk(tmp)["TASKS"]["heartbeat"]
    assert disk["enabled"] is True and disk["interval_hours"] == 6.0


def test_set_task_desconocida(env):
    _, reg, _ = env
    out = reg.execute("set_task_config", {"task": "yolo", "enabled": True}, _CTX)
    assert "desconocida" in out.text


# ─── set_feed_config / set_news_enabled ──────────────────────────────────────
def test_set_feed(env):
    tmp, reg, _ = env
    out = reg.execute("set_feed_config",
                      {"name": "polcifeed", "posting_policy": "conservative",
                       "interval_hours": 12}, _CTX)
    assert "aplicado en vivo" in out.text
    assert b.FEEDS_CONFIG[0]["posting_policy"] == "conservative"     # vivo
    assert _disk(tmp)["FEEDS"][0]["interval_hours"] == 12.0          # disco


def test_set_feed_politica_invalida(env):
    tmp, reg, _ = env
    out = reg.execute("set_feed_config", {"name": "polcifeed", "posting_policy": "yolo"}, _CTX)
    assert "no apliqué nada" in out.text
    assert b.FEEDS_CONFIG[0]["posting_policy"] == "active"           # intacto


def test_set_news(env):
    tmp, reg, _ = env
    out = reg.execute("set_news_enabled", {"enabled": "true"}, _CTX)  # string del LLM
    assert "activadas" in out.text
    assert b.NEWS_ENABLED is True
    assert _disk(tmp)["NEWS_ENABLED"] is True


# ─── set_heartbeat (todo-en-uno: instrucciones + frecuencia + on/off) ────────
def test_set_heartbeat_completo(env):
    tmp, reg, tasks = env
    out = reg.execute("set_heartbeat", {
        "instructions": "suplicale a un usuario random que te dé dulce de batata",
        "interval_hours": 0.0833, "enabled": True}, _CTX)
    assert "dulce de batata" in out.text and "5 minutos" in out.text
    assert "aplicado en vivo" in out.text
    hb = next(t for t in tasks if t.name == "heartbeat")
    assert hb.enabled is True and abs(hb.interval_hours - 0.0833) < 1e-6   # vivo
    assert _disk(tmp)["TASKS"]["heartbeat"]["enabled"] is True             # disco
    assert "dulce de batata" in b.HEARTBEAT_OVERRIDE_PATH.read_text(encoding="utf-8")


def test_set_heartbeat_borrar_instrucciones(env):
    _, reg, _ = env
    reg.execute("set_heartbeat", {"instructions": "algo"}, _CTX)
    out = reg.execute("set_heartbeat", {"instructions": ""}, _CTX)
    assert "borré" in out.text
    assert b.HEARTBEAT_OVERRIDE_PATH.read_text(encoding="utf-8") == ""


def test_set_heartbeat_sin_args(env):
    _, reg, _ = env
    out = reg.execute("set_heartbeat", {}, _CTX)
    assert "decime qué" in out.text


def test_set_heartbeat_es_config_tool_protegida(env):
    _, reg, _ = env
    out = reg.execute("set_tool_config", {"tool": "set_heartbeat", "enabled": False}, _CTX)
    assert "anti-lockout" in out.text


# ─── set_mcp_enabled ─────────────────────────────────────────────────────────
def test_set_mcp_solo_persiste(env):
    tmp, reg, _ = env
    out = reg.execute("set_mcp_enabled", {"server": "reddit", "enabled": True}, _CTX)
    assert "REINICIÁ" in out.text
    assert _disk(tmp)["MCP"]["reddit"]["enabled"] is True
    assert b.MCP_CONFIG["reddit"]["enabled"] is True                 # visible en get


# ─── locks (análisis de superficie, pedido del admin) ────────────────────────
def test_guard_claves_prohibidas(env, monkeypatch):
    tmp, reg, _ = env
    errs = b._persist_settings_delta(lambda s: s.update(ADMIN_HANDLE="atacante.com"))
    assert errs and "prohibido" in errs[0]
    assert _disk(tmp)["ADMIN_HANDLE"] == "ppolci.com"


def test_guard_endpoint_legacy_prohibido(env):
    tmp, _, _ = env
    errs = b._persist_settings_delta(lambda s: s.update(OPENAI_ENDPOINT="https://atacante.com/v1"))
    assert errs and "OPENAI_ENDPOINT" in errs[0]
    assert "OPENAI_ENDPOINT" not in _disk(tmp)


def test_lock_tools_de_config_intocables(env):
    tmp, reg, _ = env
    out = reg.execute("set_tool_config", {"tool": "set_tool_config", "enabled": False}, _CTX)
    assert "anti-lockout" in out.text
    assert reg.get("set_tool_config").enabled is True
    # y el guard también lo frena aunque un handler futuro lo intente:
    errs = b._persist_settings_delta(
        lambda s: s.setdefault("TOOLS", {}).update(get_bot_config={"enabled": False}))
    assert errs and "configuración" in errs[0]


def test_lock_no_ampliar_scopes_publicos(env):
    tmp, reg, _ = env
    antes = reg.get("save_to_memory").scopes            # admin-only
    out = reg.execute("set_tool_config",
                      {"tool": "save_to_memory", "scopes": ["reply", "admin"]}, _CTX)
    assert "AGREGAR scopes públicos" in out.text
    assert reg.get("save_to_memory").scopes == antes    # intacto
    assert not _disk(tmp)["TOOLS"]                      # disco intacto
    # guard directo (defensa en profundidad):
    errs = b._persist_settings_delta(
        lambda s: s.setdefault("TOOLS", {}).update(save_to_memory={"scopes": ["reply"]}))
    assert errs and "ampliar scopes públicos" in errs[0]


def test_lock_reducir_scopes_si_se_puede(env):
    tmp, reg, _ = env
    out = reg.execute("set_tool_config", {"tool": "web_search", "scopes": ["admin"]}, _CTX)
    assert "aplicado en vivo" in out.text               # quitar reply/feed: permitido
    assert reg.get("web_search").scopes == frozenset({"admin"})


def test_lock_mentions_no_se_apaga(env):
    tmp, reg, _ = env
    out = reg.execute("set_task_config", {"task": "mentions", "enabled": False}, _CTX)
    assert "anti-lockout" in out.text
    assert "TASKS" not in _disk(tmp) or "mentions" not in _disk(tmp).get("TASKS", {})


# ─── get_bot_config ──────────────────────────────────────────────────────────
def test_get_refleja_estado_vivo(env):
    _, reg, _ = env
    reg.execute("set_tool_config", {"tool": "web_search", "enabled": False}, _CTX)
    out = reg.execute("get_bot_config", {}, _CTX)
    assert "web_search" in out.text and "apagadas" in out.text
    assert "heartbeat=off" in out.text


def test_config_tools_son_solo_admin(env):
    _, reg, _ = env
    for name in ("get_bot_config", "set_tool_config", "set_task_config",
                 "set_feed_config", "set_news_enabled", "set_mcp_enabled"):
        assert reg.get(name).scopes == frozenset({"admin"}), name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
