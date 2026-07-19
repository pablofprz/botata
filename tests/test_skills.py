"""Tests del workspace de skills (T26): parser, selección por scope, tool use_skill."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    assert "[skill desconocida" in b._tool_use_skill({"name": "nada"}, _CTX).text


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
