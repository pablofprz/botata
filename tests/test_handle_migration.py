"""Tests de migración de handle (fix del IntegrityError users.did UNIQUE).

Escenario real: un usuario se cambia el handle en Bluesky; su DID (estable) ya
vive en la fila vieja. Al mencionar al bot con el handle nuevo, LoadContextNode
crea la fila nueva y _ingest_bio debe MIGRAR la memoria, no explotar.
"""
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import db as d
import botata as b

_DIM = 1024


def _vec(seed: float) -> bytes:
    return struct.pack(f"<{_DIM}f", *([seed] * _DIM))


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "mig_test.db")
    # usuario con el handle VIEJO: did + facts (con vectores) + evento + bot_post
    c.execute("INSERT INTO users(handle, did) VALUES ('viejo.bsky.social', 'did:plc:xyz')")
    c.execute("INSERT INTO user_facts(handle, fact_text) VALUES ('viejo.bsky.social', 'vive en Rosario')")
    fid = c.execute("SELECT id FROM user_facts").fetchone()[0]
    c.execute("INSERT INTO user_facts_vec(rowid, embedding, partition_key) VALUES (?, ?, ?)",
              (fid, _vec(0.5), "viejo.bsky.social"))
    c.execute("INSERT INTO events(handle, title, event_at) "
              "VALUES ('viejo.bsky.social', 'cumple', '2099-01-01T00:00')")
    c.execute("INSERT INTO bot_posts(uri, reply_to_handle, text) "
              "VALUES ('at://bot/1', 'viejo.bsky.social', 'hola')")
    c.commit()
    return c


def test_migrate_user_handle(conn):
    conn.execute("INSERT INTO users(handle) VALUES ('nuevo.bsky.social')")
    conn.commit()
    moved = d.migrate_user_handle(conn, "viejo.bsky.social", "nuevo.bsky.social")
    assert moved == {"facts": 1, "events": 1}
    # todo migrado al handle nuevo
    assert conn.execute("SELECT handle FROM user_facts").fetchone()[0] == "nuevo.bsky.social"
    assert conn.execute("SELECT handle FROM events").fetchone()[0] == "nuevo.bsky.social"
    assert conn.execute("SELECT reply_to_handle FROM bot_posts").fetchone()[0] == "nuevo.bsky.social"
    assert conn.execute("SELECT COUNT(*) FROM users WHERE handle='viejo.bsky.social'").fetchone()[0] == 0
    # el vector quedó en la partición nueva (recuperable buscando por esa partición)
    row = conn.execute(
        "SELECT rowid FROM user_facts_vec WHERE embedding MATCH ? AND k = 1 AND partition_key = ?",
        (_vec(0.5), "nuevo.bsky.social")).fetchone()
    assert row is not None


def test_ingest_bio_con_did_repetido_no_explota(conn, monkeypatch):
    """El caso exacto del crash: DID ya existente en otra fila."""
    class FakeProfile:
        did = "did:plc:xyz"                       # el MISMO did de viejo.bsky.social
        display_name = "Panchito"
        description = ""

    class FakeBsky:
        def get_profile(self, handle):
            return FakeProfile()

    node = b.LoadContextNode.__new__(b.LoadContextNode)
    node.bsky, node.conn, node.llm = FakeBsky(), conn, None
    conn.execute("INSERT INTO users(handle) VALUES ('nuevo.bsky.social')")
    conn.commit()
    node._ingest_bio("nuevo.bsky.social")         # antes: IntegrityError
    row = conn.execute("SELECT did, display_name FROM users WHERE handle='nuevo.bsky.social'").fetchone()
    assert row[0] == "did:plc:xyz" and row[1] == "Panchito"
    # la memoria del handle viejo lo siguió
    assert conn.execute("SELECT handle FROM user_facts").fetchone()[0] == "nuevo.bsky.social"


def test_load_context_no_propaga_errores_de_bio(conn):
    """Blindaje: un error inesperado en la bio no bloquea el reply ni el arranque."""
    class BoomBsky:
        def get_profile(self, handle):
            raise RuntimeError("api caída")

    node = b.LoadContextNode.__new__(b.LoadContextNode)
    node.bsky, node.conn, node.llm = BoomBsky(), conn, None
    out = node.run({"author_handle": "alguien.bsky.social"})   # no lanza
    assert out == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
