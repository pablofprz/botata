"""Tests del tope de reintentos por mención (T55).

Una mención que falla queda en `failed`, y `has_replied` deja pasar los `failed`
para que el próximo poll la reintente. Eso está bien cuando el error es puntual y
es un desastre cuando el error es el mundo: con los endpoints caídos, las mismas
dos menciones se reprocesaron cada 4 minutos durante 1h20 sin postear nunca, y
cada vuelta reejecutó la fase de tools (que hoy cuesta plata).

El tope corta eso. Lo que se prueba acá es que corta *de verdad*: que cuenta el
intento aunque el flujo explote a mitad, que no se resetea al reiniciar el bot, y
que no toca ninguna mención sana.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as dbmod  # noqa: E402


class FakeGraph:
    """Grafo que falla siempre, como fallaba el de producción: sin postear."""

    def __init__(self, conn, explota: bool = False):
        self.conn = conn
        self.explota = explota
        self.invocaciones = []

    def invoke(self, state):
        uri = state["mention_uri"]
        self.invocaciones.append(uri)
        if self.explota:
            raise RuntimeError("el endpoint se cayó a mitad del grafo")
        b.update_status(self.conn, uri, "failed")


class FakeChannel:
    """Canal que devuelve las menciones que el poll ve en las notificaciones."""

    def __init__(self, menciones):
        self.menciones = menciones
        self.marcadas = 0

    def get_mentions(self):
        return list(self.menciones)

    def get_mention_by_uri(self, uri):
        return next((m for m in self.menciones if m["uri"] == uri), None)

    def get_thread_info(self, uri, cid):
        return "", uri, cid, None

    def mark_all_read(self):
        self.marcadas += 1


def _mention(uri: str = "u1", author: str = "ana.test") -> dict:
    return {"uri": uri, "cid": uri, "author_handle": author, "text": "@bot hola",
            "thread_context": "", "thread_root_uri": uri, "thread_root_cid": uri}


def _fila(conn, uri: str = "u1"):
    return tuple(conn.execute(
        "SELECT status, attempts FROM replied_posts WHERE uri = ?", (uri,)).fetchone())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = dbmod.init_db(tmp_path / "botata.db")
    monkeypatch.setattr(b, "ADMIN_HANDLE", "admin.test")
    monkeypatch.setattr(b, "ADMIN_HANDLES", frozenset({"admin.test"}))
    monkeypatch.setattr(b, "MENTION_MAX_RETRIES", 3)
    yield c
    c.close()


# ─── el contador ───────────────────────────────────────────────────────────

def test_el_primer_intento_no_cuenta_como_reintento(conn):
    graph = FakeGraph(conn)
    b.process_mention(graph, conn, _mention(), "open")
    assert _fila(conn) == ("failed", 0)      # falló una vez, todavía no reintentó


def test_cada_reproceso_suma_un_intento(conn):
    graph = FakeGraph(conn)
    for _ in range(3):
        b.process_mention(graph, conn, _mention(), "open")
    assert _fila(conn) == ("failed", 2)


def test_el_intento_se_cuenta_aunque_el_grafo_explote(conn):
    """El punto fino: si el contador viviera en el paso a 'failed', un flujo que
    revienta antes de PostReplyNode no sumaría nunca y el loop seguiría igual.

    Un grafo que explota en caliente deja la mención en 'pending' (nadie llegó a
    marcarla), y de ahí la saca el rescate de los colgados. Ese ciclo completo
    —explotar, rescatar, reintentar— es el que tiene que sumar."""
    graph = FakeGraph(conn, explota=True)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            b.process_mention(graph, conn, _mention(), "open")
        conn.execute("UPDATE replied_posts SET replied_at = '2000-01-01T00:00:00+00:00'")
        b._rescatar_pending_colgados(conn)
    assert _fila(conn)[1] == 2
    assert len(graph.invocaciones) == 3


def test_una_mencion_respondida_no_acumula_intentos(conn):
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    b.update_status(conn, "u1", "replied")
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")   # el poll la vuelve a ver
    assert _fila(conn) == ("replied", 0)                   # ni se toca


# ─── el corte ──────────────────────────────────────────────────────────────

def test_has_replied_deja_reintentar_bajo_el_tope(conn):
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    b.update_status(conn, "u1", "failed")
    conn.execute("UPDATE replied_posts SET attempts = 2 WHERE uri = 'u1'")
    assert b.has_replied(conn, "u1") is False


def test_has_replied_corta_al_llegar_al_tope(conn):
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    b.update_status(conn, "u1", "failed")
    conn.execute("UPDATE replied_posts SET attempts = 3 WHERE uri = 'u1'")
    assert b.has_replied(conn, "u1") is True


def test_el_poll_deja_de_reprocesar_despues_del_tope(conn):
    """El caso de producción entero: la mención sigue en las notificaciones y el
    bot poll tras poll. Tiene que dejar de gastar."""
    graph = FakeGraph(conn)
    canal = FakeChannel([_mention()])
    for _ in range(10):
        b._poll_mentions(graph, conn, canal, "open")
    assert len(graph.invocaciones) == 4          # 1 intento + 3 reintentos, y basta
    assert _fila(conn) == ("failed", 3)


def test_el_tope_es_configurable(conn, monkeypatch):
    monkeypatch.setattr(b, "MENTION_MAX_RETRIES", 1)
    graph = FakeGraph(conn)
    canal = FakeChannel([_mention()])
    for _ in range(10):
        b._poll_mentions(graph, conn, canal, "open")
    assert len(graph.invocaciones) == 2


def test_el_tope_no_toca_menciones_nuevas(conn):
    """Un vecino quemado no puede silenciar al resto: el contador es por mención."""
    graph = FakeGraph(conn)
    conn.execute("INSERT INTO replied_posts (uri, cid, author, status, replied_at, "
                 "mode, attempts) VALUES ('viejo','c','ana.test','failed','x','open',9)")
    conn.commit()
    canal = FakeChannel([_mention("nueva")])
    b._poll_mentions(graph, conn, canal, "open")
    assert graph.invocaciones == ["nueva"]


# ─── el reinicio ───────────────────────────────────────────────────────────

def test_reiniciar_el_bot_no_reabre_el_loop(conn):
    """Sin esto el tope no sirve para nada: alcanzaba con reiniciar (y ante un bot
    que no contesta, reiniciar es lo primero que uno hace) para volver a empezar."""
    graph = FakeGraph(conn)
    conn.execute("INSERT INTO replied_posts (uri, cid, author, status, replied_at, "
                 "mode, attempts) VALUES ('u1','u1','ana.test','failed','x','open',3)")
    conn.commit()
    b.retry_stuck_mentions(graph, conn, FakeChannel([_mention()]), "open")
    assert graph.invocaciones == []


def test_al_arrancar_reintenta_las_que_todavia_tienen_credito(conn):
    graph = FakeGraph(conn)
    conn.execute("INSERT INTO replied_posts (uri, cid, author, status, replied_at, "
                 "mode, attempts) VALUES ('u1','u1','ana.test','failed','x','open',1)")
    conn.commit()
    b.retry_stuck_mentions(graph, conn, FakeChannel([_mention()]), "open")
    assert graph.invocaciones == ["u1"]


def test_un_pending_colgado_al_arrancar_sigue_reintentandose(conn):
    """El rescate de 'pending' (el bot murió a mitad) no cuenta intento por sí
    solo: el intento lo cuenta el reproceso, cuando efectivamente ocurre."""
    graph = FakeGraph(conn)
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    b.retry_stuck_mentions(graph, conn, FakeChannel([_mention()]), "open")
    assert graph.invocaciones == ["u1"]
    assert _fila(conn) == ("failed", 1)      # el reproceso, no el rescate


def test_un_pending_colgado_en_caliente_tampoco_se_pierde(conn):
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    conn.execute("UPDATE replied_posts SET replied_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    b._rescatar_pending_colgados(conn)
    assert _fila(conn) == ("failed", 0)
    assert b.has_replied(conn, "u1") is False


def test_la_migracion_agrega_attempts_a_una_db_vieja(tmp_path):
    """Las DBs en producción no tienen la columna; la migración corre sola y las
    menciones que ya estaban arrancan con crédito completo."""
    import sqlite3
    ruta = tmp_path / "vieja.db"
    vieja = sqlite3.connect(ruta)
    vieja.execute("CREATE TABLE replied_posts (uri TEXT PRIMARY KEY, cid TEXT NOT NULL, "
                  "author TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
                  "replied_at TEXT NOT NULL, mode TEXT NOT NULL)")
    vieja.execute("INSERT INTO replied_posts VALUES ('u1','c','ana.test','failed','x','open')")
    vieja.commit()
    vieja.close()

    c = dbmod.init_db(ruta)
    try:
        assert c.execute("SELECT attempts FROM replied_posts WHERE uri='u1'").fetchone()[0] == 0
    finally:
        c.close()


# ─── borrado ≠ error de red (el refetch no descarta ante la duda) ──────────

class CanalCaido(FakeChannel):
    """El refetch no puede saber si el post existe (red/PDS caído)."""

    def get_mention_by_uri(self, uri):
        from channels import MentionRefetchError
        raise MentionRefetchError("ConnectTimeout: timed out")


def test_arrancar_con_el_endpoint_caido_no_descarta_menciones(conn):
    """Regresión: get_mention_by_uri devolvía None tanto para "post borrado"
    como para CUALQUIER error de red, y retry_stuck_mentions marcaba 'ignored'
    permanente — un arranque con el PDS caído 30 segundos perdía todas las
    menciones pendientes."""
    graph = FakeGraph(conn)
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    b.update_status(conn, "u1", "failed")
    b.retry_stuck_mentions(graph, conn, CanalCaido([]), "open")
    assert _fila(conn) == ("failed", 0)      # queda para el próximo arranque
    assert graph.invocaciones == []


def test_un_post_borrado_de_verdad_si_se_ignora(conn):
    """None (el canal CONFIRMÓ que no existe) → 'ignored', como siempre."""
    graph = FakeGraph(conn)
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    b.update_status(conn, "u1", "failed")
    b.retry_stuck_mentions(graph, conn, FakeChannel([]), "open")
    assert _fila(conn)[0] == "ignored"


# ─── los dos ajustes de la barrida 2026-08-03 ──────────────────────────────

def test_el_retry_refresca_replied_at(conn):
    """Regresión: el ON CONFLICT no tocaba replied_at, que quedaba congelado en
    el PRIMER intento — el rescate de colgadas veía "pending hace 30 min" a una
    mención recién reintentada y la flipeaba al instante, anulando su gracia de
    10 minutos (el tope se quemaba al ritmo del poll en vez de espaciado)."""
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    conn.execute("UPDATE replied_posts SET replied_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    b.update_status(conn, "u1", "failed")
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")     # el retry
    b._rescatar_pending_colgados(conn)                       # recién reintentada:
    assert _fila(conn) == ("pending", 1)                     # NO la flipea


def test_el_rescate_corre_aunque_la_colgada_sea_la_unica_mencion(conn):
    """Regresión: el rescate corría solo `if mentions:`, pero la colgada es
    filtrada por has_replied — si era la única notificación, la lista quedaba
    vacía y el rescate no corría nunca: 'pending' eterno con el proceso vivo.
    Ahora corre antes del filtro, y la rescatada se procesa en ese mismo poll."""
    graph = FakeGraph(conn)
    b.mark_pending(conn, "u1", "u1", "ana.test", "open")
    conn.execute("UPDATE replied_posts SET replied_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    b._poll_mentions(graph, conn, FakeChannel([_mention()]), "open")
    assert graph.invocaciones == ["u1"]      # rescatada Y procesada en el poll
    assert _fila(conn) == ("failed", 1)      # el FakeGraph falla; contó el intento
