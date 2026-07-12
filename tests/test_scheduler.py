"""Tests del scheduler de tareas periódicas (T27)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scheduler import PeriodicTask, apply_tasks_config, run_due


class Cursors:
    def __init__(self, initial=None):
        self.data = dict(initial or {})

    def get(self, name):
        return self.data.get(name)

    def save(self, name):
        self.data[name] = datetime.now(timezone.utc).isoformat()


def _run(tasks, cursors):
    run_due(tasks, get_last=cursors.get, save_last=cursors.save)


def test_interval_cero_corre_siempre():
    calls = []
    t = PeriodicTask("feed", lambda: calls.append(1))
    c = Cursors()
    _run([t], c); _run([t], c)
    assert len(calls) == 2
    assert "task:feed" not in c.data          # interval 0 no graba cursor


def test_interval_respeta_cursor():
    calls = []
    t = PeriodicTask("hb", lambda: calls.append(1), interval_hours=12)
    c = Cursors()
    _run([t], c)                              # sin cursor → corre y graba
    _run([t], c)                              # recién corrido → no toca
    assert len(calls) == 1 and "task:hb" in c.data


def test_interval_vencido_corre():
    calls = []
    t = PeriodicTask("hb", lambda: calls.append(1), interval_hours=12)
    viejo = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    c = Cursors({"task:hb": viejo})
    _run([t], c)
    assert len(calls) == 1


def test_cursor_corrupto_corre_igual():
    calls = []
    t = PeriodicTask("hb", lambda: calls.append(1), interval_hours=12)
    _run([t], Cursors({"task:hb": "no-es-fecha"}))
    assert len(calls) == 1


def test_disabled_no_corre():
    calls = []
    t = PeriodicTask("feed", lambda: calls.append(1), enabled=False)
    _run([t], Cursors())
    assert calls == []


def test_error_no_frena_las_demas():
    calls = []

    def boom():
        raise RuntimeError("explota")

    tasks = [PeriodicTask("rota", boom), PeriodicTask("sana", lambda: calls.append(1))]
    _run(tasks, Cursors())                    # no lanza
    assert calls == [1]


def test_network_error_solo_warning(caplog):
    class FakeNetErr(Exception):
        pass

    def boom():
        raise FakeNetErr("timeout")

    run_due([PeriodicTask("net", boom)], get_last=lambda n: None,
            save_last=lambda n: None, network_errors=(FakeNetErr,))
    assert any("error de red" in r.message for r in caplog.records)


def test_keyboard_interrupt_atraviesa():
    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run([PeriodicTask("x", interrupt)], Cursors())


def test_apply_config_overrides():
    tasks = [PeriodicTask("heartbeat", lambda: None, interval_hours=12, enabled=False),
             PeriodicTask("feed", lambda: None)]
    apply_tasks_config(tasks, {"heartbeat": {"enabled": True, "interval_hours": 6},
                               "desconocida": {"enabled": True}})
    hb = tasks[0]
    assert hb.enabled is True and hb.interval_hours == 6
    assert tasks[1].enabled is True             # sin override, intacta


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
