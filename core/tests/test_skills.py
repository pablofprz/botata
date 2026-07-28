"""Tests del workspace de skills (T26): parser, selección por scope, tool use_skill."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import skills as sk
import botata as b
from tools import Scope, ToolContext

_CTX = ToolContext(state={}, conn=None)


def _write_skill(dir: Path, filename: str, text: str) -> None:
    (dir / filename).write_text(text, encoding="utf-8")


@pytest.fixture
def skills_dir(tmp_path):
    _write_skill(tmp_path, "reglas.md", (
        "---\n"
        "name: reglas-comunidad\n"
        "description: Cómo responder sobre las reglas\n"
        "scopes: reply, admin\n"
        "---\n"
        "Respondé amable sobre las reglas."
    ))
    _write_skill(tmp_path, "tono.md", (
        "---\n"
        "name: tono-eventos\n"
        "description: Tono al anunciar eventos\n"
        "scopes: feed_reflection\n"
        "inline: true\n"
        "---\n"
        "Anunciá eventos con entusiasmo moderado."
    ))
    return tmp_path


# ─── Parser ──────────────────────────────────────────────────────────────────
def test_carga_y_defaults(skills_dir):
    loaded = {s.name: s for s in sk.load_skills(skills_dir)}
    assert set(loaded) == {"reglas-comunidad", "tono-eventos"}
    reglas = loaded["reglas-comunidad"]
    assert reglas.scopes == frozenset({"reply", "admin"})
    assert reglas.enabled is True and reglas.inline is False
    assert reglas.body == "Respondé amable sobre las reglas."
    assert loaded["tono-eventos"].inline is True


def test_sin_frontmatter_se_ignora(skills_dir):
    _write_skill(skills_dir, "suelto.md", "un markdown sin frontmatter")
    assert len(sk.load_skills(skills_dir)) == 2


def test_disabled_se_excluye(skills_dir):
    _write_skill(skills_dir, "off.md", "---\nname: apagada\ndescription: x\nenabled: false\n---\ncuerpo")
    assert "apagada" not in {s.name for s in sk.load_skills(skills_dir)}


def test_scopes_invalidos_filtrados_y_default(skills_dir):
    _write_skill(skills_dir, "rara.md", "---\nname: rara\ndescription: x\nscopes: banana\n---\ncuerpo")
    rara = next(s for s in sk.load_skills(skills_dir) if s.name == "rara")
    assert rara.scopes == frozenset({"reply", "feed_reflection", "admin"})  # cae al default


def test_sin_name_se_ignora_y_no_rompe(skills_dir):
    _write_skill(skills_dir, "rota.md", "---\ndescription: sin nombre\n---\ncuerpo")
    assert len(sk.load_skills(skills_dir)) == 2


def test_readme_ignorado(skills_dir):
    _write_skill(skills_dir, "README.md", "---\nname: readme\ndescription: no soy skill\n---\nx")
    assert "readme" not in {s.name for s in sk.load_skills(skills_dir)}


def test_dir_inexistente():
    assert sk.load_skills(Path("no/existe")) == []


# ─── Bloque de prompt ────────────────────────────────────────────────────────
def test_bloque_reply_solo_indice(skills_dir):
    block = sk.skills_prompt_block(skills_dir, Scope.REPLY)
    assert "reglas-comunidad: Cómo responder sobre las reglas" in block  # índice
    assert "use_skill" in block
    assert "Respondé amable" not in block          # cuerpo NO inline
    assert "tono-eventos" not in block             # scope feed, no reply


def test_bloque_feed_inline(skills_dir):
    block = sk.skills_prompt_block(skills_dir, Scope.FEED_REFLECTION)
    assert "Anunciá eventos con entusiasmo" in block   # inline:true → cuerpo
    assert "reglas-comunidad" not in block


def test_bloque_admin_all_inline(skills_dir):
    block = sk.skills_prompt_block(skills_dir, Scope.ADMIN, all_inline=True)
    assert "Respondé amable sobre las reglas." in block
    assert "use_skill" not in block                # nada on-demand


def test_bloque_vacio_sin_skills(tmp_path):
    assert sk.skills_prompt_block(tmp_path, Scope.REPLY) == ""


# ─── Tool use_skill ──────────────────────────────────────────────────────────
def test_tool_use_skill(skills_dir, monkeypatch):
    monkeypatch.setattr(b, "SKILLS_DIR", skills_dir)
    out = b._tool_use_skill({"name": "reglas-comunidad"}, _CTX)
    assert "Respondé amable sobre las reglas." in out.text


def test_un_nombre_inventado_devuelve_el_indice_y_no_un_error_seco(skills_dir, monkeypatch):
    """La fase de tools es de UNA ronda: con "[skill desconocida]" el modelo se
    queda sin nada y improvisa. En producción pidió `use_skill("bostera")` —la
    skill se llama `boca`— y contestó "aunque la skill no ande"."""
    monkeypatch.setattr(b, "SKILLS_DIR", skills_dir)
    txt = b._tool_use_skill({"name": "bostera"}, _CTX).text
    assert "reglas-comunidad" in txt and "nombre EXACTO" in txt


def test_tolera_mayusculas_acentos_y_tipeos(skills_dir, monkeypatch):
    monkeypatch.setattr(b, "SKILLS_DIR", skills_dir)
    for variante in ("Reglas-Comunidad", "reglas-comunidád", "reglas-comunidas"):
        assert "Respondé amable" in b._tool_use_skill({"name": variante}, _CTX).text


def test_tool_use_skill_registrada():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    tool = reg.get("use_skill")
    assert tool is not None and tool.enabled
    assert tool.scopes == frozenset({"reply", "feed_reflection"})


def test_edicion_en_caliente(skills_dir, monkeypatch):
    monkeypatch.setattr(b, "SKILLS_DIR", skills_dir)
    _write_skill(skills_dir, "reglas.md", (
        "---\nname: reglas-comunidad\ndescription: x\n---\nTEXTO NUEVO EDITADO"
    ))
    assert "TEXTO NUEVO EDITADO" in b._tool_use_skill({"name": "reglas-comunidad"}, _CTX).text


# ─── Triggers: sacar al modelo del medio ─────────────────────────────────────
# El camino on-demand obliga al LLM a traducir tema → nombre exacto, y ahí se
# equivoca. El matcheo semántico no lo salva: medido con bge-m3, "bostera"→boca
# da 0.385 y "dinosaurios"→mapache 0.369, así que cualquier umbral que acierte
# uno carga la skill equivocada en el otro. Si el admin declara las palabras, la
# skill entra sola y no hay nada que adivinar.
@pytest.fixture()
def skills_con_trigger(tmp_path):
    (tmp_path / "boca.md").write_text(
        "---\nname: boca\ndescription: cuando le nombran a boca\n"
        "triggers: boke, bostero, bokita\n---\nGritá BOKE y poné un meme.\n",
        encoding="utf-8")
    (tmp_path / "otra.md").write_text(
        "---\nname: otra\ndescription: otra cosa\n---\nCuerpo de otra.\n",
        encoding="utf-8")
    return tmp_path


def test_el_trigger_mete_el_cuerpo_sin_tool(skills_con_trigger):
    block = sk.skills_prompt_block(skills_con_trigger, Scope.REPLY,
                                   texto="contame si yo digo BOKE vos me decís?")
    assert "Gritá BOKE y poné un meme." in block      # cuerpo inline
    assert "- boca:" not in block                     # ya no está en el índice
    assert "- otra: otra cosa" in block               # la otra sigue on-demand


def test_sin_el_trigger_la_skill_queda_on_demand(skills_con_trigger):
    block = sk.skills_prompt_block(skills_con_trigger, Scope.REPLY, texto="hola qué tal")
    assert "Gritá BOKE" not in block and "- boca:" in block


def test_el_trigger_matchea_por_palabra_entera_y_sin_acentos(skills_con_trigger):
    ss = sk.load_skills(skills_con_trigger)
    assert [s.name for s in sk.disparadas(ss, "dale BOKE!!")] == ["boca"]
    assert [s.name for s in sk.disparadas(ss, "soy re bostero, viste")] == ["boca"]
    # no matchea adentro de otra palabra: si no, "bokeron" o "bostereada" activan todo
    assert sk.disparadas(ss, "eso es un bokeron") == []
    assert sk.disparadas(ss, "") == []


def test_una_skill_sin_triggers_no_se_dispara_nunca(skills_con_trigger):
    ss = sk.load_skills(skills_con_trigger)
    otra = next(s for s in ss if s.name == "otra")
    assert otra.triggers == ()
    assert otra not in sk.disparadas(ss, "otra otra otra")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
