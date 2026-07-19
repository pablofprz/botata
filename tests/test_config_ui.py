"""Tests del panel de configuración (T22): validadores, store atómico, API HTTP."""
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_ui as cu

_SETTINGS = {
    "BOT_HANDLE": "bot.test", "ADMIN_HANDLE": "admin.test",
    "POLL_INTERVAL_SECONDS": 60,
    "FEEDS": [{"name": "f1", "type": "list", "uri": "at://x/lista/1",
               "posting_policy": "balanced", "enabled": True}],
    "TOOLS": {"web_search": {"enabled": True, "scopes": ["reply", "admin"]}},
    "TASKS": {"heartbeat": {"enabled": False, "interval_hours": 12}},
    "MCP": {"reddit": {"transport": "stdio", "command": "python", "enabled": False}},
    "MODELS": {
        "endpoints": {"or": {"base_url": "https://x", "api_key_env": "K"}},
        "aliases": {"reasoning": [{"endpoint": "or", "model": "m1"}]},
        "roles": {"reply": "reasoning"},
    },
    "BUDGET": {"enabled": True, "daily_usd": 1.5, "announce": True},
}


@pytest.fixture
def store(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text(
        json.dumps(_SETTINGS, indent="\t"), encoding="utf-8")
    (tmp_path / "config" / "news_sites.json").write_text("[]", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "BSKY_PASSWORD=secreto123\n# comentario\nYOUTUBE_API_KEY =conespacio\n",
        encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "s1.md").write_text(
        "---\nname: s1\ndescription: una skill\nenabled: true\n---\ncuerpo",
        encoding="utf-8")
    return cu.ConfigStore(tmp_path)


# ─── Validadores ─────────────────────────────────────────────────────────────
def test_settings_valido_pasa():
    assert cu.validate_settings(_SETTINGS) == []


@pytest.mark.parametrize("mutacion,fragmento", [
    (lambda s: s.pop("BOT_HANDLE"), "BOT_HANDLE"),
    (lambda s: s["FEEDS"][0].update(type="banana"), "type inválido"),
    (lambda s: s["FEEDS"][0].update(posting_policy="yolo"), "posting_policy"),
    (lambda s: s["TOOLS"]["web_search"].update(scopes=["reply", "banana"]), "scope"),
    (lambda s: s["MCP"]["reddit"].pop("command"), "requiere command"),
    (lambda s: s["MODELS"]["roles"].update(reply="inexistente"), "alias desconocido"),
    (lambda s: s["MODELS"]["aliases"]["reasoning"][0].update(endpoint="nope"), "endpoint desconocido"),
    (lambda s: s["BUDGET"].update(daily_usd=0), "daily_usd"),
    (lambda s: s["BUDGET"].update(daily_usd="mucho"), "daily_usd"),
    (lambda s: s["BUDGET"].update(enabled="si"), "BUDGET.enabled"),
])
def test_settings_invalido_falla(mutacion, fragmento):
    s = json.loads(json.dumps(_SETTINGS))
    mutacion(s)
    errs = cu.validate_settings(s)
    assert errs and any(fragmento in e for e in errs)


def test_news_valida():
    assert cu.validate_news([{"url": "https://x/rss", "title": "X", "mode": "comment"}]) == []
    errs = cu.validate_news([{"url": "ftp://no", "title": "", "mode": "yolo"}])
    assert len(errs) == 3


# ─── Store ───────────────────────────────────────────────────────────────────
def test_write_settings_atomico_con_backup(store):
    nuevo = json.loads(json.dumps(_SETTINGS))
    nuevo["POLL_INTERVAL_SECONDS"] = 120
    assert store.write_settings(nuevo) == []
    assert store.settings_path.with_suffix(".json.bak").exists()
    releido = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert releido["POLL_INTERVAL_SECONDS"] == 120
    assert "\t" in store.settings_path.read_text(encoding="utf-8")   # preserva tabs


def test_write_settings_invalido_no_toca_el_archivo(store):
    antes = store.settings_path.read_text(encoding="utf-8")
    roto = json.loads(json.dumps(_SETTINGS)); roto.pop("BOT_HANDLE")
    assert store.write_settings(roto)                                 # errores
    assert store.settings_path.read_text(encoding="utf-8") == antes


def test_env_status_sin_valores(store):
    status = store._env_status()
    assert status["BSKY_PASSWORD"] is True
    assert status["BRAVE_API_KEY"] is False
    assert "secreto123" not in json.dumps(status)


def test_update_env_preserva_lo_demas(store):
    assert store.update_env({"BRAVE_API_KEY": "nueva-key"}) == []
    text = store.env_path.read_text(encoding="utf-8")
    assert "BSKY_PASSWORD=secreto123" in text        # intacta
    assert "# comentario" in text                    # comentario intacto
    assert "BRAVE_API_KEY=nueva-key" in text         # agregada
    # actualizar una existente con espacio raro en el nombre
    store.update_env({"YOUTUBE_API_KEY": "otra"})
    assert "YOUTUBE_API_KEY=otra" in store.env_path.read_text(encoding="utf-8")


def test_update_env_vacio_no_toca(store):
    antes = store.env_path.read_text(encoding="utf-8")
    store.update_env({"BSKY_PASSWORD": "  ", "NO_PERMITIDA": "x"})
    assert store.env_path.read_text(encoding="utf-8") == antes


def test_skill_toggle(store):
    assert store.set_skill_enabled("s1", False) == []
    text = (store.skills_dir / "s1.md").read_text(encoding="utf-8")
    assert "enabled: false" in text and "cuerpo" in text
    assert store.set_skill_enabled("nope", True)     # desconocida → error


# ─── HTTP ────────────────────────────────────────────────────────────────────
@pytest.fixture
def server(store, tmp_path):
    httpd = cu.serve(tmp_path, port=0)               # puerto libre
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_config_no_expone_secretos(server):
    data = _get(server + "/api/config")
    assert data["settings"]["BOT_HANDLE"] == "bot.test"
    assert data["env_keys"]["BSKY_PASSWORD"] is True
    assert "secreto123" not in json.dumps(data)
    assert data["skills"][0]["name"] == "s1"


def test_api_settings_invalido_400(server, store):
    antes = store.settings_path.read_text(encoding="utf-8")
    roto = json.loads(json.dumps(_SETTINGS)); roto["FEEDS"][0]["type"] = "banana"
    code, body = _post(server + "/api/settings", roto)
    assert code == 400 and body["errors"]
    assert store.settings_path.read_text(encoding="utf-8") == antes


def test_api_settings_valido_escribe(server, store):
    nuevo = json.loads(json.dumps(_SETTINGS))
    nuevo["TASKS"]["heartbeat"]["enabled"] = True
    code, body = _post(server + "/api/settings", nuevo)
    assert code == 200 and body["ok"]
    releido = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert releido["TASKS"]["heartbeat"]["enabled"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
