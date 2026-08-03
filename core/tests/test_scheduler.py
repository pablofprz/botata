"""Tests del scheduler de tareas periódicas (T27)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


def test_cursor_que_explota_no_frena_las_demas():
    """Regresión: _is_due lee la DB vía get_last y corría FUERA del try por
    tarea — un `database is locked` (la config UI comparte la SQLite) escapaba
    run_due y en el loop principal mataba el proceso en silencio."""
    calls = []

    def get_last_roto(name):
        if name == "task:rota":
            raise RuntimeError("database is locked")
        return None

    tasks = [PeriodicTask("rota", lambda: calls.append("no"), interval_hours=1),
             PeriodicTask("sana", lambda: calls.append("si"), interval_hours=1)]
    run_due(tasks, get_last=get_last_roto, save_last=lambda name: None)  # no lanza
    assert calls == ["si"]                    # la rota se saltea, la sana corre


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


# ─── diagnóstico de errores + cursor tras un blip de red ────────────────────
class _NetErr(Exception):
    """Excepción sin mensaje, como las de atproto (str(e) == '')."""


def test_network_error_loguea_el_tipo_aunque_no_haya_mensaje(caplog):
    def boom():
        raise _NetErr()

    run_due([PeriodicTask("net", boom)], get_last=lambda n: None,
            save_last=lambda n: None, network_errors=(_NetErr,))
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "_NetErr" in msg          # el tipo va SIEMPRE: antes decía solo "()"
    assert "error de red ()" not in msg


def test_describe_error_custom_se_usa(caplog):
    def boom():
        raise _NetErr()

    run_due([PeriodicTask("net", boom)], get_last=lambda n: None,
            save_last=lambda n: None, network_errors=(_NetErr,),
            describe_error=lambda e: "HTTP 429 · RateLimitExceeded")
    assert any("HTTP 429" in r.getMessage() for r in caplog.records)


def test_error_de_red_no_consume_el_intervalo():
    # Una tarea horaria que falla por red debe reintentar el próximo ciclo,
    # no saltearse la hora entera.
    cursors = Cursors()

    def boom():
        raise _NetErr()

    run_due([PeriodicTask("heartbeat", boom, interval_hours=1.0)],
            get_last=cursors.get, save_last=cursors.save, network_errors=(_NetErr,))
    assert cursors.data == {}


def test_error_inesperado_si_consume_el_intervalo():
    # Un bug real no debe reintentarse en loop caliente cada iteración.
    cursors = Cursors()

    def boom():
        raise RuntimeError("bug de verdad")

    run_due([PeriodicTask("heartbeat", boom, interval_hours=1.0)],
            get_last=cursors.get, save_last=cursors.save, network_errors=(_NetErr,))
    assert "task:heartbeat" in cursors.data


def test_exito_guarda_el_cursor():
    cursors = Cursors()
    run_due([PeriodicTask("heartbeat", lambda: None, interval_hours=1.0)],
            get_last=cursors.get, save_last=cursors.save, network_errors=(_NetErr,))
    assert "task:heartbeat" in cursors.data


# ─── describe_bsky_error: las excepciones de atproto traen str(e) == '' ─────
def _bsky_mods():
    import os
    os.environ.setdefault("BSKY_PASSWORD", "dummy")
    import botata as b
    from atproto_client.exceptions import InvokeTimeoutError, NetworkError, RequestException
    from atproto_client.models.common import XrpcError
    from atproto_client.request import Response
    return b, InvokeTimeoutError, NetworkError, RequestException, XrpcError, Response


def test_describe_bsky_timeout_muestra_la_causa():
    import httpx
    b, InvokeTimeoutError, *_ = _bsky_mods()
    try:
        try:
            raise httpx.ConnectTimeout("timed out")
        except httpx.TimeoutException as e:
            raise InvokeTimeoutError from e
    except Exception as e:
        out = b.describe_bsky_error(e)
    # Sin .response el detalle vive en la causa httpx — sin esto el log iba vacío.
    assert "InvokeTimeoutError" in out and "ConnectTimeout" in out


def test_describe_bsky_rate_limit_muestra_status_y_reset():
    b, _, _, RequestException, XrpcError, Response = _bsky_mods()
    resp = Response(success=False, status_code=429,
                    content=XrpcError(error="RateLimitExceeded", message="Rate Limit Exceeded"),
                    headers={"ratelimit-reset": "1769368800"})
    out = b.describe_bsky_error(RequestException(resp))
    assert "429" in out and "RateLimitExceeded" in out and "se libera" in out


def test_describe_bsky_error_http_generico():
    b, _, _, RequestException, XrpcError, Response = _bsky_mods()
    resp = Response(success=False, status_code=503,
                    content=XrpcError(error="UpstreamFailure", message="Upstream Failure"),
                    headers={})
    assert "503" in b.describe_bsky_error(RequestException(resp))


def test_describe_bsky_cuerpo_no_json_no_rompe():
    b, _, NetworkError, _, _, Response = _bsky_mods()
    resp = Response(success=False, status_code=502, content=b"<html>bad gateway</html>", headers={})
    assert b.describe_bsky_error(NetworkError(resp)) == "NetworkError HTTP 502"
