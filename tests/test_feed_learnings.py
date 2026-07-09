"""Tests del aprendizaje del feed (T6, LearnFromFeedNode).

Spy sobre upsert_user_fact para no cargar bge-m3; create_event/event_exists/user_exists
corren de verdad (baratos, sin embeddings).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import butterbot as b  # noqa: E402
import db as d  # noqa: E402


class FakeLLM:
    def __init__(self, learnings: dict):
        self._learnings = learnings

    def complete(self, system, user, model):
        return b.FeedLearnings(**self._learnings)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = d.init_db(tmp_path / "learn_test.db")
    conn.execute("INSERT OR IGNORE INTO users(handle) VALUES ('ppolci.com')")  # único con perfil
    conn.commit()
    calls: list[tuple[str, str]] = []

    def spy_upsert(c, handle, fact_text, source_uri=None, **kw):
        calls.append((handle, fact_text))
        return len(calls)  # id ficticio (no-None → cuenta como insert)

    monkeypatch.setattr(b.dbmod, "upsert_user_fact", spy_upsert)

    def make_node(learnings: dict):
        return b.LearnFromFeedNode(FakeLLM(learnings), conn), calls

    return conn, make_node


def _posts(*handles):
    return [{"handle": h, "text": "x", "uri": f"at://{h}/1"} for h in handles]


def test_facts_gated_by_profile(env):
    conn, make_node = env
    node, calls = make_node({
        "facts": [
            {"handle": "ppolci.com", "fact": "Vive en Rosario"},
            {"handle": "random.com", "fact": "Le gusta el mate"},  # sin perfil → salteado
        ],
        "events": [],
    })
    res = node.run({"feed_name": "f", "posts": _posts("ppolci.com", "random.com"), "learn": True})
    assert calls == [("ppolci.com", "Vive en Rosario")]  # random.com no pasó el gate
    assert res["learned_facts"] == 1


def test_event_community_when_owner_has_no_profile(env):
    conn, make_node = env
    node, _ = make_node({
        "facts": [],
        "events": [
            {"title": "Cumple", "event_at": "2026-08-01", "handle": "ppolci.com", "kind": "birthday"},
            {"title": "Maripocine", "event_at": "2026-08-02", "handle": "nolo.com", "kind": "community"},
        ],
    })
    node.run({"feed_name": "f", "posts": _posts("ppolci.com"), "learn": True})
    rows = {(r["title"], r["handle"]) for r in conn.execute("SELECT title, handle FROM events").fetchall()}
    assert ("Cumple", "ppolci.com") in rows
    assert ("Maripocine", None) in rows  # nolo sin perfil → comunidad (respeta FK)


def test_events_idempotent(env):
    conn, make_node = env
    learnings = {"facts": [], "events": [
        {"title": "Cumple", "event_at": "2026-08-01", "handle": "ppolci.com", "kind": "birthday"},
    ]}
    node, _ = make_node(learnings)
    state = {"feed_name": "f", "posts": _posts("ppolci.com"), "learn": True}
    r1 = node.run(state)
    r2 = node.run(state)  # segunda corrida: event_exists lo saltea
    assert r1["learned_events"] == 1 and r2["learned_events"] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_learn_disabled_noop(env):
    conn, make_node = env
    node, calls = make_node({"facts": [{"handle": "ppolci.com", "fact": "x"}], "events": []})
    res = node.run({"feed_name": "f", "posts": _posts("ppolci.com"), "learn": False})
    assert res == {} and calls == []  # toggle off → no hace nada
