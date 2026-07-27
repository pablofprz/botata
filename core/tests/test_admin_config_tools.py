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
        "USER_GROUPS": {"music_users": ["fulano.bsky.social"], "vip": ["mengano.com"]},
        "TASKS": {"reflection": {"enabled": False, "interval_hours": 12}},
        "MCP": {"reddit": {"transport": "stdio", "command": "python", "enabled": False}},
    }
    (tmp_path / "config" / "settings.json").write_text(
        json.dumps(settings, indent="\t"), encoding="utf-8")
    monkeypatch.setattr(b, "BASE_DIR", tmp_path)
    monkeypatch.setattr(b, "FEEDS_CONFIG", json.loads(json.dumps(settings["FEEDS"])))
    monkeypatch.setattr(b, "MCP_CONFIG", json.loads(json.dumps(settings["MCP"])))
    monkeypatch.setattr(b, "NEWS_ENABLED", False)
    monkeypatch.setattr(b, "USER_GROUPS", json.loads(json.dumps(settings["USER_GROUPS"])))
    tasks = [PeriodicTask("feed", lambda: None),
             PeriodicTask("mentions", lambda: None),
             PeriodicTask("reflection", lambda: None, interval_hours=12, enabled=False)]
    monkeypatch.setattr(b, "_RUNTIME_TASKS", tasks)
    monkeypatch.setattr(b, "ROUTINES_DIR", tmp_path / "routines")
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


def test_set_tool_groups_restringe_ok(env):
    tmp, reg, _ = env
    out = reg.execute("set_tool_config", {"tool": "search_music", "groups": ["music_users"]}, _CTX)
    assert "aplicado en vivo" in out.text
    assert reg.get("search_music").groups == frozenset({"music_users"})          # vivo
    assert _disk(tmp)["TOOLS"]["search_music"]["groups"] == ["music_users"]      # disco


def test_set_tool_groups_desconocido(env):
    tmp, reg, _ = env
    out = reg.execute("set_tool_config", {"tool": "search_music", "groups": ["nope"]}, _CTX)
    assert "desconocido" in out.text
    assert reg.get("search_music").groups is None
    assert not _disk(tmp)["TOOLS"]


def test_set_tool_groups_no_se_afloja_por_comando(env):
    tmp, reg, _ = env
    reg.execute("set_tool_config", {"tool": "search_music", "groups": ["music_users"]}, _CTX)
    # sumar un grupo = más gente → rechazado
    out = reg.execute("set_tool_config",
                      {"tool": "search_music", "groups": ["music_users", "vip"]}, _CTX)
    assert "AFLOJAR" in out.text
    # quitar la restricción → rechazado
    out = reg.execute("set_tool_config", {"tool": "search_music", "groups": []}, _CTX)
    assert "AFLOJAR" in out.text
    assert reg.get("search_music").groups == frozenset({"music_users"})
    assert _disk(tmp)["TOOLS"]["search_music"]["groups"] == ["music_users"]


def test_guard_user_groups_protegido(env):
    tmp, reg, _ = env
    # ningún comando debe poder editar las membresías (ampliar exposición = UI)
    errs = b._persist_settings_delta(
        lambda s: s["USER_GROUPS"]["music_users"].append("intruso.bsky.social"))
    assert errs and "USER_GROUPS" in errs[0]
    assert _disk(tmp)["USER_GROUPS"]["music_users"] == ["fulano.bsky.social"]


# ─── set_task_config ─────────────────────────────────────────────────────────
def test_set_task_vivo_y_persistido(env):
    tmp, reg, tasks = env
    out = reg.execute("set_task_config",
                      {"task": "reflection", "enabled": True, "interval_hours": 6}, _CTX)
    assert "aplicado en vivo" in out.text
    rt = next(t for t in tasks if t.name == "reflection")
    assert rt.enabled is True and rt.interval_hours == 6.0
    disk = _disk(tmp)["TASKS"]["reflection"]
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


# ─── rutinas por comando (set_routine / delete_routine, solo admin) ──────────
def test_set_heartbeat_ya_no_existe(env):
    """La tool set_heartbeat se eliminó con la unificación en rutinas."""
    _, reg, _ = env
    assert reg.get("set_heartbeat") is None


def test_get_config_lista_rutinas(env, tmp_path):
    _, reg, _ = env
    out = reg.execute("get_bot_config", {}, _CTX)
    assert "rutinas: ninguna" in out.text
    rd = tmp_path / "routines"
    rd.mkdir()
    (rd / "memes.md").write_text("---\ninterval_hours: 4\n---\nposteá memes\n",
                                 encoding="utf-8")
    out = reg.execute("get_bot_config", {}, _CTX)
    assert "memes" in out.text


def test_set_routine_crea(env, tmp_path):
    _, reg, _ = env
    out = reg.execute("set_routine", {
        "name": "Memes", "instructions": "posteá un meme, no repitas",
        "interval_hours": 4}, _CTX)
    assert "creada" in out.text and "4 h" in out.text
    import routines as r
    rt = r.load_routines(tmp_path / "routines")[0]
    assert rt.name == "memes" and rt.interval_hours == 4.0 and rt.channel == ""
    assert rt.body == "posteá un meme, no repitas" and rt.enabled


def test_set_routine_update_parcial_conserva_cuerpo(env, tmp_path):
    _, reg, _ = env
    reg.execute("set_routine", {"name": "canciones", "instructions": "compartí un tema",
                                "interval_hours": 4}, _CTX)
    out = reg.execute("set_routine", {"name": "canciones", "interval_hours": 6}, _CTX)
    assert "actualizada" in out.text
    import routines as r
    rt = r.load_routines(tmp_path / "routines")[0]
    assert rt.interval_hours == 6.0 and rt.body == "compartí un tema"


def test_set_routine_apagar_y_borrar(env, tmp_path):
    _, reg, _ = env
    reg.execute("set_routine", {"name": "memes", "instructions": "x", "interval_hours": 4}, _CTX)
    out = reg.execute("set_routine", {"name": "memes", "enabled": False}, _CTX)
    assert "apagada" in out.text
    import routines as r
    assert r.load_routines(tmp_path / "routines") == []  # deshabilitada no corre
    out = reg.execute("delete_routine", {"name": "memes"}, _CTX)
    assert "borrada" in out.text
    assert not (tmp_path / "routines" / "memes.md").exists()
    out = reg.execute("delete_routine", {"name": "memes"}, _CTX)
    assert "no tengo" in out.text


def test_set_routine_validaciones(env, tmp_path):
    _, reg, _ = env
    # crear sin instrucciones → rebota
    out = reg.execute("set_routine", {"name": "nueva", "interval_hours": 2}, _CTX)
    assert "necesito las" in out.text
    # nombre inválido (anti-traversal incluido)
    for bad in ("../evil", "con espacios", "readme"):
        out = reg.execute("set_routine", {"name": bad, "instructions": "x"}, _CTX)
        assert "inválido" in out.text, bad
    # channel no numérico
    out = reg.execute("set_routine", {"name": "x", "instructions": "x",
                                      "channel": "banana"}, _CTX)
    assert "numérico" in out.text
    assert not (tmp_path / "routines").exists()  # nada se escribió


def test_routine_tools_son_solo_admin(env):
    _, reg, _ = env
    assert reg.get("set_routine").scopes == frozenset({"admin"})
    assert reg.get("delete_routine").scopes == frozenset({"admin"})


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
    assert "reflection=off" in out.text


def test_config_tools_son_solo_admin(env):
    _, reg, _ = env
    for name in ("get_bot_config", "set_tool_config", "set_task_config",
                 "set_feed_config", "set_news_enabled", "set_mcp_enabled"):
        assert reg.get(name).scopes == frozenset({"admin"}), name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
