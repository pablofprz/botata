"""Tests del ciclo de vida del bridge de WhatsApp (wa_bridge.py).

Nada acá lanza procesos ni toca la red: Popen, `go build` y el /status se
mockean. Lo que se verifica es el contrato — cuándo se lanza, con qué flags,
y que los errores salgan legibles en vez de tracebacks.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import wa_bridge as wb  # noqa: E402


def test_ensure_respeta_un_bridge_vivo(monkeypatch, tmp_path):
    """Un bridge que ya responde no se toca: puede ser uno lanzado a mano o
    compartido con otro bot del mismo número."""
    monkeypatch.setattr(wb, "bridge_status", lambda url, timeout=5: {"connected": True})
    lanzados = []
    monkeypatch.setattr(wb, "start_bridge", lambda *a: lanzados.append(a) or None)
    st, err = wb.ensure_bridge("http://127.0.0.1:8899", tmp_path, ["a@g.us"])
    assert st == {"connected": True} and err is None
    assert not lanzados


def test_sin_binario_y_sin_go_error_legible(monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "binary_path", lambda: tmp_path / "no-existe.exe")
    monkeypatch.setattr(wb.shutil, "which", lambda _: None)
    err = wb.build_binary()
    assert err and "Go" in err and "go build" in err


def test_binario_presente_no_compila(monkeypatch, tmp_path):
    exe = tmp_path / "whatsapp-bridge.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(wb, "binary_path", lambda: exe)
    assert wb.build_binary() is None


def test_start_arma_los_flags_y_guarda_el_pid(monkeypatch, tmp_path):
    exe = tmp_path / "whatsapp-bridge.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(wb, "binary_path", lambda: exe)
    visto = {}

    class ProcFalso:
        pid = 4242

    def popen_falso(args, **kw):
        visto["args"], visto["kw"] = args, kw
        return ProcFalso()

    monkeypatch.setattr(wb.subprocess, "Popen", popen_falso)
    data = tmp_path / "whatsapp"
    err = wb.start_bridge("http://127.0.0.1:9001", data, ["a@g.us", "b@g.us"])
    assert err is None
    a = visto["args"]
    assert a[0] == str(exe)
    assert a[a.index("-addr") + 1] == "127.0.0.1:9001"       # el puerto sale de la URL
    assert a[a.index("-chats") + 1] == "a@g.us,b@g.us"
    assert (data / "bridge.pid").read_text() == "4242"
    assert (data / "bridge.log").exists()


def test_start_sin_chats_es_modo_vinculacion(monkeypatch, tmp_path):
    """La primera vez no hay JIDs (se eligen en la UI con el bridge arriba):
    tiene que arrancar igual, sin -chats."""
    exe = tmp_path / "whatsapp-bridge.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(wb, "binary_path", lambda: exe)

    class ProcFalso:
        pid = 1

    visto = {}
    monkeypatch.setattr(wb.subprocess, "Popen",
                        lambda args, **kw: visto.update(args=args) or ProcFalso())
    assert wb.start_bridge("http://127.0.0.1:8899", tmp_path / "wa", []) is None
    assert "-chats" not in visto["args"]


def test_stop_solo_mata_lo_propio(monkeypatch, tmp_path):
    """Sin pid file no hay nada nuestro que matar, aunque haya un bridge vivo."""
    matados = []
    monkeypatch.setattr(wb, "_pid_vivo", lambda pid: True)
    monkeypatch.setattr(wb.subprocess, "run", lambda *a, **k: matados.append(a))
    monkeypatch.setattr(wb.os, "kill", lambda *a: matados.append(a))
    wb.stop_bridge(tmp_path)                 # sin pid file → no-op
    assert not matados
    (tmp_path / "bridge.pid").write_text("777")
    wb.stop_bridge(tmp_path)
    assert matados                           # ahora sí
    assert not (tmp_path / "bridge.pid").exists()


def test_ensure_lanza_y_espera(monkeypatch, tmp_path):
    """Bridge caído → se lanza y se espera a que el /status conteste."""
    intentos = iter([None, None, {"connected": False}])
    monkeypatch.setattr(wb, "bridge_status", lambda url, timeout=5: next(intentos))
    monkeypatch.setattr(wb, "start_bridge", lambda *a: None)
    monkeypatch.setattr(wb.time, "sleep", lambda s: None)
    st, err = wb.ensure_bridge("http://127.0.0.1:8899", tmp_path, [], wait=5)
    assert err is None and st == {"connected": False}


def test_ensure_reporta_el_error_de_lanzamiento(monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "bridge_status", lambda url, timeout=5: None)
    monkeypatch.setattr(wb, "start_bridge", lambda *a: "no hay Go")
    st, err = wb.ensure_bridge("http://x:1", tmp_path, [])
    assert st is None and err == "no hay Go"
