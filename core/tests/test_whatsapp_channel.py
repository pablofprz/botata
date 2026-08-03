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

import channels as ch  # noqa: E402
from channels import WhatsAppChannel  # noqa: E402

YO = "5491100000000@s.whatsapp.net"
# La segunda identidad del bot: los grupos nuevos direccionan por LID, un id
# opaco que no tiene nada que ver con el número.
YO_LID = "104913921159305@lid"
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
            e = RuntimeError(f"HTTP {self.status_code}")
            e.response = self  # como httpx.HTTPStatusError: el status viaja acá
            raise e


class BridgeFalso:
    """Implementa el contrato del bridge en memoria. Es la única pieza que
    habría que reemplazar por el proceso real (Baileys/whatsmeow)."""

    def __init__(self, mensajes=(), connected=True):
        self.cola = list(mensajes)
        self.connected = connected
        self.enviados: list[dict] = []
        self.reacciones: list[dict] = []
        self.entregados = 0

    def request(self, method, path, params=None, json=None, **kw):
        if path == "/status":
            return _Resp({"connected": self.connected, "qr": None,
                          "me": {"id": YO, "lid": YO_LID, "name": "Botata"}})
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
        if path == "/react":
            self.reacciones.append(json)
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
    # El teléfono pelado, no el JID: es lo que el admin escribe en ADMIN_HANDLE
    # y en USER_GROUPS, y tiene que matchear con quien escribe en el grupo.
    assert m["author_handle"] == "5491111111111"
    assert m["text"] == "@Botata hola"      # a sí mismo se lee por su nombre


def test_las_menciones_se_ven_legibles_no_como_jid():
    """Un JID crudo en el texto no le dice nada al LLM. Al bot se lo nombra por
    su nombre; a los demás, por su número (que es su identidad acá)."""
    b = BridgeFalso([_msg("1", text=f"che @{YO} y @{OTRO} miren", mentions=[YO, OTRO])])
    assert _canal(b).get_mentions()[0]["text"] == "che @Botata y @5491111111111 miren"


def test_la_raiz_sigue_subiendo_por_las_citas():
    """El contexto que ve el LLM es la ventana de conversación, pero la RAÍZ
    (con la que el motor agrupa la charla en su DB) sale de la cadena de citas:
    son dos cosas distintas y las dos tienen que seguir andando."""
    b = BridgeFalso([
        _msg("1", text="arranco yo"),
        _msg("2", text="te respondo", quoted_id="1"),
        _msg("3", text="y yo", quoted_id="2", quoted_author_id=OTRO),
    ])
    c = _canal(b)
    c.get_mentions()                        # llena el cache
    ctx, root_uri, root_cid, _ = c.get_thread_info(f"{GRUPO}/3", "3")
    assert root_uri == f"{GRUPO}/1" and root_cid == "1"
    assert ctx.splitlines()[0] == "Fulano: arranco yo"
    assert ctx.splitlines()[-1].endswith("y yo")


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


# ─── Panel de vinculación en la UI ──────────────────────────────────────────
# Vincular es el único paso de config que no es escribir en un campo: hay que
# escanear un QR. Si no se ve en la UI, el admin tiene que ir a leer la terminal
# del bridge — el muro que este proyecto no le quiere poner a nadie.
@pytest.fixture()
def store(tmp_path):
    import config_ui
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text(
        json.dumps({"CHANNEL": "whatsapp", "WHATSAPP_BRIDGE_URL": "http://127.0.0.1:9",
                    "WHATSAPP_CHAT_IDS": [GRUPO]}), encoding="utf-8")
    return config_ui.ConfigStore(tmp_path)


def test_si_el_bridge_no_esta_levantado_lo_dice_en_criollo(store):
    out = store.run_action("whatsapp_status")
    assert out["ok"] is False
    assert "proceso aparte" in out["errors"][0]


def test_devuelve_el_qr_para_mostrarlo(store, monkeypatch):
    import urllib.request

    class _R:
        def read(self): return json.dumps(
            {"connected": False, "qr": "2@abc...", "me": None}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _R())
    out = store.run_action("whatsapp_status")
    assert out["ok"] and out["status"]["qr"] == "2@abc..."
    # El panel dibuja el QR con un <img> contra el bridge: el string suelto no
    # se puede mostrar. Y sin sesión no hay grupos que listar todavía.
    assert out["qr_url"].endswith("/qr.png") and out["groups"] == []


def test_sin_bridge_url_avisa_que_falta(store, tmp_path):
    (tmp_path / "config" / "settings.json").write_text(
        json.dumps({"CHANNEL": "whatsapp"}), encoding="utf-8")
    assert "WHATSAPP_BRIDGE_URL" in store.run_action("whatsapp_status")["errors"][0]


# ─── "Me gusta" = reacción ──────────────────────────────────────────────────
def test_like_post_reacciona_con_el_autor_del_mensaje():
    """La reacción se firma contra (chat, autor, id): el bridge necesita saber
    quién escribió el mensaje, no alcanza con el id."""
    b = BridgeFalso([_msg("7", text="me encantan los mapaches")])
    c = _canal(b)
    c.get_mentions()                             # el canal ve (y recuerda) el mensaje
    assert c.like_post(f"{GRUPO}/7") is True
    assert b.reacciones == [{"chat_id": GRUPO, "message_id": "7",
                             "author_id": OTRO, "emoji": "❤️"}]


def test_like_post_con_bridge_viejo_no_rompe():
    """Un bridge sin /react devuelve 404: el bot pierde el gesto, no la respuesta."""
    b = BridgeFalso([_msg("7")])
    b.request = lambda method, path, params=None, json=None, **kw: (
        _Resp(None, 404) if path == "/react"
        else BridgeFalso.request(b, method, path, params, json, **kw))
    c = _canal(b)
    assert c.like_post(f"{GRUPO}/7") is False


# ─── El handle de WhatsApp es el teléfono ───────────────────────────────────
@pytest.mark.parametrize("escrito", [
    "5491111111111",                     # pelado
    "+54 9 11 1111-1111",                # como lo tipea un humano
    "+5491111111111",
    "5491111111111@s.whatsapp.net",      # el JID, copiado del bridge
    "5491111111111@lid",                 # con modo privado del otro lado
])
def test_todas_las_formas_del_mismo_numero_son_el_mismo_handle(escrito):
    """Lo que el admin escribe en ADMIN_HANDLE/USER_GROUPS tiene que matchear
    con el author_handle que el canal saca de un mensaje, se escriba como se
    escriba. Si no, el bot se queda mudo sin decir por qué."""
    b = BridgeFalso([_msg("1", autor=OTRO, text="@bot", mentions=[YO])])
    assert ch.wa_handle(escrito) == _canal(b).get_mentions()[0]["author_handle"]


def test_lo_que_no_es_un_numero_queda_intacto():
    """`feed:x` no es una persona, y un handle de otra red que quedó de arrastre
    tiene que seguir sin matchear — normalizarlo sería inventar un match."""
    assert ch.wa_handle("feed:polcifeed") == "feed:polcifeed"
    assert ch.wa_handle("ppolci.com") == "ppolci.com"
    assert ch.wa_handle("") == "" and ch.wa_handle(None) == ""


def test_get_profile_acepta_el_numero_y_no_solo_el_jid():
    b = BridgeFalso([_msg("1", autor=OTRO, text="@bot", mentions=[YO])])
    c = _canal(b)
    c.get_mentions()
    perfil = c.get_profile("+54 9 11 1111-1111")
    assert perfil.handle == "5491111111111" and perfil.display_name == "Fulano"


# ─── Grupos direccionados por LID ───────────────────────────────────────────
# Caso real (2026-07-31): el hello world salió, pero contestarle citándolo no
# despertaba al bot. En un grupo con LID la cita viene firmada con el LID del
# bot y el canal solo conocía su número: no se reconocía como destinatario.
def test_una_cita_firmada_con_el_lid_del_bot_lo_despierta():
    b = BridgeFalso([_msg("2", text="Test para ver que respondas.",
                          quoted_id="1", quoted_author_id=YO_LID)])
    assert [m["cid"] for m in _canal(b).get_mentions()] == ["2"]


def test_una_mencion_por_lid_tambien_lo_despierta():
    b = BridgeFalso([_msg("3", text="che @bot", mentions=[YO_LID])])
    assert [m["cid"] for m in _canal(b).get_mentions()] == ["3"]


def test_el_bot_sigue_sin_contestarse_a_si_mismo_por_lid():
    """Anti-loop: sus propios mensajes le llegan con la otra identidad."""
    b = BridgeFalso([_msg("4", autor=YO_LID, text="algo mío", mentions=[YO])])
    assert _canal(b).get_mentions() == []


# ─── El contexto es la conversación, no el hilo ─────────────────────────────
# En un grupo la gente contesta sin citar: reconstruir "el hilo" devolvía casi
# siempre UNA línea (el mensaje mismo) y el bot contestaba sin saber de qué se
# venía hablando.
def _canal_con(bridge, n=20):
    return WhatsAppChannel("http://x", [GRUPO], http=bridge, context_messages=n)


def test_el_contexto_son_los_ultimos_mensajes_del_grupo():
    b = BridgeFalso([
        _msg("1", text="alguien vio el partido?"),
        _msg("2", text="un desastre el arquero"),
        _msg("3", text="che @bot qué opinás", mentions=[YO]),
    ])
    c = _canal_con(b)
    c.get_mentions()
    ctx, _, _, _ = c.get_thread_info(f"{GRUPO}/3", "3")
    assert ctx.splitlines() == ["Fulano: alguien vio el partido?",
                                "Fulano: un desastre el arquero",
                                "Fulano: che @bot qué opinás"]


def test_el_contexto_se_recorta_a_lo_configurado():
    b = BridgeFalso([_msg(str(i), text=f"mensaje {i}") for i in range(1, 8)]
                    + [_msg("8", text="@bot", mentions=[YO])])
    c = _canal_con(b, n=3)
    c.get_mentions()
    ctx, _, _, _ = c.get_thread_info(f"{GRUPO}/8", "8")
    assert len(ctx.splitlines()) == 3 and ctx.splitlines()[-1].endswith("@bot")


def test_la_cita_se_marca_en_la_linea():
    """Sin esto, en un grupo no se entiende quién le contesta a quién."""
    b = BridgeFalso([
        _msg("1", text="yo digo que sí"),
        _msg("2", text="no estoy de acuerdo", quoted_id="1"),
        _msg("3", text="@bot desempatá", mentions=[YO]),
    ])
    c = _canal_con(b)
    c.get_mentions()
    ctx, _, _, _ = c.get_thread_info(f"{GRUPO}/3", "3")
    assert "Fulano [le contesta a Fulano: «yo digo que sí»]: no estoy de acuerdo" in ctx


def test_el_mensaje_citado_entra_aunque_haya_quedado_fuera_de_la_ventana():
    """Lo más específico que hay sobre qué le están contestando al bot no se
    puede perder por el recorte."""
    b = BridgeFalso([_msg("1", text="la pregunta original")]
                    + [_msg(str(i), text=f"ruido {i}") for i in range(2, 7)]
                    + [_msg("7", text="@bot mirá esto", mentions=[YO], quoted_id="1")])
    c = _canal_con(b, n=2)
    c.get_mentions()
    ctx, _, _, _ = c.get_thread_info(f"{GRUPO}/7", "7")
    assert "la pregunta original" in ctx


def test_los_chats_ajenos_no_se_guardan_ni_en_memoria():
    """El número recibe TODO, incluidas las conversaciones personales del
    dueño: no tienen por qué quedar en la RAM del bot."""
    b = BridgeFalso([
        _msg("1", chat=PRIVADO, text="algo privado del dueño"),
        _msg("2", text="@bot", mentions=[YO]),
    ])
    c = _canal_con(b)
    c.get_mentions()
    assert PRIVADO not in c._recientes and "1" not in c._msgs
    ctx, _, _, _ = c.get_thread_info(f"{GRUPO}/2", "2")
    assert "privado" not in ctx


def test_el_bot_se_reconoce_por_su_nombre_en_el_contexto():
    b = BridgeFalso([
        _msg("1", autor=YO, from_me=True, text="lo que dije antes"),
        _msg("2", text="@bot y ahora?", mentions=[YO]),
    ])
    c = _canal_con(b)
    c.get_mentions()
    ctx, _, _, _ = c.get_thread_info(f"{GRUPO}/2", "2")
    assert ctx.splitlines()[0] == "Botata: lo que dije antes"


# ─── Arrobado a mano ────────────────────────────────────────────────────────
@pytest.mark.parametrize("texto, contesta", [
    ("@Botata qué opinás", True),          # tipeado a mano, sin elegir del listado
    ("@botata en minúscula", True),
    ("@5491100000000 por número", True),
    ("che botata qué opinás", False),      # nombrarlo no es arrobarlo
    ("mandale un mail a juan@botata.com", False),
])
def test_arrobarlo_a_mano_tambien_cuenta(texto, contesta):
    """WhatsApp solo crea una mención si elegís al bot del listado del `@`.
    Escrito a mano no genera nada y se ve idéntico: el bot quedaba mudo sin
    motivo visible."""
    b = BridgeFalso([_msg("1", text=texto)])
    assert bool(_canal(b).get_mentions()) is contesta


# ─── Pase de feed: la conversación ES el feed ───────────────────────────────
# No hay timeline, pero la idea del pase vale igual: leer cada tanto de qué se
# habla y aprender de la gente (decisión del admin, 2026-07-31).
def _con_charla(n=20):
    b = BridgeFalso([
        _msg("1", text="hoy juega Boca"),
        _msg("2", autor=YO, from_me=True, text="lo que dije yo"),
        _msg("3", text="a las 9", quoted_id="1"),
    ])
    c = _canal_con(b, n)
    c.get_mentions()
    return c


def test_el_feed_devuelve_la_conversacion_en_la_forma_del_pase():
    posts = _con_charla().get_feed_posts("chat", GRUPO)
    assert [p["text"] for p in posts] == ["hoy juega Boca", "a las 9"]
    p = posts[0]
    # El teléfono, igual que en las menciones: si acá fuera el nombre visible,
    # el bot aprendería de "Fulano" y le contestaría a un número — dos personas
    # distintas para su memoria.
    assert p["handle"] == "5491111111111"
    assert p["uri"] == f"{GRUPO}/1" and p["reply_to"] is None
    assert posts[1]["reply_to"] == f"{GRUPO}/1"


def test_el_feed_no_le_devuelve_al_bot_lo_que_dijo_el_bot():
    assert all("lo que dije yo" not in p["text"] for p in
               _con_charla().get_feed_posts("chat", GRUPO))


def test_el_feed_respeta_el_since():
    from datetime import datetime, timedelta, timezone
    futuro = datetime.now(timezone.utc) + timedelta(days=1)
    assert _con_charla().get_feed_posts("chat", GRUPO, since=futuro) == []


def test_el_feed_sin_fuente_usa_el_chat_principal():
    assert _con_charla().get_feed_posts("chat", None)


def test_el_feed_no_lee_chats_fuera_del_allowlist():
    """Mismo criterio que el poll: si no está en WHATSAPP_CHAT_IDS, no se lee —
    ni aunque una fuente mal configurada lo pida por nombre."""
    assert _con_charla().get_feed_posts("chat", PRIVADO) == []


def test_un_tipo_de_fuente_de_otra_red_no_rompe():
    assert _con_charla().get_feed_posts("list", "at://loquesea") == []


# ─── Lo que NO aplica en WhatsApp ───────────────────────────────────────────
def test_bloquear_no_aplica_y_lo_dice():
    """En un grupo bloquear no saca a nadie: el pedido tiene que fallar, no
    fingir que pasó algo (decisión del admin, 2026-07-31)."""
    assert _canal(BridgeFalso()).block_user("5491111111111") is False


# ─── Vision: citar una foto y preguntar por ella ────────────────────────────
# Caso real (2026-07-31): se le citó un mensaje con imagen pidiéndole que la
# describa y contestó que no puede ver imágenes. La imagen no estaba en el
# mensaje que lo menciona (solo texto) sino en el CITADO, y solo se describía
# la hoja.
def _con_foto():
    b = BridgeFalso([
        _msg("1", text="miren esto", media=[{"path": "/tmp/foto.jpg", "mime": "image/jpeg"}]),
        _msg("2", text="@bot describime la imagen", mentions=[YO], quoted_id="1"),
    ])
    c = _canal_con(b)
    c.set_media_describer(lambda fuente: f"un mapache lavando una galletita ({fuente})")
    c.get_mentions()
    return c


def test_describe_la_imagen_del_mensaje_citado():
    _, _, _, media = _con_foto().get_thread_info(f"{GRUPO}/2", "2")
    assert "mapache lavando una galletita" in media and "lo que cita" in media


def test_la_imagen_de_la_ventana_se_anota_pero_no_se_mira():
    """Costo acotado: vision solo sobre el mensaje que se contesta y el que
    cita. El resto de la conversación lleva la anotación barata."""
    c = _canal_con(BridgeFalso([
        _msg("1", text="foto vieja", media=[{"path": "/tmp/v.jpg", "mime": "image/jpeg"}]),
        _msg("2", text="@bot hola", mentions=[YO]),
    ]))
    mirados = []
    c.set_media_describer(lambda f: mirados.append(f) or "descripción")
    c.get_mentions()
    ctx, _, _, _ = c.get_thread_info(f"{GRUPO}/2", "2")
    assert "[image]" in ctx and mirados == []


def test_el_perfil_trae_did_aunque_whatsapp_no_tenga_ese_concepto():
    """El motor cachea `did` al conocer a alguien. Sin el campo, reventaba con
    AttributeError en cada mensaje de esa persona: el except lo tragaba, la fila
    quedaba incompleta y el intento se repetía para siempre."""
    b = BridgeFalso([_msg("1", text="@bot", mentions=[YO])])
    c = _canal(b)
    c.get_mentions()
    perfil = c.get_profile("5491111111111")
    assert perfil.did == "5491111111111" and perfil.display_name == "Fulano"


def test_arrobarlo_le_llega_con_su_nombre_y_no_un_numero_crudo():
    """Caso real (2026-07-31): arrobarlo dejaba en el texto el LID con el que
    el grupo lo direcciona, y el bot leía «@104913921159305 sobre esto qué
    opinás?» sin reconocer que ese número es él."""
    b = BridgeFalso([_msg("1", text=f"@{YO_LID.split('@')[0]} sobre esto qué opinás?")])
    m = _canal(b).get_mentions()[0]
    assert m["text"] == "@Botata sobre esto qué opinás?"


# ─── barrida 2026-08-03: naranjas de WhatsApp ───────────────────────────────

def test_wa_handle_descarta_el_sufijo_de_device():
    """Regresión: whatsmeow puede mandar JIDs con device (`...:12@s.whatsapp.net`)
    y el ":" hacía que el JID entero volviera crudo — no matcheaba ADMIN_HANDLE
    ni USER_GROUPS."""
    from channels import wa_handle
    assert wa_handle("5491111111111:12@s.whatsapp.net") == "5491111111111"
    assert wa_handle("5491111111111@s.whatsapp.net") == "5491111111111"
    assert wa_handle("feed:algo") == "feed:algo"     # las refs siguen intactas


class BridgeNombreConEspacio(BridgeFalso):
    def request(self, method, path, params=None, json=None, **kw):
        if path == "/status":
            return _Resp({"connected": True, "qr": None,
                          "me": {"id": YO, "lid": YO_LID, "name": "Botata Rancher"}})
        return super().request(method, path, params, json, **kw)


def test_nombre_con_espacios_no_ensucia_los_comandos():
    """Regresión: con display name "Botata Rancher", la mención se reescribía
    como "@Botata Rancher /stop" y el parser de comandos comía solo "@Botata" —
    el resto del nombre ensuciaba el comando y /stop quedaba mudo."""
    lid = YO_LID.split("@")[0]
    bridge = BridgeNombreConEspacio([_msg("1", text=f"@{lid} /stop",
                                          mentions=[YO_LID])])
    canal = _canal(bridge)
    menciones = canal.get_mentions()
    assert menciones and menciones[0]["text"] == "@BotataRancher /stop"


def test_nombre_con_espacios_igual_lo_reconoce_arrobado_a_mano():
    bridge = BridgeNombreConEspacio([
        _msg("1", text="@botata rancher qué onda"),
        _msg("2", text="@botatarancher qué onda"),
    ])
    canal = _canal(bridge)
    assert len(canal.get_mentions()) == 2


class BridgeSinId(BridgeFalso):
    def request(self, method, path, params=None, json=None, **kw):
        if path == "/send":
            return _Resp({})                 # respuesta sin id
        return super().request(method, path, params, json, **kw)


def test_send_sin_id_explota_en_vez_de_envenenar_la_db():
    """Regresión: sin id la URI quedaba "{chat}/" — key degenerada en
    bot_posts/replied_posts que después rompía dedup y refetch."""
    canal = _canal(BridgeSinId())
    with pytest.raises(RuntimeError, match="sin id"):
        canal.post("hola")


def test_el_cache_de_mensajes_tiene_techo():
    """Regresión: _msgs crecía sin tope — leak lento en un proceso de semanas."""
    canal = _canal(BridgeFalso())
    canal.MSGS_MAX = 10
    for i in range(30):
        canal._guardar_msg(_msg(str(i)))
    assert len(canal._msgs) == 10
    assert "29" in canal._msgs and "0" not in canal._msgs  # quedan los últimos
