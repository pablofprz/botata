"""Canal WhatsApp sobre un bridge local (T41).

WhatsApp no tiene API usable para un bot comunitario, así que se entra como
DISPOSITIVO VINCULADO — y eso lo hace una librería que no es Python (Baileys en
Node, whatsmeow en Go). A diferencia de Mastodon y Discord, acá hay un proceso
aparte: el bridge.

Todo lo que se testea acá corre contra un **bridge falso**: el contrato HTTP es
la frontera, así que el canal entero se puede escribir y verificar sin vincular
un número, sin gastarle vida útil al chip y sin haber elegido todavía el motor.

Lo que más importa: el ALLOWLIST de chats. Todos los dispositivos vinculados
reciben todos los mensajes, así que compartir el número con otro bot solo
funciona si cada uno decide en qué chats actúa. Si eso falla, el bot contesta
en conversaciones personales.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

from channels import WhatsAppChannel  # noqa: E402

YO = "5491100000000@s.whatsapp.net"
OTRO = "5491111111111@s.whatsapp.net"
GRUPO = "12036300000000@g.us"
PRIVADO = "5491122222222@s.whatsapp.net"


def _msg(mid, chat=GRUPO, autor=OTRO, text="hola", **kw):
    base = {"id": mid, "chat_id": chat, "chat_name": "La comunidad",
            "author_id": autor, "author_name": "Fulano", "text": text,
            "quoted_id": None, "quoted_author_id": None, "mentions": [],
            "media": [], "from_me": False, "ts": 1}
    base.update(kw)
    return base


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class BridgeFalso:
    """Implementa el contrato del bridge en memoria. Es la única pieza que
    habría que reemplazar por el proceso real (Baileys/whatsmeow)."""

    def __init__(self, mensajes=(), connected=True):
        self.cola = list(mensajes)
        self.connected = connected
        self.enviados: list[dict] = []
        self.entregados = 0

    def request(self, method, path, params=None, json=None, **kw):
        if path == "/status":
            return _Resp({"connected": self.connected, "qr": None,
                          "me": {"id": YO, "name": "Botata"}})
        if path == "/messages":
            pendientes = self.cola[self.entregados:]
            self.entregados = len(self.cola)
            return _Resp({"cursor": str(self.entregados), "messages": pendientes})
        if path.startswith("/messages/"):
            mid = path.rsplit("/", 1)[1]
            hallado = next((m for m in self.cola if m["id"] == mid), None)
            return _Resp(hallado) if hallado else _Resp(None, 404)
        if path == "/send":
            self.enviados.append(json)
            return _Resp({"id": f"enviado-{len(self.enviados)}"})
        if path == "/profile":
            return _Resp(None, 204)
        return _Resp(None, 404)


def _canal(bridge):
    return WhatsAppChannel("http://x", [GRUPO], http=bridge)


# ─── Vinculación ────────────────────────────────────────────────────────────
def test_sin_vincular_corta_con_instrucciones(capsys):
    """El error tiene que decir qué hacer: escanear el QR no es adivinable."""
    with pytest.raises(SystemExit) as e:
        _canal(BridgeFalso(connected=False))
    assert "Dispositivos vinculados" in str(e.value)


def test_toma_su_identidad_del_bridge():
    c = _canal(BridgeFalso())
    assert c.handle == "Botata" and c._me_id == YO


# ─── El allowlist: la partición con cualquier otro cliente del número ───────
def test_ignora_los_chats_que_no_estan_en_la_lista():
    """Todos los dispositivos vinculados reciben TODO. Sin este filtro, el bot
    contestaría en las conversaciones personales del número."""
    b = BridgeFalso([
        _msg("1", chat=PRIVADO, text="che @5491100000000", mentions=[YO]),
        _msg("2", chat=GRUPO, text="che @5491100000000", mentions=[YO]),
    ])
    menciones = _canal(b).get_mentions()
    assert [m["cid"] for m in menciones] == ["2"]


def test_solo_contesta_si_lo_mencionan_o_lo_citan():
    b = BridgeFalso([
        _msg("1", text="charla suelta del grupo"),
        _msg("2", text="che @5491100000000 mirá", mentions=[YO]),
        _msg("3", text="jaja", quoted_id="99", quoted_author_id=YO),
        _msg("4", text="citando a otro", quoted_id="98", quoted_author_id=OTRO),
    ])
    assert [m["cid"] for m in _canal(b).get_mentions()] == ["2", "3"]


def test_no_se_contesta_a_si_mismo():
    """Anti-loop: el bot ve sus propios mensajes como cualquier dispositivo."""
    b = BridgeFalso([
        _msg("1", autor=YO, from_me=True, text="@5491100000000", mentions=[YO]),
        _msg("2", autor=YO, text="algo mío", mentions=[YO]),
    ])
    assert _canal(b).get_mentions() == []


def test_el_cursor_avanza_y_no_relee():
    b = BridgeFalso([_msg("1", text="@yo", mentions=[YO])])
    c = _canal(b)
    assert len(c.get_mentions()) == 1
    assert c.get_mentions() == []          # nada nuevo
    b.cola.append(_msg("2", text="@yo de nuevo", mentions=[YO]))
    assert [m["cid"] for m in c.get_mentions()] == ["2"]


def test_si_el_bridge_se_cae_el_loop_no_muere():
    class _Roto(BridgeFalso):
        def request(self, method, path, **kw):
            if path == "/messages":
                raise ConnectionError("bridge caído")
            return super().request(method, path, **kw)

    assert _canal(_Roto()).get_mentions() == []


# ─── Mapeo al contrato del grafo ────────────────────────────────────────────
def test_la_mencion_tiene_la_forma_que_espera_el_grafo():
    b = BridgeFalso([_msg("7", text="@5491100000000 hola", mentions=[YO])])
    m = _canal(b).get_mentions()[0]
    assert m["uri"] == f"{GRUPO}/7" and m["cid"] == "7"
    assert m["author_handle"] == OTRO       # el JID es el id opaco, como en Discord
    assert m["text"] == "@5491100000000 hola"


def test_las_menciones_se_ven_legibles_no_como_jid():
    b = BridgeFalso([_msg("1", text=f"che @{YO} mirá", mentions=[YO])])
    assert _canal(b).get_mentions()[0]["text"] == "che @5491100000000 mirá"


def test_el_hilo_sube_por_las_citas():
    b = BridgeFalso([
        _msg("1", text="arranco yo"),
        _msg("2", text="te respondo", quoted_id="1"),
        _msg("3", text="y yo", quoted_id="2", quoted_author_id=OTRO),
    ])
    c = _canal(b)
    c.get_mentions()                        # llena el cache
    ctx, root_uri, root_cid, _ = c.get_thread_info(f"{GRUPO}/3", "3")
    assert ctx.splitlines() == ["Fulano: arranco yo", "Fulano: te respondo",
                                "Fulano: y yo"]
    assert root_uri == f"{GRUPO}/1" and root_cid == "1"


def test_un_mensaje_sin_cita_es_su_propia_raiz():
    """En un grupo la gente contesta sin citar: la cadena se corta ahí y no es
    un error, es cómo funciona WhatsApp."""
    b = BridgeFalso([_msg("5", text="suelto")])
    c = _canal(b)
    c.get_mentions()
    ctx, root_uri, _, _ = c.get_thread_info(f"{GRUPO}/5", "5")
    assert ctx == "Fulano: suelto" and root_uri == f"{GRUPO}/5"


def test_la_media_se_describe_solo_donde_importa():
    """Vision sobre el mensaje que se va a contestar, nunca en lectura masiva:
    sería una llamada por imagen y por mensaje leído."""
    b = BridgeFalso([_msg("1", text="mirá esto", mentions=[YO],
                          media=[{"url": "http://x/f.jpg", "mime": "image/jpeg",
                                  "filename": "f.jpg"}])])
    c = _canal(b)
    c.set_media_describer(lambda src: "un mapache con anteojos")
    assert "[image:" not in c.get_mentions()[0]["text"]        # lectura masiva: sin vision
    m = c.get_mention_by_uri(f"{GRUPO}/1")
    assert "un mapache con anteojos" in m["text"]


def test_media_sin_describidor_cae_al_nombre_del_archivo():
    b = BridgeFalso([_msg("1", media=[{"mime": "video/mp4", "filename": "clip.mp4"}])])
    c = _canal(b)
    c.get_mentions()
    assert "[video: clip.mp4]" in c.get_mention_by_uri(f"{GRUPO}/1")["text"]


# ─── Escritura ──────────────────────────────────────────────────────────────
def test_reply_cita_el_mensaje_y_devuelve_uri():
    b = BridgeFalso()
    uri = _canal(b).reply("dale", f"{GRUPO}/7", "7", f"{GRUPO}/7", "7")
    assert b.enviados[0] == {"chat_id": GRUPO, "text": "dale", "reply_to": "7"}
    assert uri == f"{GRUPO}/enviado-1"


def test_post_sin_target_va_al_chat_principal():
    b = BridgeFalso()
    _canal(b).post("buenas")
    assert b.enviados[0]["chat_id"] == GRUPO


def test_post_con_target_va_al_chat_de_la_rutina():
    b = BridgeFalso()
    c = WhatsAppChannel("http://x", [GRUPO, "otro@g.us"], http=b)
    c.post("hola", target="otro@g.us")
    assert b.enviados[0]["chat_id"] == "otro@g.us"


def test_sin_chats_configurados_postear_falla_claro():
    c = WhatsAppChannel("http://x", [], http=BridgeFalso())
    with pytest.raises(RuntimeError, match="WHATSAPP_CHAT_IDS"):
        c.post("hola")


def test_saca_los_links_de_media_inventados():
    """Misma limpieza que en los otros canales: el modelo a veces escribe la
    URL de una imagen que en realidad va adjunta."""
    b = BridgeFalso()
    _canal(b).post("miren [📸 un mapache](https://cdn.fake/x.jpg)")
    assert "cdn.fake" not in b.enviados[0]["text"]


def test_la_media_va_por_path_al_bridge():
    b = BridgeFalso()
    _canal(b).reply("tomá", f"{GRUPO}/1", "1", f"{GRUPO}/1", "1",
                    media_path="C:/tmp/foto.jpg")
    assert b.enviados[0]["media_path"] == "C:/tmp/foto.jpg"


# ─── Lo que WhatsApp no tiene ───────────────────────────────────────────────
def test_no_hay_feed():
    assert _canal(BridgeFalso()).get_feed_posts("list", None) == []


def test_get_profile_da_el_nombre_pero_no_bio():
    b = BridgeFalso([_msg("1")])
    c = _canal(b)
    c.get_mentions()
    p = c.get_profile(OTRO)
    assert p.display_name == "Fulano" and p.description == ""


def test_block_user_es_no_op_declarado():
    assert _canal(BridgeFalso()).block_user(OTRO) is False
