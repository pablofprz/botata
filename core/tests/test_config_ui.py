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


def test_user_groups_validos_pasan():
    s = json.loads(json.dumps(_SETTINGS))
    s["USER_GROUPS"] = {"music_users": ["fulano.bsky.social", "feed:f1"]}
    s["TOOLS"]["web_search"]["groups"] = ["music_users"]
    assert cu.validate_settings(s) == []


def test_news_valida():
    assert cu.validate_news([{"url": "https://x/rss", "title": "X"}]) == []
    errs = cu.validate_news([{"url": "ftp://no", "title": ""}])
    assert len(errs) == 2


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


def test_content_sources_valida():
    assert cu.validate_content_sources([{"source": "cuenta", "category": "futbol"}]) == []
    errs = cu.validate_content_sources([{"source": "", "category": ""}])
    assert len(errs) == 2


def test_content_sources_roundtrip(store):
    assert store.write_content_sources(
        [{"source": "futbol_memes", "category": "futbol", "enabled": True}]) == []
    assert store.read_all()["content_sources"][0]["source"] == "futbol_memes"


def test_content_sources_invalido_no_escribe(store):
    store.write_content_sources([{"source": "ok", "category": "futbol"}])
    assert store.write_content_sources([{"source": "sin_tema"}])          # error
    assert store.read_all()["content_sources"][0]["source"] == "ok"       # intacto


# ─── Membrilla: lanzador del scraper (settings.MEMBRILLA) ────────────────────
def test_membrilla_sin_config_da_instrucciones(store):
    out = store.run_action("membrilla_scrape")
    assert out["ok"] is False
    assert any("MEMBRILLA sin configurar" in e for e in out["errors"])


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
    ({"manual": {"schedule": {"lun": "upbeat"}}}, "día(s) inválido(s)"),
])
def test_validate_moods_invalido(moods, fragmento):
    s = json.loads(json.dumps(_SETTINGS)); s["MOODS"] = moods
    assert any(fragmento in e for e in cu.validate_settings(s))


def test_validate_moods_y_prefs_validos():
    s = json.loads(json.dumps(_SETTINGS))
    s["MOODS"] = {"enabled": True, "mode": "auto", "susceptibility": 0.5,
                  "hysteresis_hours": 2, "manual": {"fixed": "", "schedule": {"mon": "upbeat"}}}
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
