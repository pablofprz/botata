"""Tests del pase heartbeat (T27): decide sobre eventos, postea o calla."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import db as d
import botata as b

_AR = timezone(timedelta(hours=-3))


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "hb_test.db")
    c.execute("INSERT OR IGNORE INTO users(handle) VALUES ('ppolci.com')")
    c.commit()
    return c


@pytest.fixture(autouse=True)
def _hb_path(tmp_path, monkeypatch):
    """Aísla el override runtime Y el default versionado de los archivos reales."""
    monkeypatch.setattr(b, "HEARTBEAT_OVERRIDE_PATH", tmp_path / "heartbeat_override.md")
    monkeypatch.setattr(b, "HEARTBEAT_CHECKLIST_PATH", tmp_path / "heartbeat_checklist.md")


class FakeBsky:
    def __init__(self):
        self.posts = []
        self.images = []

    def post(self, text, image_path=None):
        self.posts.append(text)
        self.images.append(image_path)
        return f"at://fake/{len(self.posts)}"


class FakeLLM:
    """Reemplaza RoleLLM: devuelve una decisión fija y cuenta llamadas."""

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def complete(self, system, user, model_cls=None):
        self.calls += 1
        self.last_system = system
        return self.decision


def _hoy_evento(conn, title="Cumple de Polci", handle="ppolci.com"):
    hoy = datetime.now(_AR).strftime("%Y-%m-%d")
    return d.create_event(conn, title=title, event_at=f"{hoy}T20:00",
                          handle=handle, kind="birthday", source="test")


def _run(conn, bsky, llm, monkeypatch):
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_heartbeat_pass(bsky, router=None, conn=conn)


def test_sin_eventos_no_llama_llm(conn, monkeypatch):
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=True, text="hola"))
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 0 and bsky.posts == []


def test_con_evento_postea(conn, monkeypatch):
    _hoy_evento(conn)
    decision = b.FeedDecision(should_post=True, reason="cumple", text="¡Feliz cumple @ppolci.com!")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1
    assert bsky.posts == ["¡Feliz cumple @ppolci.com!"]
    assert "Cumple de Polci" in llm.last_system          # el evento llegó al prompt
    assert b.recent_bot_posts(conn) == ["¡Feliz cumple @ppolci.com!"]  # registrado


def test_should_post_false_no_postea(conn, monkeypatch):
    _hoy_evento(conn)
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="nada nuevo"))
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1 and bsky.posts == []


def test_dedup_contra_bot_posts(conn, monkeypatch):
    _hoy_evento(conn)
    b.log_bot_post(conn, uri="at://viejo", in_reply_to=None, reply_to_handle=None,
                   text="¡Feliz cumple @ppolci.com!")
    decision = b.FeedDecision(should_post=True, text="¡FELIZ CUMPLE  @ppolci.com!")  # ≈ igual
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []                              # normalizado → duplicado → skip


def test_texto_vacio_no_postea(conn, monkeypatch):
    _hoy_evento(conn)
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=True, text="  "))
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []


def test_instrucciones_sin_eventos_llama_llm(conn, monkeypatch):
    """Con instrucciones vigentes, el pase corre aunque no haya eventos."""
    b.HEARTBEAT_OVERRIDE_PATH.write_text(
        "suplicale a un usuario random que te dé dulce de batata\n", encoding="utf-8")
    decision = b.FeedDecision(should_post=True, reason="batata", text="alguien tiene batata? 🙏")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1
    assert "INSTRUCCIONES VIGENTES" in llm.last_system
    assert "dulce de batata" in llm.last_system
    assert bsky.posts == ["alguien tiene batata? 🙏"]


def test_default_versionado_corre_sin_override(conn, monkeypatch):
    """El heartbeat inicial (prompts/heartbeat_checklist.md) es el mecanismo principal:
    corre sin que el admin haya posteado nada."""
    b.HEARTBEAT_CHECKLIST_PATH.write_text("ronda de base: mirá el calendario\n", encoding="utf-8")
    decision = b.FeedDecision(should_post=False, reason="nada nuevo")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1
    assert "INSTRUCCIONES POR DEFECTO" in llm.last_system
    assert "ronda de base" in llm.last_system
    assert "INSTRUCCIONES VIGENTES del admin para este pase" not in llm.last_system


def test_override_del_admin_pisa_al_default(conn, monkeypatch):
    """El post del admin (set_heartbeat) es auxiliar: pisa temporalmente al
    default, que no se inyecta mientras el override exista."""
    b.HEARTBEAT_CHECKLIST_PATH.write_text("ronda de base: mirá el calendario\n", encoding="utf-8")
    b.HEARTBEAT_OVERRIDE_PATH.write_text("pedí dulce de batata\n", encoding="utf-8")
    decision = b.FeedDecision(should_post=True, reason="batata", text="batata? 🙏")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert "INSTRUCCIONES VIGENTES" in llm.last_system
    assert "dulce de batata" in llm.last_system
    assert "ronda de base" not in llm.last_system


def test_override_borrado_vuelve_al_default(conn, monkeypatch):
    """Borrar el override (set_heartbeat instructions='') restaura el default."""
    b.HEARTBEAT_CHECKLIST_PATH.write_text("ronda de base: mirá el calendario\n", encoding="utf-8")
    b.HEARTBEAT_OVERRIDE_PATH.write_text("", encoding="utf-8")   # borrado = archivo vacío
    decision = b.FeedDecision(should_post=False, reason="nada")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1
    assert "INSTRUCCIONES POR DEFECTO" in llm.last_system


def test_sin_eventos_ni_instrucciones_no_llama(conn, monkeypatch):
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=True, text="x"))
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 0 and bsky.posts == []


def test_heartbeat_adjunta_imagen(conn, monkeypatch):
    """Con catálogo disponible e image_query en la decisión, el post sale con imagen."""
    b.HEARTBEAT_OVERRIDE_PATH.write_text("subí imágenes de gatos\n", encoding="utf-8")
    monkeypatch.setattr(b, "TASKS_CONFIG", {"heartbeat": {"autonomous_images": True}})
    monkeypatch.setattr(b.dbmod, "get_image_catalog_stats", lambda c: {"total": 3})
    monkeypatch.setattr(b, "resolve_catalog_image",
                        lambda c, q, **k: "scrape/pictures/manual/gato.jpg")
    decision = b.FeedDecision(should_post=True, reason="gato", text="miren este gato",
                              image_query="gato sorprendido")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert "catálogo de imágenes" in llm.last_system      # se le ofreció el catálogo
    assert bsky.posts == ["miren este gato"]
    assert bsky.images == ["scrape/pictures/manual/gato.jpg"]


def test_heartbeat_toggle_imagenes_apagado(conn, monkeypatch):
    """TASKS.heartbeat.autonomous_images=false → ni ofrece catálogo ni adjunta."""
    b.HEARTBEAT_OVERRIDE_PATH.write_text("subí imágenes de gatos\n", encoding="utf-8")
    monkeypatch.setattr(b, "TASKS_CONFIG", {"heartbeat": {"autonomous_images": False}})
    monkeypatch.setattr(b.dbmod, "get_image_catalog_stats", lambda c: {"total": 3})
    spy = {"called": False}
    monkeypatch.setattr(b, "resolve_catalog_image",
                        lambda c, q, **k: spy.__setitem__("called", True) or "x.jpg")
    decision = b.FeedDecision(should_post=True, reason="gato", text="miren este gato",
                              image_query="gato")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert "catálogo de imágenes" not in llm.last_system
    assert bsky.images == [None]
    assert spy["called"] is False


def test_heartbeat_catalogo_vacio_no_ofrece(conn, monkeypatch):
    """Sin imágenes cargadas, el bloque de catálogo no entra al prompt."""
    b.HEARTBEAT_OVERRIDE_PATH.write_text("subí imágenes de gatos\n", encoding="utf-8")
    monkeypatch.setattr(b, "TASKS_CONFIG", {"heartbeat": {"autonomous_images": True}})
    monkeypatch.setattr(b.dbmod, "get_image_catalog_stats", lambda c: {"total": 0})
    decision = b.FeedDecision(should_post=True, reason="x", text="hola", image_query="gato")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert "catálogo de imágenes" not in llm.last_system
    assert bsky.images == [None]


def test_error_del_llm_es_graceful(conn, monkeypatch):
    _hoy_evento(conn)

    class BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("api caída")

    bsky = FakeBsky()
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: BoomLLM())
    b.run_heartbeat_pass(bsky, router=None, conn=conn)   # no lanza
    assert bsky.posts == []


# ─── Fase de tools del heartbeat ─────────────────────────────────────────────
class FakeToolLLM(FakeLLM):
    """FakeLLM que además pide UNA tool call en la fase de tools."""

    def __init__(self, decision, tool_name, tool_args):
        super().__init__(decision)
        self._call = type("C", (), {"function": type("F", (), {
            "name": tool_name, "arguments": __import__("json").dumps(tool_args)})()})()

    def call_with_tools(self, system, user, tools):
        self.tool_system = system
        return "", [self._call]


def _music_registry():
    from tools import ToolRegistry, ToolResult, Scope
    reg = ToolRegistry()
    reg.register(
        "search_music", "busca música",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        lambda args, ctx: ToolResult(text="- La H no murió — Hermética (https://spoti.fy/x)"),
        {Scope.FEED_REFLECTION},
    )
    return reg


def test_instruccion_con_tool_trae_resultado_real(conn, monkeypatch):
    """'posteá un tema de Hermética' → la fase de tools llama search_music y el
    resultado (tema + link real) entra al contexto de la decisión."""
    b.HEARTBEAT_OVERRIDE_PATH.write_text("posteá un tema de Hermética\n", encoding="utf-8")
    decision = b.FeedDecision(should_post=True, reason="orden musical",
                              text="escuchen La H no murió https://spoti.fy/x")
    bsky = FakeBsky()
    llm = FakeToolLLM(decision, "search_music", {"query": "Hermética"})
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_heartbeat_pass(bsky, router=None, conn=conn, registry=_music_registry())
    assert "La H no murió" in llm.last_system          # el resultado llegó a la decisión
    assert "Resultado de search_music" in llm.last_system
    assert bsky.posts == ["escuchen La H no murió https://spoti.fy/x"]


def test_sin_registry_no_hay_fase_de_tools(conn, monkeypatch):
    """Back-compat: sin registry el pase funciona igual (tests/pases viejos)."""
    b.HEARTBEAT_OVERRIDE_PATH.write_text("posteá algo\n", encoding="utf-8")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=True, text="algo"))
    _run(conn, bsky, llm, monkeypatch)                 # registry=None
    assert bsky.posts == ["algo"]


def test_fase_de_tools_rota_no_frena_el_pase(conn, monkeypatch):
    """Una tool que explota se loguea y el pase sigue sin su resultado."""
    from tools import ToolRegistry, Scope
    reg = ToolRegistry()

    def _boom(args, ctx):
        raise RuntimeError("spotify caído")

    reg.register("search_music", "x",
                 {"type": "object", "properties": {}, "required": []},
                 _boom, {Scope.FEED_REFLECTION})
    b.HEARTBEAT_OVERRIDE_PATH.write_text("posteá un tema\n", encoding="utf-8")
    decision = b.FeedDecision(should_post=False, reason="no pude")
    llm = FakeToolLLM(decision, "search_music", {})
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_heartbeat_pass(FakeBsky(), router=None, conn=conn, registry=reg)  # no lanza
    assert llm.calls == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
