"""Tests de channels.py (T28): MastodonChannel contra una API falsa."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from channels import MastodonChannel, strip_html, truncate_post  # noqa: E402


# ─── API falsa (shape de Mastodon.py: dicts con acceso por clave) ────────────
def _status(id, content, acct="ana", media=None, in_reply_to=None):
    return {
        "id": id,
        "content": content,
        "account": {"acct": acct, "id": 900, "display_name": "Ana",
                    "note": "<p>bio de ana</p>"},
        "media_attachments": media or [],
        "in_reply_to_id": in_reply_to,
        "created_at": "2026-07-24T12:00:00+00:00",
    }


class FakeApi:
    def __init__(self):
        self.posted = []
        self.blocked = []
        self.statuses = {
            "10": _status("10", "<p>hola bot</p>"),
            "11": _status("11", "<p>seguimos el hilo</p>", in_reply_to="10"),
        }
        self.contexts = {"11": {"ancestors": [self.statuses["10"]], "descendants": []}}
        self.notifs = [
            {"type": "mention", "account": {"acct": "ana"},
             "status": _status("11", "<p>@bot qué opinás?</p>")},
            {"type": "favourite", "account": {"acct": "otro"}, "status": None},
        ]

    def account_verify_credentials(self):
        return {"id": 1, "acct": "botata"}

    def notifications(self, limit=25):
        return self.notifs

    def status(self, id):
        return self.statuses[str(id)]

    def status_context(self, id):
        return self.contexts.get(str(id), {"ancestors": [], "descendants": []})

    def status_post(self, text, in_reply_to_id=None, media_ids=None):
        self.posted.append({"text": text, "in_reply_to_id": in_reply_to_id,
                            "media_ids": media_ids})
        return {"id": 99, "url": "https://masto.example/@botata/99"}

    def account_lookup(self, acct):
        if acct == "fantasma":
            raise ValueError("not found")
        return {"id": 900, "acct": acct, "display_name": "Ana",
                "note": "<p>bio de <b>ana</b></p>"}

    def account_search(self, q, limit=1):
        return []

    def account_block(self, id):
        self.blocked.append(id)

    def timeline_home(self, limit=40):
        return [self.statuses["10"], self.statuses["11"]]


def make_channel():
    api = FakeApi()
    return MastodonChannel("https://masto.example", "tok", api=api), api


# ─── helpers ─────────────────────────────────────────────────────────────────
def test_strip_html():
    assert strip_html("<p>hola <b>mundo</b></p>") == "hola mundo"
    assert strip_html("<p>uno</p><p>dos</p>") == "uno\n\ndos"
    assert strip_html("línea<br/>otra") == "línea\notra"
    assert strip_html("&amp; eso") == "& eso"
    assert strip_html("") == ""


def test_truncate_post_frontera_de_oracion():
    text = "Primera oración. Segunda que no entra porque es larguísima de verdad"
    out = truncate_post(text, 40)
    assert out == "Primera oración."
    assert truncate_post("corto", 40) == "corto"


# ─── contrato Channel ────────────────────────────────────────────────────────
def test_login_y_handle():
    ch, _ = make_channel()
    assert ch.handle == "botata"


def test_get_mentions_filtra_y_mapea():
    ch, _ = make_channel()
    mentions = ch.get_mentions()
    assert len(mentions) == 1  # el favourite se filtra
    m = mentions[0]
    assert m["uri"] == "11" and m["cid"] == "11"
    assert m["author_handle"] == "ana"
    assert m["text"] == "@bot qué opinás?"


def test_get_thread_info_ancestros_y_root():
    ch, _ = make_channel()
    ctx, root_uri, root_cid, media = ch.get_thread_info("11", "11")
    assert ctx == "ana: hola bot"
    assert root_uri == "10" and root_cid == "10"
    assert media == ""


def test_get_thread_info_media_alt_text():
    ch, api = make_channel()
    api.statuses["11"]["media_attachments"] = [
        {"type": "image", "description": "un gato naranja"},
        {"type": "video", "description": ""},
    ]
    *_, media = ch.get_thread_info("11", "11")
    assert media == "[image: un gato naranja] [video sin descripción]"


def test_reply_antepone_mencion_al_autor():
    ch, api = make_channel()
    out = ch.reply("de acuerdo con todo", "11", "11", "10", "10")
    assert out == "99"
    posted = api.posted[-1]
    assert posted["text"].startswith("@ana ")
    assert posted["in_reply_to_id"] == "11"
    # si el texto ya menciona al autor, no duplica
    ch.reply("@ana tal cual", "11", "11", "10", "10")
    assert api.posted[-1]["text"] == "@ana tal cual"


def test_post_trunca_y_postea():
    ch, api = make_channel()
    out = ch.post("un posteo proactivo")
    assert out == "99"
    assert api.posted[-1]["text"] == "un posteo proactivo"
    assert api.posted[-1]["in_reply_to_id"] is None


def test_get_profile_adapta_bio():
    ch, _ = make_channel()
    p = ch.get_profile("@ana")
    assert p.did == "900"
    assert p.display_name == "Ana"
    assert p.description == "bio de ana"
    assert ch.get_profile("fantasma") is None


def test_block_user():
    ch, api = make_channel()
    assert ch.block_user("ana") is True
    assert api.blocked == [900]
    assert ch.block_user("fantasma") is False


def test_get_mention_by_uri_reconstruye():
    ch, _ = make_channel()
    m = ch.get_mention_by_uri("11")
    assert m["uri"] == "11" and m["author_handle"] == "ana"
    assert m["thread_context"] == "ana: hola bot"
    assert m["thread_root_uri"] == "10"


def test_get_feed_posts_following():
    ch, _ = make_channel()
    posts = ch.get_feed_posts("following", None, since=None)
    assert [p["uri"] for p in posts] == ["10", "11"]
    assert posts[0]["handle"] == "ana"
    assert ch.get_feed_posts("feed", "x", since=None) == []  # tipo bluesky-only
