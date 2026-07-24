"""Tests de resolución de instancia (instance.py) + init_instance (T28c)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import instance  # noqa: E402
from init_instance import init_instance  # noqa: E402


def test_sin_instancia_corta_con_mensaje(monkeypatch):
    # El repo es motor puro (sin config/settings.json): sin flag ni env var, corta.
    monkeypatch.delenv("BOTATA_INSTANCE", raising=False)
    monkeypatch.setattr(sys, "argv", ["botata"])
    with pytest.raises(SystemExit, match="instancia"):
        instance.instance_dir()


def test_repo_como_instancia_backcompat(monkeypatch, tmp_path):
    # Si la raíz del repo tiene config/settings.json (deploy viejo), sigue sirviendo de default.
    monkeypatch.delenv("BOTATA_INSTANCE", raising=False)
    monkeypatch.setattr(sys, "argv", ["botata"])
    monkeypatch.setattr(instance, "REPO_DIR", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text("{}", encoding="utf-8")
    assert instance.instance_dir() == tmp_path


def test_env_var_resuelve(monkeypatch, tmp_path):
    monkeypatch.setenv("BOTATA_INSTANCE", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["botata"])
    assert instance.instance_dir() == tmp_path.resolve()


def test_flag_cli_gana_sobre_env(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    monkeypatch.setenv("BOTATA_INSTANCE", str(a))
    monkeypatch.setattr(sys, "argv", ["botata", "--instance", str(b)])
    assert instance.instance_dir() == b.resolve()
    monkeypatch.setattr(sys, "argv", ["botata", f"--instance={b}"])
    assert instance.instance_dir() == b.resolve()


def test_directorio_inexistente_corta(monkeypatch, tmp_path):
    monkeypatch.setenv("BOTATA_INSTANCE", str(tmp_path / "nope"))
    monkeypatch.setattr(sys, "argv", ["botata"])
    with pytest.raises(SystemExit):
        instance.instance_dir()


def test_init_instance_crea_esqueleto(tmp_path):
    target = tmp_path / "mi-bot"
    init_instance(target)
    assert (target / "config" / "settings.json").exists()
    assert (target / "context" / "SOUL.md").exists()
    assert (target / ".env.example").exists()
    assert (target / "prompts").is_dir()          # defaults del motor copiados
    assert (target / "moods" / "chill.md").exists()
    assert (target / "skills").is_dir()
    assert (target / "posted").is_dir()
    assert (target / "context" / "feeds").is_dir()
    # el settings neutro es JSON válido y sin identidad
    import json
    s = json.loads((target / "config" / "settings.json").read_text(encoding="utf-8"))
    assert s["BOT_HANDLE"] == "" and s["FEEDS"] == []


def test_init_instance_no_pisa_existente(tmp_path):
    target = tmp_path / "mi-bot"
    init_instance(target)
    with pytest.raises(SystemExit):
        init_instance(target)


def test_resolve_init_target():
    from init_instance import REPO_DIR, resolve_init_target

    # nombre pelado → bots/<nombre> en el workspace (padre del motor)
    assert resolve_init_target("mi-bot") == (REPO_DIR.parent / "bots" / "mi-bot").resolve()
    # un path (con separador o absoluto) se respeta tal cual
    assert resolve_init_target(str(REPO_DIR / "x")) == (REPO_DIR / "x").resolve()


def test_run_init_sin_nombre_corta():
    from init_instance import run_init

    with pytest.raises(SystemExit, match="--init"):
        run_init(["botata", "--init"])
    with pytest.raises(SystemExit, match="nombre"):
        run_init(["botata", "--init", "--no-browser"])
