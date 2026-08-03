"""Tests del panel de configuración (T22): validadores, store atómico, API HTTP."""
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config_ui as cu

_SETTINGS = {
    "BOT_HANDLE": "bot.test", "ADMIN_HANDLE": "admin.test",
    "POLL_INTERVAL_SECONDS": 60,
    "FEEDS": [{"name": "f1", "type": "list", "uri": "at://x/lista/1",
               "enabled": True}],
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


@pytest.fixture(autouse=True)
def _restaurar_local_tz():
    """El store aplica el TIMEZONE de sus settings con set_local_tz — un GLOBAL
    de db.py. Los tests de mood usan Asia/Tokyo, y sin restore quedaba pegada
    para el resto de la suite: test_events_today_ar fallaba solo cuando Tokio
    ya había cruzado la medianoche (flaky por hora del día, 12:00-21:00 AR)."""
    import db as dbmod
    saved = dbmod.LOCAL_TZ
    yield
    dbmod.LOCAL_TZ = saved


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
    (lambda s: s["TOOLS"]["web_search"].update(scopes=["reply", "banana"]), "scope"),
    (lambda s: s["MCP"]["reddit"].pop("command"), "requiere command"),
    (lambda s: s["MODELS"]["roles"].update(reply="inexistente"), "alias desconocido"),
    (lambda s: s["MODELS"]["aliases"]["reasoning"][0].update(endpoint="nope"), "endpoint desconocido"),
    (lambda s: s["BUDGET"].update(daily_usd=0), "daily_usd"),
    (lambda s: s["BUDGET"].update(daily_usd="mucho"), "daily_usd"),
    (lambda s: s["BUDGET"].update(enabled="si"), "BUDGET.enabled"),
    (lambda s: s.update(USER_GROUPS="banana"), "USER_GROUPS"),
    (lambda s: s.update(USER_GROUPS={"g": ["ok", ""]}), "USER_GROUPS.g"),
    (lambda s: s["TOOLS"]["web_search"].update(groups=["fantasma"]), "desconocido"),
    (lambda s: s["TOOLS"]["web_search"].update(groups="music_users"), "lista"),
    (lambda s: s.update(USER_GROUPS={"g": ["feed:fantasma"]}), "no matchea"),
    (lambda s: s.update(BOT_ACTIONS_FROM="banana"), "BOT_ACTIONS_FROM"),
    (lambda s: s.update(READ_THREAD_MEDIA="si"), "READ_THREAD_MEDIA"),
    (lambda s: s.update(TIMEZONE="Zona/Inexistente"), "TIMEZONE"),
])
def test_settings_invalido_falla(mutacion, fragmento):
    s = json.loads(json.dumps(_SETTINGS))
    mutacion(s)
    errs = cu.validate_settings(s)
    assert errs and any(fragmento in e for e in errs)


@pytest.mark.parametrize("extra, fragmento", [
    # Sin chats el bot no tiene dónde vivir — y como el dispositivo vinculado
    # recibe TODOS los mensajes del número, la lista vacía no es "default": es
    # la diferencia entre atender un grupo y atender tus chats personales.
    ({}, "WHATSAPP_CHAT_IDS"),
    ({"WHATSAPP_CHAT_IDS": []}, "WHATSAPP_CHAT_IDS"),
    ({"WHATSAPP_CHAT_IDS": ["x@g.us"], "WHATSAPP_BRIDGE_URL": ""}, "WHATSAPP_BRIDGE_URL"),
    ({"WHATSAPP_CHAT_IDS": ["x@g.us"],
      "WHATSAPP_BRIDGE_URL": "http://10.0.0.5:8787"}, "loopback"),
])
def test_whatsapp_incompleto_falla(extra, fragmento):
    s = json.loads(json.dumps(_SETTINGS))
    s["CHANNEL"] = "whatsapp"
    s.update(extra)
    errs = cu.validate_settings(s)
    assert errs and any(fragmento in e for e in errs)


def test_whatsapp_completo_pasa():
    s = json.loads(json.dumps(_SETTINGS))
    s.update(CHANNEL="whatsapp", WHATSAPP_CHAT_IDS=["5491111111111-1431400469@g.us"],
             WHATSAPP_BRIDGE_URL="http://127.0.0.1:8787",
             ADMIN_HANDLE="+54 9 11 1111-1111", ADMIN_HANDLES=["5491122222222"])
    assert cu.validate_settings(s) == []


@pytest.mark.parametrize("extra, fragmento", [
    # El caso real: una instancia clonada de otra red se queda con el handle de
    # allá y en WhatsApp no matchea con nadie — el bot pierde el admin y no lo
    # dice. Si no se avisa acá, no se avisa en ningún lado.
    ({"ADMIN_HANDLE": "ppolci.com"}, "ADMIN_HANDLE"),
    ({"ADMIN_HANDLES": ["otro.bsky.social"]}, "ADMIN_HANDLES"),
    ({"USER_GROUPS": {"power": ["fulano.com"]}}, "USER_GROUPS.power"),
])
def test_whatsapp_avisa_si_el_admin_no_es_un_telefono(extra, fragmento):
    s = json.loads(json.dumps(_SETTINGS))
    s.update(CHANNEL="whatsapp", WHATSAPP_CHAT_IDS=["1@g.us"],
             WHATSAPP_BRIDGE_URL="http://127.0.0.1:8787",
             ADMIN_HANDLE="5491111111111")
    s.update(extra)
    assert any(fragmento in e and "teléfono" in e for e in cu.validate_settings(s))


def test_user_groups_validos_pasan():
    s = json.loads(json.dumps(_SETTINGS))
    s["USER_GROUPS"] = {"music_users": ["fulano.bsky.social", "feed:f1"]}
    s["TOOLS"]["web_search"]["groups"] = ["music_users"]
    assert cu.validate_settings(s) == []


def test_sources_valida():
    assert cu.validate_sources(
        [{"type": "rss", "name": "X", "sources": ["https://x/rss"]}]) == []
    assert cu.validate_sources(
        [{"type": "membrilla", "category": "futbol", "sources": ["cuenta"]}]) == []


def test_sources_invalidas():
    # tipo desconocido, sin fuentes, y una URL de RSS que no es URL
    assert any("type inválido" in e for e in cu.validate_sources([{"type": "yolo"}]))
    assert any("sin fuentes" in e for e in
               cu.validate_sources([{"type": "rss", "name": "X", "sources": []}]))
    assert any("no es una URL" in e for e in cu.validate_sources(
        [{"type": "rss", "name": "X", "sources": ["ftp://no"]}]))
    assert any("tema" in e for e in cu.validate_sources(
        [{"type": "membrilla", "sources": ["x"]}]))


# ─── Conector declarativo `api`: config, prueba y credenciales ───────────────
_API_OK = {"type": "api", "category": "pokemon", "sources": ["pikachu"],
           "url": "https://pokeapi.co/api/v2/pokemon/{source}",
           "map": {"image_url": "sprites.front_default"}}


def test_sources_api_valida():
    assert cu.validate_sources([_API_OK]) == []


def test_sources_api_invalida():
    assert any("falta la URL" in e for e in
               cu.validate_sources([{**_API_OK, "url": ""}]))
    assert any("http" in e for e in
               cu.validate_sources([{**_API_OK, "url": "file:///etc/passwd"}]))
    assert any("imagen" in e for e in cu.validate_sources([{**_API_OK, "map": {}}]))


def test_sources_api_no_exige_la_credencial_para_guardar():
    # Se puede describir la fuente hoy y cargar la clave en el .env después.
    assert cu.validate_sources(
        [{**_API_OK, "url": "https://x/y?k={env:CLAVE_QUE_NO_EXISTE}"}]) == []


def test_probar_fuente_api_devuelve_el_error_explicado(store, monkeypatch):
    def _boom(source, limit=15, entry=None):
        raise cu.connectorsmod.ApiSourceError("no encontré 'results' en la respuesta")
    monkeypatch.setattr(cu.connectorsmod, "api_items", _boom)
    r = store.test_source({"entry": _API_OK})
    assert r["ok"] is False and "results" in r["error"]


def test_probar_fuente_api_ok_devuelve_una_muestra(store, monkeypatch):
    monkeypatch.setattr(cu.connectorsmod, "api_items",
                        lambda s, l=15, e=None: [{"image_url": "https://x/a.png",
                                                  "title": "pikachu", "url": "",
                                                  "source": s}])
    r = store.test_source({"entry": _API_OK})
    assert r["ok"] and r["count"] == 1 and r["sample"]["title"] == "pikachu"
    assert r["source"] == "pikachu"          # usó la primera fuente de la entrada


def test_probar_fuente_sin_fuentes_avisa(store):
    assert store.test_source({"entry": {**_API_OK, "sources": []}})["ok"] is False


def test_probar_fuente_avisa_que_falta_la_credencial(store, monkeypatch):
    monkeypatch.delenv("CLAVE_INEXISTENTE", raising=False)
    r = store.test_source({"entry": {
        **_API_OK, "url": "https://x/y?k={env:CLAVE_INEXISTENTE}"}})
    assert r["ok"] is False and r["missing_env"] == ["CLAVE_INEXISTENTE"]


def test_credencial_pedida_por_una_fuente_se_puede_guardar(store):
    """Sin esto el conector declarativo no serviría para ninguna API con token."""
    store.write_sources([{**_API_OK, "url": "https://x/y?k={env:POKE_KEY}"}])
    assert "POKE_KEY" in store.read_all()["env_keys"]
    assert store.update_env({"POKE_KEY": "abc123"}) == []
    assert store.read_all()["env_keys"]["POKE_KEY"] is True


def test_no_se_pueden_escribir_claves_arbitrarias_ni_reservadas(store):
    # No referenciada por ninguna fuente: la UI no la ofrece ni la escribe.
    store.update_env({"CUALQUIER_COSA": "x"})
    assert "CUALQUIER_COSA" not in store.env_path.read_text(encoding="utf-8")
    # Referenciada, pero gobierna el proceso: tampoco.
    store.write_sources([{**_API_OK, "url": "https://x/y?k={env:PATH}"}])
    assert "PATH" not in store.read_all()["env_keys"]
    store.update_env({"PATH": "/tmp/malo"})
    assert "PATH=" not in store.env_path.read_text(encoding="utf-8")


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


def test_sources_roundtrip(store):
    assert store.write_sources(
        [{"type": "membrilla", "category": "futbol", "sources": ["a", "b"], "enabled": True}]) == []
    guardado = store.read_all()["sources"][0]
    assert guardado["sources"] == ["a", "b"] and guardado["type"] == "membrilla"


def test_sources_invalido_no_escribe(store):
    store.write_sources([{"type": "membrilla", "category": "ok", "sources": ["a"]}])
    assert store.write_sources([{"type": "membrilla", "category": "x", "sources": []}])
    assert store.read_all()["sources"][0]["category"] == "ok"     # intacto


def test_sources_migra_los_registros_viejos(store):
    """Una instancia sin sources.json ve igual sus feeds RSS de siempre."""
    (store.base / "config" / "news_sites.json").write_text(
        json.dumps([{"url": "https://x/rss", "title": "X", "category": "noticias"}]),
        encoding="utf-8")
    migrado = store.read_all()["sources"]
    assert migrado[0]["type"] == "rss" and migrado[0]["sources"] == ["https://x/rss"]
    # y al guardar, el archivo viejo se retira para que no queden dos verdades
    assert store.write_sources(migrado) == []
    assert not (store.base / "config" / "news_sites.json").exists()
    assert (store.base / "config" / "sources.json").exists()


# ─── Membrilla: lanzador del scraper (settings.MEMBRILLA) ────────────────────
def test_membrilla_sin_config_da_instrucciones(store):
    out = store.run_action("membrilla_scrape")
    assert out["ok"] is False
    assert any("sin configurar" in e for e in out["errors"])


def test_membrilla_dice_CUAL_mitad_falta(store):
    """Con el repo puesto y los comandos vacíos, "sin configurar" manda a
    revisar lo que ya estaba bien. Caso real del admin."""
    s = json.loads(store.settings_path.read_text(encoding="utf-8"))
    s["MEMBRILLA"] = {"repo": str(store.base), "commands": []}
    store.settings_path.write_text(json.dumps(s), encoding="utf-8")
    err = store.run_action("membrilla_scrape")["errors"][0]
    assert "commands está vacío" in err and str(store.base) in err

    s["MEMBRILLA"] = {"repo": "", "commands": ["echo hola"]}
    store.settings_path.write_text(json.dumps(s), encoding="utf-8")
    err = store.run_action("membrilla_scrape")["errors"][0]
    assert "Falta la ruta del repo" in err


def test_membrilla_repo_inexistente(store):
    s = json.loads(store.settings_path.read_text(encoding="utf-8"))
    s["MEMBRILLA"] = {"repo": str(store.base / "no-existe"), "commands": ["echo hola"]}
    store.settings_path.write_text(json.dumps(s), encoding="utf-8")
    out = store.run_action("membrilla_scrape")
    assert out["ok"] is False
    assert any("no existe" in e for e in out["errors"])


def test_membrilla_corre_comandos_con_output_dir(store, tmp_path):
    repo = tmp_path / "membrilla"; repo.mkdir()
    s = json.loads(store.settings_path.read_text(encoding="utf-8"))
    # el comando imprime la env var que le inyectamos (portátil vía python)
    s["MEMBRILLA"] = {"repo": str(repo), "commands": [
        f'"{sys.executable}" -c "import os; print(os.environ[\'MEMBRILLA_OUTPUT_DIR\'])"']}
    store.settings_path.write_text(json.dumps(s), encoding="utf-8")
    out = store.run_action("membrilla_scrape")
    assert out["ok"] is True
    assert "scrape" in out["output"]          # apunta al scrape/ de la instancia


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


# ─── Validadores MOODS / PREFS ────────────────────────────────────────────────
@pytest.mark.parametrize("moods,fragmento", [
    ({"mode": "banana"}, "MOODS.mode"),
    ({"susceptibility": 1.5}, "susceptibility"),
    ({"hysteresis_hours": -1}, "hysteresis_hours"),
    ({"manual": {"schedule": {"mon": "upbeat"}}}, "ya no existe"),
])
def test_validate_moods_invalido(moods, fragmento):
    s = json.loads(json.dumps(_SETTINGS)); s["MOODS"] = moods
    assert any(fragmento in e for e in cu.validate_settings(s))


def test_validate_moods_y_prefs_validos():
    s = json.loads(json.dumps(_SETTINGS))
    s["MOODS"] = {"enabled": True, "mode": "auto", "susceptibility": 0.5,
                  "hysteresis_hours": 2, "manual": {"fixed": "", "schedule": {}}}
    s["PREFS"] = {"mode": "add_only"}
    assert cu.validate_settings(s) == []


def test_validate_prefs_invalido():
    s = json.loads(json.dumps(_SETTINGS)); s["PREFS"] = {"mode": "yolo"}
    assert any("PREFS.mode" in e for e in cu.validate_settings(s))


# ─── Datos vivos del DB (memoria / preferencias / calendario) ─────────────────
def test_data_endpoints_crud(store):
    assert store.edit_memory({"action": "add", "text": "el bot es peronista"}) == []
    assert store.edit_preferences({"action": "add", "kind": "like", "text": "los panchos"}) == []
    assert store.edit_events({"action": "add", "title": "juntada",
                              "event_at": "2099-01-01T21:00"}) == []
    data = store.read_data()
    assert data["memory"][0]["text"] == "el bot es peronista"
    assert data["memory"][0]["source"] == "admin"
    assert data["preferences"][0]["kind"] == "like"
    assert data["events"][0]["title"] == "juntada"

    assert store.edit_memory({"action": "delete", "id": data["memory"][0]["id"]}) == []
    assert store.edit_preferences({"action": "delete", "id": data["preferences"][0]["id"]}) == []
    assert store.edit_events({"action": "delete", "id": data["events"][0]["id"]}) == []
    data = store.read_data()
    assert data["memory"] == [] and data["preferences"] == [] and data["events"] == []


def test_data_validaciones(store):
    assert store.edit_memory({"action": "add", "text": "  "}) != []
    assert store.edit_preferences({"action": "add", "kind": "meh", "text": "x"}) != []
    assert store.edit_events({"action": "add", "title": "x", "event_at": "mañana"}) != []
    assert store.edit_events({"action": "add", "title": "x", "event_at": "2099-01-01",
                              "handle": "nadie.test"}) != []  # sin perfil → error claro
    assert store.edit_memory({"action": "delete", "id": 999}) == ["id inexistente"]


def test_event_edit_fecha_y_recurrencia(store):
    assert store.edit_events({"action": "add", "title": "juntada",
                              "event_at": "2099-01-01T21:00"}) == []
    ev = store.read_data()["events"][0]
    assert store.edit_events({"action": "edit", "id": ev["id"],
                              "event_at": "2099-01-02T22:30", "recur": "weekly"}) == []
    ev2 = store.read_data()["events"][0]
    assert ev2["event_at"] == "2099-01-02T22:30" and ev2["recur"] == "weekly"
    # sacar la recurrencia (vuelve a "una sola vez")
    assert store.edit_events({"action": "edit", "id": ev["id"],
                              "event_at": "2099-01-02T22:30", "recur": ""}) == []
    assert store.read_data()["events"][0]["recur"] is None
    # validaciones
    assert store.edit_events({"action": "edit", "id": 999,
                              "event_at": "2099-01-01T10:00"}) == ["id inexistente"]
    assert store.edit_events({"action": "edit", "id": ev["id"],
                              "event_at": "mañana"}) != []
    assert store.edit_events({"action": "edit", "id": ev["id"],
                              "event_at": "2099-01-01T10:00", "recur": "cada tanto"}) != []


# ─── Mood en vivo desde la UI ────────────────────────────────────────────────
# El humor del bot es lo único de la UI que cambia SOLO mientras el panel está
# abierto, así que la vista y la escritura tienen que hablar el mismo idioma:
# la zona horaria de la instancia. Con un -3 hardcodeado (como estaba), en
# cualquier instancia fuera de Argentina el mood elegido acá se guardaba con
# otra fecha y current_mood() lo ignoraba: el admin lo cambiaba y no pasaba nada.
def _con_moods(store, **cfg):
    s = json.loads(store.settings_path.read_text(encoding="utf-8"))
    s["MOODS"] = {"enabled": True, "mode": "auto", "default": "normal", **cfg}
    s["TIMEZONE"] = "Asia/Tokyo"          # +9: nada que ver con el -3 de antes
    store.settings_path.write_text(json.dumps(s), encoding="utf-8")
    store.moods_dir.mkdir(exist_ok=True)
    for n in ("normal", "snarky"):
        (store.moods_dir / f"{n}.md").write_text(
            f"---\nname: {n}\ndescription: d\n---\ncuerpo", encoding="utf-8")


def test_mood_de_hoy_usa_la_zona_de_la_instancia(store):
    """Tokio: +9. Con el -3 de antes, el estado quedaba fechado en otro día."""
    _con_moods(store)
    assert store.set_mood_today({"mood": "snarky"}) == []
    from datetime import datetime
    from zoneinfo import ZoneInfo
    data = store.read_data()
    assert data["mood_today"] == "snarky"          # el motor lo vería vigente
    assert data["mood_state"]["mode"] == "ui"
    assert data["mood_state"]["date"] == datetime.now(
        ZoneInfo("Asia/Tokyo")).date().isoformat()


def test_mood_de_la_ui_deja_changed_at_para_la_histeresis(store):
    """Sin changed_at el bot podía pisar el humor del admin en la reply siguiente."""
    _con_moods(store)
    store.set_mood_today({"mood": "snarky"})
    from datetime import datetime
    ca = store.read_data()["mood_state"]["changed_at"]
    assert datetime.fromisoformat(ca).tzinfo is not None   # aware, como el motor


def test_mood_today_respeta_apagado_y_modo_fijo(store):
    _con_moods(store, enabled=False)
    assert store.read_data()["mood_today"] is None
    _con_moods(store, mode="manual", manual={"fixed": "snarky"})
    assert store.read_data()["mood_today"] == "snarky"


def test_mood_today_cae_al_default_sin_estado_de_hoy(store):
    _con_moods(store)
    assert store.read_data()["mood_today"] == "normal"
    store.set_mood_today({"mood": "snarky"})
    assert store.set_mood_today({"mood": ""}) == []        # soltar
    assert store.read_data()["mood_today"] == "normal"


def test_mood_inexistente_se_rechaza(store):
    _con_moods(store)
    assert store.set_mood_today({"mood": "eufórico"}) != []


# ─── Importar calendario (CSV / ICS) ──────────────────────────────────────────
_CSV_IMPORT = """fecha,titulo,descripcion,tipo,de_quien,repeticion
2099-08-15,Cumple de Ana,,birthday,ana.test,anual
2099-08-01T21:00,Juntada,en el bar,community,,mensual
2099-09-01,Tipo raro,,fiesta,,
banana,Fecha rota,,,,
"""


def test_import_csv_preview_y_confirmacion(store):
    import db as dbmod
    conn = dbmod.init_db(store.db_path)
    conn.execute("INSERT INTO users(handle) VALUES ('ana.test')")
    conn.commit()
    conn.close()

    prev = store.import_events({"text": _CSV_IMPORT, "dry_run": True})
    assert prev["ok"] and prev["dry_run"]
    estados = {r["titulo"]: r["estado"] for r in prev["rows"]}
    assert estados == {"Cumple de Ana": "ok", "Juntada": "ok",
                       "Tipo raro": "error", "Fecha rota": "error"}
    assert prev["importables"] == 2 and prev["importados"] == 0
    assert store.read_data()["events"] == []  # la vista previa no escribe

    real = store.import_events({"text": _CSV_IMPORT, "dry_run": False})
    assert real["importados"] == 2
    evs = {e["title"]: e for e in store.read_data()["events"]}
    assert evs["Cumple de Ana"]["recur"] == "yearly"    # alias 'anual'
    assert evs["Cumple de Ana"]["handle"] == "ana.test"
    assert evs["Juntada"]["recur"] == "monthly"
    # reimportar → duplicados, no re-inserta
    again = store.import_events({"text": _CSV_IMPORT, "dry_run": False})
    assert again["importados"] == 0
    assert sum(1 for r in again["rows"] if r["estado"] == "duplicado") == 2


def test_import_csv_punto_y_coma_y_vacio(store):
    assert store.import_events({"text": ""})["ok"] is False
    out = store.import_events({"text": "fecha;titulo\n2099-01-01;Año nuevo",
                               "dry_run": False})
    assert out["importados"] == 1
    assert store.import_events({"text": "cualquier,cosa\n1,2"})["ok"] is False  # sin fecha/titulo


def test_import_ics(store):
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "BEGIN:VEVENT",
        "DTSTART;VALUE=DATE:20990815",
        "SUMMARY:Cumple de Ana",
        "RRULE:FREQ=YEARLY",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "DTSTART:20990801T210000Z",
        "SUMMARY:Juntada\\, la de siempre",
        "DESCRIPTION:linea uno\\nlinea dos",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "DTSTART:20990901T120000",
        "SUMMARY:Regla rara",
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TU",
        "END:VEVENT",
        "END:VCALENDAR",
    ])
    out = store.import_events({"text": ics, "dry_run": False})
    estados = {r["titulo"]: r["estado"] for r in out["rows"]}
    assert estados["Cumple de Ana"] == "ok"
    assert estados["Juntada, la de siempre"] == "ok"   # coma des-escapada
    assert estados["Regla rara"] == "error"            # RRULE compuesta
    evs = {e["title"]: e for e in store.read_data()["events"]}
    assert evs["Cumple de Ana"]["recur"] == "yearly"
    assert evs["Juntada, la de siempre"]["event_at"] == "2099-08-01T18:00"  # Z → AR


# ─── Memoria por usuario (visor + borrado con limpieza de embeddings) ─────────
def test_user_memory_viewer_y_borrado(store):
    import numpy as np
    import db as dbmod
    conn = dbmod.init_db(store.db_path)
    emb = np.zeros(dbmod.EMBED_DIM, dtype=np.float32).tobytes()
    conn.execute("INSERT INTO users(handle, display_name) VALUES ('ana.test', 'Ana')")
    conn.execute("INSERT INTO user_facts(id, handle, fact_text) "
                 "VALUES (1, 'ana.test', 'le gustan los gatos')")
    conn.execute("INSERT INTO user_facts_vec(rowid, embedding, partition_key) "
                 "VALUES (1, ?, 'ana.test')", (emb,))
    conn.execute("INSERT INTO interactions(handle, summary) "
                 "VALUES ('ana.test', 'charla de prueba')")
    conn.execute("INSERT INTO lessons(id, lesson_text) VALUES (7, 'lección x')")
    conn.execute("INSERT INTO lessons_vec(rowid, embedding) VALUES (7, ?)", (emb,))
    conn.commit()
    conn.close()

    over = store.read_user_memory()
    assert over["users"][0]["handle"] == "ana.test"
    assert over["users"][0]["facts"] == 1
    assert [x["id"] for x in over["lessons"]] == [7]

    d = store.read_user_memory("@Ana.Test")  # normaliza @ y mayúsculas
    assert d["facts"][0]["fact_text"] == "le gustan los gatos"
    assert d["interactions"][0]["summary"] == "charla de prueba"

    assert store.delete_user_memory({"kind": "fact", "id": 1}) == []
    assert store.delete_user_memory({"kind": "lesson", "id": 7}) == []
    assert store.delete_user_memory({"kind": "fact", "id": 99}) == ["id inexistente"]
    assert store.delete_user_memory({"kind": "banana", "id": 1}) != []
    assert store.delete_user_memory({"kind": "fact", "id": "x"}) != []

    conn = dbmod.init_db(store.db_path)
    for tabla in ("user_facts", "user_facts_vec", "lessons", "lessons_vec"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0] == 0, tabla
    conn.close()


def test_behavior_file_editor(store):
    """Editor de skills/rooms de la UI: crear/leer/borrar con validación de
    frontmatter y sin traversal."""
    skill_md = "---\nname: lore\ndescription: lore de la comunidad\n---\nel lore\n"
    assert store.edit_behavior_file(
        {"kind": "skill", "action": "save", "file": "lore.md", "text": skill_md})["ok"]
    got = store.edit_behavior_file({"kind": "skill", "action": "get", "file": "lore.md"})
    assert got["ok"] and "el lore" in got["text"]
    assert any(s["name"] == "lore" for s in store._skills_info())
    # validaciones de skill
    for text in ("sin frontmatter",
                 "---\nname: x\n---\ncuerpo",             # falta description
                 "---\nname: x\ndescription: d\n---\n "):  # cuerpo vacío
        assert not store.edit_behavior_file(
            {"kind": "skill", "action": "save", "file": "x.md", "text": text})["ok"]
    # nombres/kind inválidos (anti-traversal incluido)
    for body in ({"kind": "banana", "action": "get", "file": "x.md"},
                 {"kind": "skill", "action": "get", "file": "../evil.md"},
                 {"kind": "skill", "action": "get", "file": "sub/x.md"},
                 {"kind": "skill", "action": "get", "file": "README.md"},
                 {"kind": "skill", "action": "banana", "file": "lore.md"}):
        assert not store.edit_behavior_file(body)["ok"]
    # routine: channel opcional (si está, numérico) e interval numérico
    rt_md = "---\nchannel: 123\ninterval_hours: 1\nenabled: true\n---\nmemes acá\n"
    assert store.edit_behavior_file(
        {"kind": "routine", "action": "save", "file": "memes.md", "text": rt_md})["ok"]
    rt_sin_canal = "---\ninterval_hours: 4\n---\nposteá algo al feed\n"
    assert store.edit_behavior_file(
        {"kind": "routine", "action": "save", "file": "general.md", "text": rt_sin_canal})["ok"]
    info = store._routines_info()
    assert {i["name"] for i in info} >= {"memes", "general"}
    memes = next(i for i in info if i["name"] == "memes")
    assert memes["channel"] == "123" and memes["enabled"]
    assert next(i for i in info if i["name"] == "general")["channel"] == ""
    for text in ("---\nchannel: no-numerico\n---\nx\n",
                 "---\nchannel: 123\ninterval_hours: banana\n---\nx\n"):
        assert not store.edit_behavior_file(
            {"kind": "routine", "action": "save", "file": "memes.md", "text": text})["ok"]
    # kind viejo "room" ya no existe
    assert not store.edit_behavior_file(
        {"kind": "room", "action": "save", "file": "memes.md", "text": rt_md})["ok"]
    # delete
    assert store.edit_behavior_file({"kind": "routine", "action": "delete", "file": "memes.md"})["ok"]
    assert store.edit_behavior_file({"kind": "routine", "action": "delete", "file": "general.md"})["ok"]
    assert not store.edit_behavior_file({"kind": "routine", "action": "get", "file": "memes.md"})["ok"]
    assert store._routines_info() == []


def test_edit_mood_con_triggers(store):
    """`triggers` opcional: si viene se escribe la línea, si no se omite."""
    assert store.edit_mood({"action": "save", "name": "angry", "description": "d",
                            "triggers": "me bardearon mucho", "body": "cuerpo"}) == []
    info = next(m for m in store._moods_info() if m["name"] == "angry")
    assert info["triggers"] == "me bardearon mucho"
    assert store.edit_mood({"action": "save", "name": "chill", "description": "d",
                            "body": "cuerpo"}) == []
    assert next(m for m in store._moods_info() if m["name"] == "chill")["triggers"] == ""
    assert "triggers" not in (store.moods_dir / "chill.md").read_text(encoding="utf-8")


def test_user_memory_alta_manual(store, monkeypatch):
    """Alta desde la UI: usa los upserts reales (dedup incluido) con embed mockeado
    (no cargar bge-m3 en tests)."""
    import numpy as np
    import db as dbmod
    # ones, no zeros: la distancia coseno contra el vector nulo es indefinida (NULL)
    monkeypatch.setattr(dbmod, "embed",
                        lambda text: np.ones(dbmod.EMBED_DIM, dtype=np.float32).tobytes())
    conn = dbmod.init_db(store.db_path)
    conn.execute("INSERT INTO users(handle) VALUES ('ana.test')")
    conn.commit()
    conn.close()

    assert store.add_user_memory({"kind": "fact", "handle": "@Ana.Test",
                                  "text": "vive en Rosario"}) == []
    # dedup semántico: el mismo embedding (mock) → equivalente → rechazado
    assert store.add_user_memory({"kind": "fact", "handle": "ana.test",
                                  "text": "vive en Rosario"}) != []
    assert store.add_user_memory({"kind": "lesson",
                                  "text": "respuestas cortas funcionan"}) == []
    # validaciones
    assert store.add_user_memory({"kind": "fact", "text": "sin handle"}) != []
    assert store.add_user_memory({"kind": "fact", "handle": "nadie.test",
                                  "text": "x"}) != []       # usuario sin perfil
    assert store.add_user_memory({"kind": "interaction", "text": "x"}) != []
    assert store.add_user_memory({"kind": "lesson", "text": "  "}) != []
    assert store.add_user_memory({"kind": "lesson", "text": "y",
                                  "scope": "banana"}) != []  # scope inválido

    conn = dbmod.init_db(store.db_path)
    assert conn.execute("SELECT fact_text FROM user_facts").fetchall()[0][0] == "vive en Rosario"
    assert conn.execute("SELECT COUNT(*) FROM user_facts_vec").fetchone()[0] == 1
    assert conn.execute("SELECT scope FROM lessons").fetchone()[0] == "community"
    conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_ui_ve_los_plugins_de_la_instancia(store):
    """El plugin recién puesto tiene que aparecer en el catálogo de la UI: antes
    solo se cargaban en el motor y desde el panel eran invisibles."""
    d = store.base / "connectors"
    d.mkdir(exist_ok=True)
    (d / "lemmy.py").write_text(
        'CONNECTOR = {"id": "lemmy", "label": "Lemmy", "color": "#0b8", "initial": "L",\n'
        '             "placeholder": "c@i", "tool": "get_latest_media", "help": "h"}\n'
        'def fetch(source, limit=15):\n    return []\n', encoding="utf-8")
    cat = {c["id"]: c for c in store.read_all()["connectors"]}
    assert cat["lemmy"]["label"] == "Lemmy" and cat["lemmy"]["core"] is False
    # y al borrar el archivo, el conector desaparece del catálogo (recarga)
    (d / "lemmy.py").unlink()
    assert "lemmy" not in {c["id"] for c in store.read_all()["connectors"]}


# ─── Log del bot: sale por la consola Y al archivo ──────────────────────────
def test_bomba_de_log_escribe_en_los_dos_lados(tmp_path, capsys):
    """Antes el log del bot solo iba a bot.log: quien lanzaba el panel desde una
    terminal no veía nada."""
    class _Proc:
        stdout = iter(["arranque\n", "segunda linea\n"])

    destino = tmp_path / "bot.log"
    cu._bomba_de_log(_Proc(), destino)
    assert destino.read_text(encoding="utf-8") == "arranque\nsegunda linea\n"
    assert "arranque" in capsys.readouterr().out          # y también por consola


def test_bomba_de_log_no_propaga_errores(tmp_path):
    class _Proc:
        @property
        def stdout(self):
            raise RuntimeError("pipe roto")
    cu._bomba_de_log(_Proc(), tmp_path / "bot.log")        # no debe lanzar


def test_bot_status_devuelve_la_cola_pedida(tmp_path):
    (tmp_path / "bot.log").write_text("\n".join(f"linea {i}" for i in range(500)),
                                      encoding="utf-8")
    assert len(cu.bot_status(tmp_path)["log_tail"].splitlines()) == 200   # default
    assert len(cu.bot_status(tmp_path, lineas=5)["log_tail"].splitlines()) == 5


# ─── rutinas: interruptor y frecuencia desde la lista (frontmatter) ──────────
@pytest.fixture
def store_rutinas(store, tmp_path):
    (tmp_path / "routines").mkdir()
    (tmp_path / "routines" / "memes.md").write_text(
        "---\ninterval_hours: 4\nenabled: true\n---\nposteá un meme\n", encoding="utf-8")
    (tmp_path / "routines" / "sinflag.md").write_text(
        "---\ninterval_hours: 2\n---\nposteá algo\n", encoding="utf-8")
    return store


def _rutina(tmp_path, name):
    return (tmp_path / "routines" / name).read_text(encoding="utf-8")


def test_routine_meta_apaga_sin_tocar_el_cuerpo(store_rutinas, tmp_path):
    assert store_rutinas.set_routine_meta({"file": "memes.md", "enabled": False}) == []
    texto = _rutina(tmp_path, "memes.md")
    assert "enabled: false" in texto
    assert "posteá un meme" in texto          # la conducta no se toca
    assert "interval_hours: 4" in texto


def test_routine_meta_inserta_el_flag_si_no_estaba(store_rutinas, tmp_path):
    assert store_rutinas.set_routine_meta({"file": "sinflag.md", "enabled": False}) == []
    meta, cuerpo = cu._parse_frontmatter(_rutina(tmp_path, "sinflag.md"))
    assert meta["enabled"] == "false" and cuerpo.strip() == "posteá algo"


def test_routine_meta_frecuencia(store_rutinas, tmp_path):
    assert store_rutinas.set_routine_meta({"file": "memes.md", "interval_hours": 0.5}) == []
    assert "interval_hours: 0.5" in _rutina(tmp_path, "memes.md")
    # los enteros quedan enteros (nada de "6.0" en el archivo)
    store_rutinas.set_routine_meta({"file": "memes.md", "interval_hours": 6})
    assert "interval_hours: 6\n" in _rutina(tmp_path, "memes.md")


@pytest.mark.parametrize("body,fragmento", [
    ({"file": "../../evil.md", "enabled": True}, "inválido"),
    ({"file": "fantasma.md", "enabled": True}, "no existe"),
    ({"file": "memes.md", "interval_hours": "cada rato"}, "numérico"),
    ({"file": "memes.md", "interval_hours": -3}, "negativo"),
    ({"file": "memes.md"}, "nada que cambiar"),
])
def test_routine_meta_invalido(store_rutinas, body, fragmento):
    errs = store_rutinas.set_routine_meta(body)
    assert errs and fragmento in errs[0]


# ─── presentación: status / descartar (el popup del primer arranque) ─────────
def test_hello_status_y_descartar(store, tmp_path, monkeypatch):
    (tmp_path / "posted").mkdir(exist_ok=True)
    out = store.hello_world({"action": "status"})
    assert out["ok"] and out["ya_posteado"] is None
    assert out["errors"]                      # instancia sin configurar: dice qué falta
    assert store.hello_world({"action": "descartar"})["ok"]
    assert store.hello_world({"action": "status"})["ya_posteado"] == "descartado por el admin"


def test_hello_action_invalida(store):
    assert "action inválida" in store.hello_world({"action": "banana"})["errors"][0]


# ─── La playlist vieja de settings no puede ser inmortal ────────────────────
def test_guardar_fuentes_retira_la_playlist_legacy(store, tmp_path):
    """`SPOTIFY_PLAYLIST_ID` se inyecta como fuente ("playlist comunitaria"),
    así que mientras siga en settings reaparece por más que se borre en la UI —
    y no hay campo para editarla. En una instancia clonada eso hace que la copia
    herede la playlist de la original y le escriba temas (rancher con arg,
    2026-08-01)."""
    s = json.loads((tmp_path / "config" / "settings.json").read_text(encoding="utf-8"))
    s["SPOTIFY_PLAYLIST_ID"] = "0dw6jEHMNM9JGBK5LPCWpz"
    (tmp_path / "config" / "settings.json").write_text(
        json.dumps(s, indent="\t"), encoding="utf-8")
    # Sin sources.json todavía: la playlist suelta se ve, que es la migración.
    assert any(e["type"] == "spotify" for e in store._read_sources(s))

    # El admin guarda sus fuentes SIN la playlist heredada
    assert store.write_sources([{"type": "spotify", "name": "la mía",
                                 "sources": ["1sewYURqUwizDmY2CwvoAD"]}]) == []

    final = json.loads((tmp_path / "config" / "settings.json").read_text(encoding="utf-8"))
    assert "SPOTIFY_PLAYLIST_ID" not in final
    ids = [i for e in store._read_sources(final)
           if e["type"] == "spotify" for i in e["sources"]]
    assert ids == ["1sewYURqUwizDmY2CwvoAD"]      # ya no reaparece la ajena


# ─── Fuentes de YouTube: la UI guarda ids, no URLs ──────────────────────────
def test_la_url_de_youtube_se_guarda_como_id(store, tmp_path):
    """El admin pega lo que ve en la barra del browser. Si eso se guarda tal
    cual, el motor se lo manda a la API como id y recibe un 400 (2026-08-01)."""
    assert store.write_sources([{
        "type": "youtube", "name": "la lista",
        "sources": ["https://www.youtube.com/playlist?list=PLL5QXRk91MYY&jct=-SLfRFPeL"],
    }]) == []
    guardado = json.loads((tmp_path / "config" / "sources.json").read_text(encoding="utf-8"))
    assert guardado[0]["sources"] == ["PLL5QXRk91MYY"]


def test_una_fuente_de_youtube_que_no_sirve_se_rechaza():
    errs = cu.validate_sources([{"type": "youtube", "name": "x",
                                 "sources": ["https://vimeo.com/12345"]}])
    assert errs and "YouTube" in errs[0]


@pytest.mark.parametrize("valor", [0, 6, "tres", 2.5, True])
def test_tool_rounds_invalido_falla(valor):
    s = json.loads(json.dumps(_SETTINGS))
    s["TOOL_ROUNDS"] = valor
    assert any("TOOL_ROUNDS" in e for e in cu.validate_settings(s))


def test_tool_rounds_valido_pasa():
    s = json.loads(json.dumps(_SETTINGS))
    s["TOOL_ROUNDS"] = 3
    assert cu.validate_settings(s) == []


# ─── Store de plugins (/plugins) ─────────────────────────────────────────────
def test_plugins_catalog(store):
    cat = store.plugins_catalog()
    ids = [c["id"] for c in cat["connectors"]]
    assert "rss" in ids and "membrilla" in ids
    assert cat["mcp"] == [{"name": "reddit", "enabled": False, "transport": "stdio"}]
    assert cat["membrilla"]["installed"] is False
    assert "BSKY_PASSWORD" in cat["env_keys"]


def test_plugin_toggle_connector_persiste(store):
    assert store.set_plugin_enabled({"kind": "connector", "id": "rss", "enabled": False}) == []
    s = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert s["CONNECTORS"]["rss"]["enabled"] is False
    rss = next(c for c in store.plugins_catalog()["connectors"] if c["id"] == "rss")
    assert rss["enabled"] is False


def test_plugin_toggle_core_rechazado(store):
    errs = store.set_plugin_enabled(
        {"kind": "connector", "id": "membrilla", "enabled": False})
    assert errs and "motor" in errs[0]


def test_plugin_toggle_mcp_persiste(store):
    assert store.set_plugin_enabled({"kind": "mcp", "id": "reddit", "enabled": True}) == []
    s = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert s["MCP"]["reddit"]["enabled"] is True
    assert s["MCP"]["reddit"]["transport"] == "stdio"   # no pisó el resto


def test_plugin_toggle_desconocido(store):
    assert store.set_plugin_enabled({"kind": "mcp", "id": "nope", "enabled": True})
    assert store.set_plugin_enabled({"kind": "banana", "id": "rss", "enabled": True})


def test_membrilla_install_clona_y_configura(store, tmp_path, monkeypatch):
    dest = tmp_path / "ws" / "membrilla"
    monkeypatch.setattr(cu.ConfigStore, "_membrilla_default_dir",
                        staticmethod(lambda: dest))

    def _fake_clone(cmd, **kw):
        assert cmd[:2] == ["git", "clone"] and cmd[-1] == str(dest)
        dest.mkdir(parents=True)

        class R:
            returncode = 0
            stdout = "Cloning...\n"
            stderr = ""
        return R()

    monkeypatch.setattr(cu.subprocess, "run", _fake_clone)
    r = store.install_membrilla()
    assert r["ok"] and r["installed"]
    s = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert s["MEMBRILLA"]["repo"] == str(dest)
    # segunda vez: idempotente, sin volver a clonar
    monkeypatch.setattr(cu.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debía clonar")))
    r2 = store.install_membrilla()
    assert r2["ok"] and "ya está" in r2["output"]


def test_membrilla_install_conserva_commands(store, tmp_path, monkeypatch):
    # Carpeta ya clonada a mano pero sin configurar: instala sin git y sin
    # pisar los commands que ya estaban.
    dest = tmp_path / "ws2" / "membrilla"
    dest.mkdir(parents=True)
    monkeypatch.setattr(cu.ConfigStore, "_membrilla_default_dir",
                        staticmethod(lambda: dest))
    s = json.loads(store.settings_path.read_text(encoding="utf-8"))
    s["MEMBRILLA"] = {"commands": ["python x.py run"]}
    store.settings_path.write_text(json.dumps(s), encoding="utf-8")
    monkeypatch.setattr(cu.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debía clonar")))
    r = store.install_membrilla()
    assert r["ok"]
    s2 = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert s2["MEMBRILLA"] == {"commands": ["python x.py run"], "repo": str(dest)}


def test_api_plugins(server):
    data = _get(server + "/api/plugins")
    assert data["membrilla"]["installed"] is False
    with urllib.request.urlopen(server + "/plugins", timeout=10) as r:
        assert r.status == 200
        assert "Store de plugins" in r.read().decode("utf-8")


def test_api_plugins_install_id_invalido(server):
    code, body = _post(server + "/api/plugins/install", {"id": "banana"})
    assert code == 400 and body["errors"]
