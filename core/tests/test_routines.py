"""Tests de rutinas: conducta proactiva en routines/*.md — parser, cadencia por
cursor, pase sin canal (ex-heartbeat: eventos como contexto, filtro anti-bypass,
imágenes, tools) y pase con canal (ex-rooms: destino, registro, dedup por canal)."""
from __future__ import annotations

import os
import sys
from datetime import datetime as _dt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b
import db as d
import routines as r
from channels import DiscordChannel
from test_channels import FakeDiscordHttp


def _routine_md(dirpath, name, channel=None, interval="1", enabled="true",
                body="posteá memes"):
    chan = f"channel: {channel}\n" if channel else ""
    (dirpath / f"{name}.md").write_text(
        f"---\n{chan}interval_hours: {interval}\n"
        f"enabled: {enabled}\n---\n{body}\n", encoding="utf-8")


# ─── parser ──────────────────────────────────────────────────────────────────
def test_load_routines_parsea_y_filtra(tmp_path):
    _routine_md(tmp_path, "memes", channel="111")
    _routine_md(tmp_path, "apagada", enabled="false")
    _routine_md(tmp_path, "solo-actitud", channel="222", interval="0", body="acá serio")
    _routine_md(tmp_path, "general", interval="2", body="mirá el ambiente")
    (tmp_path / "README.md").write_text("doc", encoding="utf-8")
    (tmp_path / "rota.md").write_text("sin frontmatter", encoding="utf-8")
    routines = r.load_routines(tmp_path)
    assert [x.name for x in routines] == ["general", "memes", "solo-actitud"]
    general = routines[0]
    assert general.channel == "" and general.interval_hours == 2.0
    memes = routines[1]
    assert memes.channel == "111" and memes.interval_hours == 1.0
    assert memes.body == "posteá memes"


def test_rutina_sin_cuerpo_se_ignora(tmp_path):
    (tmp_path / "x.md").write_text("---\ninterval_hours: 1\n---\n  \n",
                                   encoding="utf-8")
    assert r.load_routines(tmp_path) == []


def test_interval_invalido_degrada_a_cero(tmp_path):
    _routine_md(tmp_path, "x", interval="banana")
    assert r.load_routines(tmp_path)[0].interval_hours == 0.0


def test_routine_for_uri_solo_con_canal():
    memes = r.Routine(name="memes", body="x", path=Path("x"), channel="111")
    general = r.Routine(name="general", body="x", path=Path("x"))
    routines = [general, memes]
    assert r.routine_for_uri(routines, "111/999") is memes
    assert r.routine_for_uri(routines, "222/999") is None
    assert r.routine_for_uri(routines, "at://did:plc:x/app.bsky.feed.post/1") is None
    assert r.routine_for_uri(routines, "1112/9") is None  # prefijo con "/", no substring


# ─── fixtures del pase ───────────────────────────────────────────────────────
@pytest.fixture
def conn(tmp_path):
    c = d.init_db(tmp_path / "routines.db")
    yield c
    c.close()


@pytest.fixture
def routines_dir(tmp_path, monkeypatch):
    rd = tmp_path / "routines"
    rd.mkdir()
    monkeypatch.setattr(b, "ROUTINES_DIR", rd)
    return rd


class FakeBsky:
    def __init__(self, channel_msgs=None):
        self.posts = []   # (text, target)
        self.images = []
        self.channel_msgs = channel_msgs or []

    def post(self, text, limit=295, media_path=None, target=None):
        self.posts.append((text, target))
        self.images.append(media_path)
        return f"{target or 'main'}/{len(self.posts)}"

    def get_feed_posts(self, source_type, identifier, since, limit=50):
        return self.channel_msgs


class FakeLLM:
    def __init__(self, decision):
        self.decision, self.calls = decision, 0

    def complete(self, system, user, model_cls=None):
        self.calls += 1
        self.last_system = system
        return self.decision


def _run(conn, bsky, llm, monkeypatch):
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_routines_pass(bsky, router=None, conn=conn)


# ─── pase con canal (ex-rooms) ───────────────────────────────────────────────
def test_pass_postea_al_canal_de_la_rutina(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "memes", channel="111",
                body="posteá memes. Actitud: shitposting")
    llm = FakeLLM(b.FeedDecision(should_post=True, reason="memes", text="jeje meme"))
    bsky = FakeBsky(channel_msgs=[{"handle": "ana", "text": "jajaja este canal"}])
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == [("jeje meme", "111")]           # destino = canal de la rutina
    assert "shitposting" in llm.last_system               # el cuerpo entró al prompt
    assert "jajaja este canal" in llm.last_system         # contexto del canal


def test_pass_sin_canal_postea_al_feed_principal(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "general", body="mirá el ambiente y decí algo")
    llm = FakeLLM(b.FeedDecision(should_post=True, reason="ok", text="hola mundo"))
    bsky = FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == [("hola mundo", None)]           # target None = feed principal
    assert "mirá el ambiente" in llm.last_system
    assert b.recent_bot_posts(conn) == ["hola mundo"]     # registrado


def test_pass_respeta_cadencia_por_cursor(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "memes", channel="111")
    llm = FakeLLM(b.FeedDecision(should_post=True, reason="x", text="meme"))
    bsky = FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    _run(conn, bsky, llm, monkeypatch)   # dentro del intervalo → no corre
    assert llm.calls == 1 and len(bsky.posts) == 1


def test_interval_cero_no_postea(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "seria", channel="222", interval="0", body="acá serio")
    llm = FakeLLM(b.FeedDecision(should_post=True, reason="x", text="hola"))
    bsky = FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 0 and bsky.posts == []


def test_pass_declinado_consume_el_intervalo(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "memes", channel="111")
    llm = FakeLLM(b.FeedDecision(should_post=False, reason="nada nuevo"))
    bsky = FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1 and bsky.posts == []


def test_llm_caido_consume_el_intervalo(conn, routines_dir, monkeypatch):
    """Un pase fallido espera su intervalo — sin hot-loop de LLM cada ciclo."""
    _routine_md(routines_dir, "memes", channel="111")

    class BoomLLM:
        calls = 0

        def complete(self, *a, **k):
            BoomLLM.calls += 1
            raise RuntimeError("boom")

    monkeypatch.setattr(b, "RoleLLM", lambda router, role: BoomLLM())
    bsky = FakeBsky()
    b.run_routines_pass(bsky, router=None, conn=conn)   # no lanza
    b.run_routines_pass(bsky, router=None, conn=conn)
    assert BoomLLM.calls == 1 and bsky.posts == []


def test_dedup_contra_posts_del_mismo_canal(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "memes", channel="111")
    b.log_bot_post(conn, uri="111/9", in_reply_to=None, reply_to_handle=None,
                   text="jeje meme")
    llm = FakeLLM(b.FeedDecision(should_post=True, reason="x", text="JEJE  MEME"))
    bsky = FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []  # normalizado → duplicado del canal → skip


def test_dedup_global_para_rutina_sin_canal(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "general", body="decí algo")
    b.log_bot_post(conn, uri="at://viejo", in_reply_to=None, reply_to_handle=None,
                   text="hola mundo")
    llm = FakeLLM(b.FeedDecision(should_post=True, reason="x", text="HOLA  MUNDO"))
    bsky = FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []


def test_recent_bot_posts_filtra_por_canal(conn):
    b.log_bot_post(conn, uri="111/1", in_reply_to=None, reply_to_handle=None, text="en memes")
    b.log_bot_post(conn, uri="222/1", in_reply_to=None, reply_to_handle=None, text="en politica")
    assert b.recent_bot_posts(conn, uri_prefix="111/") == ["en memes"]
    assert set(b.recent_bot_posts(conn)) == {"en memes", "en politica"}


# ─── imágenes del catálogo ───────────────────────────────────────────────────
def test_rutina_adjunta_imagen(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "gatos", body="subí imágenes de gatos")
    monkeypatch.setattr(b.dbmod, "get_image_catalog_stats", lambda c: {"total": 3})
    monkeypatch.setattr(b, "resolve_catalog_image",
                        lambda c, q, **k: "scrape/pictures/manual/gato.jpg")
    decision = b.FeedDecision(should_post=True, reason="gato", text="miren este gato",
                              image_query="gato sorprendido")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert "catálogo de imágenes" in llm.last_system
    assert bsky.posts == [("miren este gato", None)]
    assert bsky.images == ["scrape/pictures/manual/gato.jpg"]


def test_catalogo_vacio_no_ofrece(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "gatos", body="subí imágenes de gatos")
    monkeypatch.setattr(b.dbmod, "get_image_catalog_stats", lambda c: {"total": 0})
    decision = b.FeedDecision(should_post=True, reason="x", text="hola", image_query="gato")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert "catálogo de imágenes" not in llm.last_system
    assert bsky.images == [None]


# ─── fase de tools ───────────────────────────────────────────────────────────
class FakeToolLLM(FakeLLM):
    """FakeLLM que además pide UNA tool call en la fase de tools."""

    def __init__(self, decision, tool_name, tool_args):
        super().__init__(decision)
        self._call = type("C", (), {"function": type("F", (), {
            "name": tool_name, "arguments": __import__("json").dumps(tool_args)})()})()

    def call_with_tools(self, system, user, tools):
        self.tool_system = system
        return "", [self._call]


def test_rutina_con_tool_trae_resultado_real(conn, routines_dir, monkeypatch):
    """'posteá un tema de Hermética' → la fase de tools llama search_music y el
    resultado (tema + link real) entra al contexto de la decisión."""
    from tools import ToolRegistry, ToolResult, Scope
    reg = ToolRegistry()
    reg.register(
        "search_music", "busca música",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        lambda args, ctx: ToolResult(text="- La H no murió — Hermética (https://spoti.fy/x)"),
        {Scope.FEED_REFLECTION},
    )
    _routine_md(routines_dir, "musica", body="posteá un tema de Hermética")
    decision = b.FeedDecision(should_post=True, reason="orden musical",
                              text="escuchen La H no murió https://spoti.fy/x")
    bsky = FakeBsky()
    llm = FakeToolLLM(decision, "search_music", {"query": "Hermética"})
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_routines_pass(bsky, router=None, conn=conn, registry=reg)
    assert "La H no murió" in llm.last_system
    assert "Resultado de search_music" in llm.last_system
    assert bsky.posts == [("escuchen La H no murió https://spoti.fy/x", None)]


def test_fase_de_tools_rota_no_frena_el_pase(conn, routines_dir, monkeypatch):
    """Una tool que explota se loguea y el pase sigue sin su resultado.

    Aislado por tool: el fallo no corta la ronda ni el pase (antes se comía la
    fase entera con un try/except alrededor de todo)."""
    from tools import ToolRegistry, Scope
    reg = ToolRegistry()

    def _boom(args, ctx):
        raise RuntimeError("spotify caído")

    reg.register("search_music", "x",
                 {"type": "object", "properties": {}, "required": []},
                 _boom, {Scope.FEED_REFLECTION})
    _routine_md(routines_dir, "musica", body="posteá un tema")
    decision = b.FeedDecision(should_post=False, reason="no pude")
    llm = FakeToolLLM(decision, "search_music", {})
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_routines_pass(FakeBsky(), router=None, conn=conn, registry=reg)  # no lanza
    assert llm.calls == 1                     # la decisión igual se pidió


# ─── eventos como contexto + filtro anti-bypass ──────────────────────────────
# Los anuncios son de la tarea `calendar`; a la rutina los eventos le llegan
# solo como contexto, y un evento con hora ya vencida SALE del contexto (bypass
# visto en vivo en mafiazeth). Reloj congelado vía db.local_now para determinismo.
def _congelar(monkeypatch, dt):
    monkeypatch.setattr(d, "local_now", lambda: dt)


def test_evento_de_hoy_entra_al_contexto(conn, routines_dir, monkeypatch):
    _congelar(monkeypatch, _dt(2026, 3, 10, 10, 0))
    conn.execute("INSERT OR IGNORE INTO users(handle) VALUES ('ppolci.com')")
    conn.commit()
    d.create_event(conn, title="Cumple de Polci", event_at="2026-03-10",
                   handle="ppolci.com", kind="birthday", source="ui")
    _routine_md(routines_dir, "general", body="mirá el calendario")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="x"))
    _run(conn, bsky, llm, monkeypatch)
    assert "Cumple de Polci" in llm.last_system


def test_evento_vencido_sale_del_contexto(conn, routines_dir, monkeypatch):
    _congelar(monkeypatch, _dt(2026, 3, 10, 10, 0))
    d.create_event(conn, title="ronda matutina", event_at="2026-03-10T08:00",
                   handle=None, kind="reminder", source="tool:@user1.test")
    _routine_md(routines_dir, "general", body="mirá el calendario")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="x"))
    _run(conn, bsky, llm, monkeypatch)
    assert "ronda matutina" not in llm.last_system


def test_evento_futuro_de_hoy_queda(conn, routines_dir, monkeypatch):
    _congelar(monkeypatch, _dt(2026, 3, 10, 10, 0))
    d.create_event(conn, title="juntada nocturna", event_at="2026-03-10T18:00",
                   handle=None, kind="meetup", source="ui")
    _routine_md(routines_dir, "general", body="mirá el calendario")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="x"))
    _run(conn, bsky, llm, monkeypatch)
    assert "juntada nocturna" in llm.last_system
    assert "[HOY, faltan ~8 h]" in llm.last_system


def test_recurrente_se_normaliza_a_hoy(conn, routines_dir, monkeypatch):
    """El event_at crudo de un recurrente es su PRIMERA ocurrencia (histórica);
    al contexto va la ocurrencia de HOY — si no, el timing daría [YA PASÓ]."""
    _congelar(monkeypatch, _dt(2026, 3, 10, 10, 0))
    d.create_event(conn, title="ronda diaria", event_at="2020-01-01T20:00",
                   handle=None, kind="community", source="ui", recur="daily")
    _routine_md(routines_dir, "general", body="mirá el calendario")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="x"))
    _run(conn, bsky, llm, monkeypatch)
    assert "2026-03-10T20:00" in llm.last_system
    assert "[YA PASÓ]" not in llm.last_system


def test_recurrente_de_hoy_ya_vencido_sale(conn, routines_dir, monkeypatch):
    _congelar(monkeypatch, _dt(2026, 3, 10, 22, 0))
    d.create_event(conn, title="ronda diaria", event_at="2020-01-01T20:00",
                   handle=None, kind="community", source="ui", recur="daily")
    _routine_md(routines_dir, "general", body="mirá el calendario")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="x"))
    _run(conn, bsky, llm, monkeypatch)
    # la ocurrencia de hoy (20:00) ya pasó → fuera de "hoy"; como próximo puede
    # aparecer la de MAÑANA (contexto legítimo de futuro), nunca la vencida
    assert "2026-03-10T20:00" not in llm.last_system


def test_lecciones_recientes_entran_al_contexto(conn, routines_dir, monkeypatch):
    """Las lecciones destiladas (tarea reflection) alimentan a las rutinas —
    material clave de la rutina reflexiva (ex public_reflection)."""
    conn.execute("INSERT INTO lessons(lesson_text) VALUES ('no repitas chistes viejos')")
    conn.commit()
    _routine_md(routines_dir, "reflexion", body="reflexioná sobre lo que viviste")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="x"))
    _run(conn, bsky, llm, monkeypatch)
    assert "Lecciones que destilaste" in llm.last_system
    assert "no repitas chistes viejos" in llm.last_system


# ─── registro por canal en replies + destino en DiscordChannel.post ──────────
def test_routine_block_reply_mantiene_identidad():
    rt = r.Routine(name="politica", body="acá serio", path=Path("x"), channel="222")
    block = b._routine_block(rt, para_reply=True)
    assert "acá serio" in block
    assert "tu identidad y tu humor" in block.lower()  # el marco anti-reemplazo


def test_routine_block_proactivo_permite_callar():
    rt = r.Routine(name="general", body="mirá el ambiente", path=Path("x"))
    block = b._routine_block(rt, para_reply=False)
    assert "mirá el ambiente" in block
    assert "feed principal" in block


def test_discord_post_con_target():
    class Http(FakeDiscordHttp):
        def request(self, method, path, params=None, json=None, files=None, data=None):
            if method == "POST" and path == "/channels/222/messages":
                self.posted.append({"path": path, "json": json})
                from test_channels import FakeResp
                return FakeResp({"id": "77"})
            return super().request(method, path, params=params, json=json,
                                   files=files, data=data)

    http = Http()
    ch = DiscordChannel("tok", ["111"], http=http)
    assert ch.post("hola politica", target="222") == "222/77"
    assert http.posted[-1]["json"] == {"content": "hola politica"}
    # sin target sigue yendo al principal
    assert ch.post("hola main").startswith("111/")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ─── el bug de "dice que comparte una canción y no pone nada" ────────────────
# Mecanismo real (log de producción, 2026-07-28): la fase de tools era UN solo
# llamado. El modelo lo gastaba en las tools de contexto (summarize_feed,
# get_my_recent_posts) y después ya no tenía cómo traer el tema — pero igual
# posteaba "este martes pide un tema de los redondos" sin link. 7 de 12 pases de
# la rutina `canciones` nunca llamaron get_playlist_track.

class LLMDosRondas(FakeLLM):
    """Pide tools de CONTEXTO en la ronda 1 y la que trae el tema en la 2 —
    exactamente lo que el modelo hacía y no podía completar."""

    def __init__(self, decision, ronda1, ronda2):
        super().__init__(decision)
        self.rondas, self.systems = [ronda1, ronda2], []

    def call_with_tools(self, system, user, tools):
        self.systems.append(system)
        pedido = self.rondas[min(len(self.systems), len(self.rondas)) - 1]
        self.last_system = system
        if not pedido:
            return "", []
        import json as _json
        return "", [type("C", (), {"function": type("F", (), {
            "name": pedido[0], "arguments": _json.dumps(pedido[1])})()})()]


def _registry_musical():
    from tools import ToolRegistry, ToolResult, Scope
    reg = ToolRegistry()
    reg.register("summarize_feed", "clima", {"type": "object", "properties": {}},
                 lambda a, c: ToolResult(text="se habla de los redondos"),
                 {Scope.FEED_REFLECTION})
    reg.register("get_playlist_track", "un tema", {"type": "object", "properties": {}},
                 lambda a, c: ToolResult(
                     text="Tema: Juguetes Perdidos — Redondos\nLink (incluilo en el "
                          "post): https://open.spotify.com/track/abc"),
                 {Scope.FEED_REFLECTION})
    return reg


def test_segunda_ronda_le_deja_traer_la_cancion(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "canciones", body="compartí un tema de la playlist")
    decision = b.FeedDecision(should_post=True, reason="clima musical",
                              text="martes de redondos https://open.spotify.com/track/abc")
    llm = LLMDosRondas(decision, ("summarize_feed", {}), ("get_playlist_track", {}))
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    bsky = FakeBsky()
    b.run_routines_pass(bsky, router=None, conn=conn, registry=_registry_musical())
    assert len(llm.systems) == 2                       # hubo segunda ronda
    assert "Resultado de get_playlist_track" in llm.last_system
    assert "spotify.com/track/abc" in bsky.posts[0][0]


def test_sin_tools_en_la_primera_ronda_no_hay_segunda(conn, routines_dir, monkeypatch):
    """Si no pidió nada, no se paga un llamado extra."""
    _routine_md(routines_dir, "general", body="mirá el ambiente")
    llm = LLMDosRondas(b.FeedDecision(should_post=False, reason="nada"), None, None)
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_routines_pass(FakeBsky(), router=None, conn=conn, registry=_registry_musical())
    assert len(llm.systems) == 1


def test_no_repite_la_misma_tool_con_los_mismos_args(conn, routines_dir, monkeypatch):
    """La ronda 2 no vuelve a gastar la misma llamada (anti-loop)."""
    _routine_md(routines_dir, "canciones", body="compartí un tema")
    llamadas = []
    from tools import ToolRegistry, ToolResult, Scope
    reg = ToolRegistry()
    reg.register("get_playlist_track", "un tema", {"type": "object", "properties": {}},
                 lambda a, c: (llamadas.append(1),
                               ToolResult(text="Tema: X — https://sp.fy/1"))[1],
                 {Scope.FEED_REFLECTION})
    decision = b.FeedDecision(should_post=True, reason="x", text="tema https://sp.fy/1")
    llm = LLMDosRondas(decision, ("get_playlist_track", {}), ("get_playlist_track", {}))
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_routines_pass(FakeBsky(), router=None, conn=conn, registry=reg)
    assert len(llamadas) == 1


def test_rescata_el_link_que_el_post_se_comio(conn, routines_dir, monkeypatch):
    """Trajo el tema pero escribió el post sin el link: se lo pega el código."""
    _routine_md(routines_dir, "canciones", body="compartí un tema")
    decision = b.FeedDecision(should_post=True, reason="x",
                              text="martes de redondos, juguetes perdidos 🎵")
    llm = LLMDosRondas(decision, ("get_playlist_track", {}), None)
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    bsky = FakeBsky()
    b.run_routines_pass(bsky, router=None, conn=conn, registry=_registry_musical())
    posteado = bsky.posts[0][0]
    assert posteado.startswith("martes de redondos")
    assert posteado.endswith("https://open.spotify.com/track/abc")


def test_no_toca_el_post_si_ya_trae_un_link(conn, routines_dir, monkeypatch):
    _routine_md(routines_dir, "canciones", body="compartí un tema")
    texto = "escuchate esto https://open.spotify.com/track/abc"
    decision = b.FeedDecision(should_post=True, reason="x", text=texto)
    llm = LLMDosRondas(decision, ("get_playlist_track", {}), None)
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    bsky = FakeBsky()
    b.run_routines_pass(bsky, router=None, conn=conn, registry=_registry_musical())
    assert bsky.posts[0][0] == texto


def test_con_varios_links_no_adivina(conn, routines_dir, monkeypatch):
    """Con dos links no hay forma de saber cuál prometía: se deja como está."""
    from tools import ToolRegistry, ToolResult, Scope
    reg = ToolRegistry()
    reg.register("get_news", "titulares", {"type": "object", "properties": {}},
                 lambda a, c: ToolResult(text="uno https://a.com/1 · dos https://b.com/2"),
                 {Scope.FEED_REFLECTION})
    _routine_md(routines_dir, "noticias", body="contá una noticia")
    decision = b.FeedDecision(should_post=True, reason="x", text="pasaron cosas hoy")
    llm = LLMDosRondas(decision, ("get_news", {}), None)
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    bsky = FakeBsky()
    b.run_routines_pass(bsky, router=None, conn=conn, registry=reg)
    assert bsky.posts[0][0] == "pasaron cosas hoy"


def test_el_rescate_deja_lugar_para_el_link(conn, routines_dir, monkeypatch):
    """El texto se recorta ANTES de pegar el link: si no, el truncado del canal
    se comía justo el link que estamos rescatando."""
    _routine_md(routines_dir, "canciones", body="compartí un tema")
    decision = b.FeedDecision(should_post=True, reason="x", text="ma " * 120)
    llm = LLMDosRondas(decision, ("get_playlist_track", {}), None)
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    bsky = FakeBsky()
    b.run_routines_pass(bsky, router=None, conn=conn, registry=_registry_musical())
    posteado = bsky.posts[0][0]
    assert len(posteado) <= b._POST_LIMIT
    assert posteado.endswith("https://open.spotify.com/track/abc")
