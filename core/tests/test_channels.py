"""Tests de channels.py (T28): MastodonChannel y DiscordChannel contra APIs falsas."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from channels import DiscordChannel, MastodonChannel, strip_html, truncate_post  # noqa: E402


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


# ═══ DiscordChannel ══════════════════════════════════════════════════════════
_BOT = {"id": "1", "username": "botata", "bot": True}
_ANA = {"id": "900", "username": "ana", "global_name": "Ana"}
_OTRO_BOT = {"id": "666", "username": "spambot", "bot": True}


def _msg(id, content, author=_ANA, mentions=None, ref=None, ref_msg=None,
         attachments=None):
    m = {
        "id": str(id),
        "content": content,
        "author": author,
        "mentions": mentions or [],
        "attachments": attachments or [],
        "timestamp": "2026-07-25T12:00:00+00:00",
    }
    if ref:
        m["message_reference"] = {"message_id": str(ref)}
        if ref_msg:
            m["referenced_message"] = ref_msg
    return m


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.headers = data, status, {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeDiscordHttp:
    """Shape mínimo de httpx.Client que DiscordChannel usa (.request)."""

    def __init__(self):
        self.posted = []
        self.reactions = []
        bot_hello = _msg(15, "hola gente, soy botata", author=_BOT)
        self.messages = {
            "15": bot_hello,
            # mención directa al bot
            "20": _msg(20, "<@1> qué opinás?", mentions=[_BOT]),
            # reply a un mensaje del bot, sin mencionarlo
            "21": _msg(21, "seguí contando", ref=15, ref_msg=bot_hello),
            # charla ajena y otro bot: se ignoran
            "22": _msg(22, "hablando de otra cosa"),
            "23": _msg(23, "<@1> spam", author=_OTRO_BOT, mentions=[_BOT]),
            # un mensaje del propio bot: se ignora
            "24": _msg(24, "yo mismo", author=_BOT),
        }

    def request(self, method, path, params=None, json=None, files=None, data=None):
        if path == "/users/@me":
            return FakeResp(_BOT)
        if method == "GET" and path == "/channels/111/messages":
            ids = ["24", "23", "22", "21", "20"]  # reverso-cronológico
            return FakeResp([self.messages[i] for i in ids])
        if method == "GET" and path.startswith("/channels/111/messages/"):
            mid = path.rsplit("/", 1)[1]
            if mid not in self.messages:
                return FakeResp({"message": "Unknown Message"}, status=404)
            return FakeResp(self.messages[mid])
        if method == "POST" and path == "/channels/111/messages":
            self.posted.append({"json": json, "files": files, "data": data})
            return FakeResp({"id": "99"})
        if method == "PUT" and path.startswith("/channels/111/") and "/reactions/" in path:
            self.reactions.append(path)
            return FakeResp(None, status=204)
        return FakeResp({"message": "Not Found"}, status=404)


def make_discord():
    http = FakeDiscordHttp()
    return DiscordChannel("tok", ["111"], http=http), http


def test_discord_login_y_handle():
    ch, _ = make_discord()
    assert ch.handle == "botata"


def test_discord_get_mentions_filtra_y_mapea():
    ch, _ = make_discord()
    mentions = ch.get_mentions()
    # quedan la mención directa (20) y la reply al bot (21); se filtran
    # el mensaje propio, el otro bot y la charla ajena
    assert {m["uri"] for m in mentions} == {"111/20", "111/21"}
    m20 = next(m for m in mentions if m["cid"] == "20")
    assert m20["author_handle"] == "ana"
    assert m20["text"] == "@botata qué opinás?"  # <@1> traducido


def test_discord_thread_info_cadena_de_replies():
    ch, _ = make_discord()
    ctx, root_uri, root_cid, media = ch.get_thread_info("111/21", "21")
    assert ctx == "botata: hola gente, soy botata"
    assert root_uri == "111/15" and root_cid == "15"
    assert media == ""


def test_discord_reply_referencia_al_parent():
    ch, http = make_discord()
    out = ch.reply("de acuerdo", "111/20", "20", "111/20", "20")
    assert out == "111/99"
    payload = http.posted[-1]["json"]
    assert payload["content"] == "de acuerdo"
    assert payload["message_reference"]["message_id"] == "20"


def test_discord_post_al_canal_principal():
    ch, http = make_discord()
    out = ch.post("un posteo proactivo")
    assert out == "111/99"
    assert http.posted[-1]["json"] == {"content": "un posteo proactivo"}


def test_discord_get_mention_by_uri():
    ch, _ = make_discord()
    m = ch.get_mention_by_uri("111/21")
    assert m["uri"] == "111/21" and m["author_handle"] == "ana"
    assert m["thread_context"] == "botata: hola gente, soy botata"
    assert m["thread_root_uri"] == "111/15"
    assert ch.get_mention_by_uri("111/404") is None


def test_discord_get_profile_cache_de_vistos():
    ch, _ = make_discord()
    assert ch.get_profile("ana") is None  # todavía no vista
    ch.get_mentions()
    p = ch.get_profile("@ana")
    assert p.did == "900" and p.display_name == "Ana" and p.description == ""
    assert ch.resolve_did("ana") == "900"


def test_discord_block_user_no_op():
    ch, _ = make_discord()
    assert ch.block_user("ana") is False


def test_discord_feed_posts_tipo_channel():
    ch, _ = make_discord()
    posts = ch.get_feed_posts("channel", "111", since=None)
    # sin bots (23, 24 fuera) — quedan los mensajes humanos
    assert {p["uri"] for p in posts} == {"111/20", "111/21", "111/22"}
    p21 = next(p for p in posts if p["uri"] == "111/21")
    assert p21["reply_to"] == "111/15"
    assert ch.get_feed_posts("following", None, since=None) == []


def test_discord_media_note_en_attachments():
    ch, http = make_discord()
    http.messages["21"]["attachments"] = [
        {"content_type": "image/png", "filename": "gato.png"},
        {"filename": "audio.ogg"},
    ]
    *_, media = ch.get_thread_info("111/21", "21")
    assert media == "[image: gato.png] [file: audio.ogg]"


# ─── T39: vision sobre attachments (antes el bot solo veía el nombre) ────────
def test_discord_vision_describe_la_imagen_del_mensaje():
    ch, http = make_discord()
    http.messages["21"]["attachments"] = [
        {"content_type": "image/png", "filename": "gato.png",
         "url": "https://cdn.discordapp.com/gato.png"},
    ]
    vistos = []
    ch.set_media_describer(lambda url: vistos.append(url) or "un gato con anteojos")
    *_, media = ch.get_thread_info("111/21", "21")
    assert vistos == ["https://cdn.discordapp.com/gato.png"]   # se llamó con la URL del CDN
    assert media == "[image: un gato con anteojos]"            # y entra la descripción


def test_discord_vision_no_corre_en_la_lectura_del_feed():
    """Costo: una llamada de vision por imagen y por mensaje leído sería carísimo."""
    ch, http = make_discord()
    http.messages["22"]["attachments"] = [
        {"content_type": "image/png", "filename": "x.png", "url": "https://cdn/x.png"},
    ]
    llamadas = []
    ch.set_media_describer(lambda url: llamadas.append(url) or "algo")
    ch.get_feed_posts("channel", "111", since=None)
    assert llamadas == []


def test_discord_vision_falla_sin_romper():
    ch, http = make_discord()
    http.messages["21"]["attachments"] = [
        {"content_type": "image/png", "filename": "gato.png", "url": "https://cdn/g.png"},
    ]
    def _boom(url):
        raise RuntimeError("vision caída")
    ch.set_media_describer(_boom)
    *_, media = ch.get_thread_info("111/21", "21")
    assert media == "[image: gato.png]"      # cae a la anotación barata


# ─── T39: el poll no puede perder menciones ─────────────────────────────────
class CatchupHttp(FakeDiscordHttp):
    """Fake que respeta `after` y puede simular un canal con mucho movimiento."""

    def __init__(self, extra_ids=()):
        super().__init__()
        self.pages = []                       # (after, limit) de cada request
        for i in extra_ids:
            self.messages[str(i)] = _msg(i, f"<@1> mensaje {i}", mentions=[_BOT])

    def request(self, method, path, params=None, json=None, files=None, data=None):
        if method == "GET" and path == "/channels/111/messages":
            params = params or {}
            self.pages.append((params.get("after"), params.get("limit")))
            limit = params.get("limit", 50)
            after = params.get("after")
            if after is None:
                # sin cursor: los MÁS NUEVOS del canal (ventana inicial)
                ids = sorted((int(i) for i in self.messages), reverse=True)[:limit]
            else:
                # con `after`: los inmediatamente SIGUIENTES al cursor — es lo que
                # hace de `after` la primitiva de paginado hacia adelante (si
                # devolviera los más nuevos, el medio se perdería).
                ids = sorted(i for i in map(int, self.messages) if i > int(after))[:limit]
                ids.reverse()   # la respuesta viene igual en orden reverso-cronológico
            return FakeResp([self.messages[str(i)] for i in ids])
        return super().request(method, path, params, json, files, data)


def test_discord_segundo_poll_solo_trae_lo_nuevo():
    http = CatchupHttp()
    ch = DiscordChannel("tok", ["111"], http=http)
    assert len(ch.get_mentions()) == 2          # 20 (mención) y 21 (reply al bot)
    assert ch.get_mentions() == []              # nada nuevo → no relee lo mismo
    assert http.pages[-1][0] == "24"            # y usó el cursor del último visto

    # llega una mención nueva: aparece sin releer todo el historial
    http.messages["30"] = _msg(30, "<@1> y ahora?", mentions=[_BOT])
    nuevas = ch.get_mentions()
    assert [m["uri"] for m in nuevas] == ["111/30"]


def test_discord_canal_activo_no_pierde_menciones():
    """El bug: con un lote fijo de 25, un canal movido dejaba menciones sin leer."""
    http = CatchupHttp()
    ch = DiscordChannel("tok", ["111"], http=http)
    ch.get_mentions()                                   # fija el cursor
    for i in range(100, 400):                           # 300 mensajes de un saque
        http.messages[str(i)] = _msg(i, f"<@1> hola {i}", mentions=[_BOT])
    assert len(ch.get_mentions()) == 300                 # las ve TODAS (paginó)
    assert len(http.pages) > 3                           # y necesitó varias páginas


def test_discord_tope_de_paginas_por_ciclo(caplog):
    """Pero con un tope: un canal desbocado no se come el ciclo — y avisa."""
    http = CatchupHttp()
    ch = DiscordChannel("tok", ["111"], http=http)
    ch.CATCHUP_PAGE, ch.MAX_CATCHUP_PAGES = 10, 3        # tope chico para el test
    ch.get_mentions()
    for i in range(100, 200):
        http.messages[str(i)] = _msg(i, f"<@1> hola {i}", mentions=[_BOT])
    with caplog.at_level("WARNING"):
        vistas = ch.get_mentions()
    assert len(vistas) == 30                             # 3 páginas × 10
    assert "menciones sin leer" in caplog.text           # el admin se entera


# ─── "me gusta" (like/favourite/reacción) ────────────────────────────────────
def test_mastodon_like_post():
    ch, api = make_channel()
    api.favs = []
    api.status_favourite = api.favs.append
    assert ch.like_post("11", "11") is True
    assert api.favs == ["11"]


def test_mastodon_like_post_falla_sin_romper():
    ch, api = make_channel()
    def _explota(_id):
        raise RuntimeError("403")
    api.status_favourite = _explota
    assert ch.like_post("11", "11") is False


def test_discord_like_post_reacciona():
    ch, http = make_discord()
    assert ch.like_post("111/20", "20") is True
    assert http.reactions == ["/channels/111/messages/20/reactions/%E2%9D%A4%EF%B8%8F/@me"]


def test_discord_like_post_falla_sin_romper():
    ch, http = make_discord()
    assert ch.like_post("222/20", "20") is False   # canal que el fake no conoce → 404
