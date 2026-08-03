"""Tests de la generación de imágenes: el transporte (router) y la tool.

El modelo y el endpoint salen del router por el rol `image_generate`, así que
hereda la cadena de fallback de todo lo demás. Conviven dos formas de API porque
los proveedores no se pusieron de acuerdo: `chat` (chat/completions con
`modalities`, lo que habla OpenRouter) e `images` (`/v1/images/generations`, lo
que hablan OpenAI, xAI/Grok y los servidores locales). Nada acá toca la red.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import router as rmod  # noqa: E402
from router import ModelRouter  # noqa: E402

_PNG = b"\x89PNG\r\n\x1a\n_pixeles_"
_B64 = base64.b64encode(_PNG).decode()


# ─── Fakes del SDK de OpenAI ──────────────────────────────────────────────────
class _Msg:
    def __init__(self, images):
        self.content = ""
        self.images = images


class _Choice:
    def __init__(self, msg):
        self.message = msg


class _Resp:
    def __init__(self, msg):
        self.choices = [_Choice(msg)]


class _Dato:
    def __init__(self, b64=None, url=None):
        self.b64_json = b64
        self.url = url


class _RespImgs:
    def __init__(self, dato):
        self.data = [dato]


class _Chat:
    """El sub-cliente `client.chat.completions`."""
    def __init__(self, dueño):
        self.completions = self
        self._d = dueño

    def create(self, **kw):
        self._d.visto.append(kw)
        return _Resp(_Msg(self._d.imgs_chat))


class _Imgs:
    """El sub-cliente `client.images`."""
    def __init__(self, dueño):
        self._d = dueño

    def generate(self, **kw):
        self._d.visto.append(kw)
        if self._d.rechaza_response_format and "response_format" in kw:
            raise TypeError("Unknown parameter: 'response_format'")
        return _RespImgs(self._d.dato)


class ClienteFalso:
    """Imita al cliente de OpenAI en las dos formas: `.chat.completions.create`
    (devuelve `imgs_chat` en el mensaje) y `.images.generate` (devuelve `dato`)."""

    def __init__(self, *, imgs_chat=None, dato=None, rechaza_response_format=False):
        self.imgs_chat = imgs_chat
        self.dato = dato
        self.rechaza_response_format = rechaza_response_format
        self.visto: list[dict] = []
        self.chat = _Chat(self)
        self.images = _Imgs(self)


def _router(cliente, *, api=None) -> ModelRouter:
    hop = {"endpoint": "e", "model": "m"}
    if api:
        hop["api"] = api
    r = ModelRouter({"e": {"base_url": "http://x/v1", "api_key": "k"}},
                    {"image_gen": [hop]}, {"image_generate": "image_gen"},
                    max_retries=1, backoff_base=0)
    r._clients["e"] = cliente
    return r


# ─── Forma `chat` (OpenRouter) ───────────────────────────────────────────────
def test_chat_devuelve_bytes_desde_data_url():
    c = ClienteFalso(imgs_chat=[{"type": "image_url",
                                 "image_url": {"url": f"data:image/png;base64,{_B64}"}}])
    assert _router(c).generate_image("image_generate", "un mapache") == _PNG
    assert c.visto[0]["extra_body"] == {"modalities": ["image", "text"]}
    assert c.visto[0]["messages"][0]["content"] == "un mapache"


def test_chat_acepta_b64_pelado():
    c = ClienteFalso(imgs_chat=[{"b64_json": _B64}])
    assert _router(c).generate_image("image_generate", "x") == _PNG


def test_chat_sin_imagenes_es_error():
    with pytest.raises(RuntimeError):
        _router(ClienteFalso(imgs_chat=[])).generate_image("image_generate", "x")


def test_url_http_se_baja(monkeypatch):
    c = ClienteFalso(imgs_chat=[{"image_url": {"url": "https://cdn.x/y.png"}}])

    class _R:
        def read(self, n=None):
            return _PNG
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(rmod.urllib.request, "urlopen", lambda u, timeout=0: _R())
    assert _router(c).generate_image("image_generate", "x") == _PNG


# ─── Forma `images` (OpenAI, xAI/Grok, local) ────────────────────────────────
def test_images_pide_b64_y_pasa_size():
    c = ClienteFalso(dato=_Dato(b64=_B64))
    out = _router(c, api="images").generate_image("image_generate", "un mapache", size="1024x1024")
    assert out == _PNG
    assert c.visto[0]["response_format"] == "b64_json"
    assert c.visto[0]["size"] == "1024x1024"
    assert c.visto[0]["prompt"] == "un mapache"


def test_images_reintenta_sin_response_format():
    """gpt-image-1 rechaza response_format; xAI lo exige. Se pide, y si molesta
    se vuelve a pedir sin él en vez de dar la generación por perdida."""
    c = ClienteFalso(dato=_Dato(b64=_B64), rechaza_response_format=True)
    assert _router(c, api="images").generate_image("image_generate", "x") == _PNG
    assert len(c.visto) == 2 and "response_format" not in c.visto[1]


def test_images_error_real_no_se_traga():
    """Un 402 no es 'no acepta response_format': se propaga por la cadena de
    fallback (envuelto por el router) con la causa real colgada."""
    c = ClienteFalso(dato=_Dato(b64=_B64))
    llamadas = []
    def _boom(**kw):
        llamadas.append(kw)
        raise RuntimeError("402 sin crédito")
    c.images.generate = _boom
    with pytest.raises(RuntimeError, match="agotaron") as exc:
        _router(c, api="images").generate_image("image_generate", "x")
    assert "crédito" in str(exc.value.__cause__)
    assert len(llamadas) == 1         # NO reintentó sin response_format


# ─── El rol tiene que estar configurado de verdad ────────────────────────────
def test_tiene_rol():
    c = ClienteFalso()
    r = _router(c)
    assert r.tiene_rol("image_generate")
    assert not r.tiene_rol("rol_inexistente")


def test_sin_alias_no_generas_con_un_modelo_de_texto():
    """`_chain` es indulgente a propósito (un rol nuevo no rompe instancias
    viejas), pero caer al alias de texto acá sería pedirle un PNG a un LLM."""
    r = ModelRouter({"e": {"base_url": "http://x/v1", "api_key": "k"}},
                    {"lite": [{"endpoint": "e", "model": "m"}]}, {"reply": "lite"})
    assert not r.tiene_rol("image_generate")
    with pytest.raises(KeyError):
        r.generate_image("image_generate", "x")


# ─── La tool ─────────────────────────────────────────────────────────────────
import botata as b  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

_CTX = ToolContext(state={}, conn=None)


class RouterFalso:
    def __init__(self, blob=_PNG, error=None, rol=True):
        self.blob, self.error, self.rol = blob, error, rol
        self.pedidos: list[tuple] = []

    def tiene_rol(self, role):
        return self.rol

    def generate_image(self, role, prompt, *, size=None):
        self.pedidos.append((role, prompt, size))
        if self.error:
            raise self.error
        return self.blob


@pytest.fixture
def dir_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "_IMG_DIR", tmp_path / "generated")
    return tmp_path / "generated"


def test_genera_y_adjunta(dir_tmp):
    r = RouterFalso()
    out = b._make_generate_image_tool(r)({"prompt": "a raccoon lawyer"}, _CTX)
    assert out.image_path and Path(out.image_path).read_bytes() == _PNG
    assert Path(out.image_path).parent == dir_tmp
    assert r.pedidos[0][:2] == ("image_generate", "a raccoon lawyer")


def test_prompt_vacio(dir_tmp):
    out = b._make_generate_image_tool(RouterFalso())({"prompt": "  "}, _CTX)
    assert "qué dibujar" in out.text and out.image_path is None


def test_sin_router_configurado(dir_tmp):
    for r in (None, RouterFalso(rol=False)):
        out = b._make_generate_image_tool(r)({"prompt": "x"}, _CTX)
        assert "no tengo generación de imágenes" in out.text
        assert out.image_path is None


def test_tope_diario(dir_tmp, monkeypatch):
    """En un grupo público la tool está al alcance de cualquiera que escriba
    'dibujame otra', y cada imagen se paga."""
    monkeypatch.setattr(b, "settings", {**b.settings, "IMAGE_GEN": {"max_per_day": 2}})
    r = RouterFalso()
    tool = b._make_generate_image_tool(r)
    assert tool({"prompt": "una"}, _CTX).image_path
    assert tool({"prompt": "dos"}, _CTX).image_path
    tercera = tool({"prompt": "tres"}, _CTX)
    assert tercera.image_path is None and "para hoy" in tercera.text
    assert len(r.pedidos) == 2          # ni siquiera se llamó al proveedor


def test_tope_por_hilo(dir_tmp, monkeypatch, tmp_path):
    """Caso real (2026-08-02): en un hilo donde las imágenes eran el tema, el bot
    generó un retrato del admin como respuesta a "escribile una carta a
    panchitos", a "el romance no murió" y al chiste de que subía retratos sin
    parar. Pedírselo por prompt ya falló: acá no puede."""
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "hilo.db")
    monkeypatch.setattr(b, "settings", {**b.settings, "IMAGE_GEN": {"max_per_thread": 2}})
    r = RouterFalso()
    tool = b._make_generate_image_tool(r, None)
    ctx = ToolContext(state=dict(_ESTADO), conn=conn)
    assert tool({"prompt": "una"}, ctx).image_path
    assert tool({"prompt": "dos"}, ctx).image_path
    tercera = tool({"prompt": "tres"}, ctx)
    assert tercera.image_path is None and "en esta charla" in tercera.text
    assert len(r.pedidos) == 2               # no se llamó al proveedor
    # otro hilo arranca de cero
    otro = ToolContext(state={**_ESTADO, "thread_root_uri": "uri://otra-charla"}, conn=conn)
    assert tool({"prompt": "cuatro"}, otro).image_path


def test_tope_por_hilo_cero_lo_desactiva(dir_tmp, monkeypatch, tmp_path):
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "hilo0.db")
    monkeypatch.setattr(b, "settings", {**b.settings, "IMAGE_GEN": {"max_per_thread": 0}})
    tool = b._make_generate_image_tool(RouterFalso(), None)
    ctx = ToolContext(state=dict(_ESTADO), conn=conn)
    for i in range(4):
        assert tool({"prompt": f"n{i}"}, ctx).image_path


def test_tope_cero_es_sin_tope(dir_tmp, monkeypatch):
    monkeypatch.setattr(b, "settings", {**b.settings, "IMAGE_GEN": {"max_per_day": 0}})
    tool = b._make_generate_image_tool(RouterFalso())
    for i in range(3):
        assert tool({"prompt": f"n{i}"}, _CTX).image_path


def test_size_de_la_config_llega_al_router(dir_tmp, monkeypatch):
    monkeypatch.setattr(b, "settings", {**b.settings, "IMAGE_GEN": {"size": "512x512"}})
    r = RouterFalso()
    b._make_generate_image_tool(r)({"prompt": "x"}, _CTX)
    assert r.pedidos[0][2] == "512x512"


def test_filtro_del_proveedor_se_distingue_de_una_caida(dir_tmp):
    censura = b._make_generate_image_tool(
        RouterFalso(error=RuntimeError("Request blocked by safety filters")))
    assert "rechazó ese pedido" in censura({"prompt": "x"}, _CTX).text
    caida = b._make_generate_image_tool(RouterFalso(error=RuntimeError("connection reset")))
    assert "no pude generar" in caida({"prompt": "x"}, _CTX).text


# ─── Aviso previo: generar tarda ~7-10s y el otro no ve nada mientras tanto ──
class CanalFalso:
    def __init__(self, rompe=False):
        self.enviados: list[dict] = []
        self.rompe = rompe

    def reply(self, **kw):
        if self.rompe:
            raise RuntimeError("el canal se cayó")
        self.enviados.append(kw)
        return "uri://aviso"


_ESTADO = {"mention_uri": "uri://mencion", "mention_cid": "cid1",
           "thread_root_uri": "uri://raiz", "thread_root_cid": "cid0"}


def _ctx_conversacion(conn=None):
    return ToolContext(state=dict(_ESTADO), conn=conn)


@pytest.fixture
def con_aviso(monkeypatch):
    monkeypatch.setattr(b, "settings",
                        {**b.settings, "IMAGE_GEN": {"aviso": ["ahí va, la estoy dibujando"]}})


def test_avisa_antes_de_generar(dir_tmp, con_aviso):
    canal, r = CanalFalso(), RouterFalso()
    out = b._make_generate_image_tool(r, canal)({"prompt": "x"}, _ctx_conversacion())
    assert len(canal.enviados) == 1
    env = canal.enviados[0]
    assert env["text"] == "ahí va, la estoy dibujando"
    assert env["parent_uri"] == "uri://mencion" and env["root_uri"] == "uri://raiz"
    assert env.get("media_path") is None          # el aviso va sin imagen
    assert out.image_path                         # y la imagen igual salió


def test_el_aviso_se_le_cuenta_a_la_fase_de_escritura(dir_tmp, con_aviso):
    """Caso real (2026-08-02): con la nota puesta como "no repitas el aviso"
    contestó 'generando, esta vez bien te juro' CON la imagen ya adjunta. Hay
    que decirle qué sí hacer, no solo qué evitar."""
    out = b._make_generate_image_tool(RouterFalso(), CanalFalso())(
        {"prompt": "x"}, _ctx_conversacion())
    assert "YA está adjunta" in out.text and "presentala" in out.text
    sin_aviso = b._make_generate_image_tool(RouterFalso(), None)(
        {"prompt": "x"}, _ctx_conversacion())
    assert "adjunta" not in sin_aviso.text


def test_el_aviso_queda_en_la_historia_del_bot(dir_tmp, con_aviso, tmp_path):
    """Es un post del bot como cualquier otro: si no se registra,
    `get_my_recent_posts` le miente sobre lo que acaba de decir."""
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "hist.db")
    conn.execute("INSERT INTO users (handle) VALUES ('ppolci.com')")  # FK de bot_posts
    ctx = ToolContext(state={**_ESTADO, "author_handle": "ppolci.com"}, conn=conn)
    b._make_generate_image_tool(RouterFalso(), CanalFalso())({"prompt": "x"}, ctx)
    fila = conn.execute("SELECT uri, in_reply_to, reply_to_handle, text "
                        "FROM bot_posts ORDER BY rowid DESC LIMIT 1").fetchone()
    assert fila["text"] == "ahí va, la estoy dibujando"
    assert fila["uri"] == "uri://aviso" and fila["in_reply_to"] == "uri://mencion"
    assert fila["reply_to_handle"] == "ppolci.com"


def test_si_falla_el_registro_la_imagen_sale_igual(dir_tmp, con_aviso, tmp_path):
    """bot_posts tiene FK a users: si el autor todavía no está, el INSERT
    revienta. El aviso YA se mandó — llevar el registro no puede voltear la
    respuesta."""
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "sinuser.db")     # sin la fila de users
    ctx = ToolContext(state={**_ESTADO, "author_handle": "fantasma.com"}, conn=conn)
    canal = CanalFalso()
    out = b._make_generate_image_tool(RouterFalso(), canal)({"prompt": "x"}, ctx)
    assert len(canal.enviados) == 1        # el aviso salió
    assert out.image_path                  # y la imagen también


def test_sin_configurar_no_manda_nada(dir_tmp, monkeypatch):
    """Opt-in: una instancia que no lo configuró no empieza a postear de a dos."""
    monkeypatch.setattr(b, "settings", {**b.settings, "IMAGE_GEN": {}})
    canal = CanalFalso()
    b._make_generate_image_tool(RouterFalso(), canal)({"prompt": "x"}, _ctx_conversacion())
    assert canal.enviados == []


def test_una_rutina_no_avisa(dir_tmp, con_aviso):
    """Sin mensaje al que contestar no hay nadie esperando la imagen."""
    canal = CanalFalso()
    b._make_generate_image_tool(RouterFalso(), canal)({"prompt": "x"},
                                                      ToolContext(state={}, conn=None))
    assert canal.enviados == []


def test_si_el_aviso_falla_la_imagen_sale_igual(dir_tmp, con_aviso):
    out = b._make_generate_image_tool(RouterFalso(), CanalFalso(rompe=True))(
        {"prompt": "x"}, _ctx_conversacion())
    assert out.image_path and "no lo repitas" not in out.text


def test_no_avisa_dos_veces_en_un_reintento(dir_tmp, con_aviso, tmp_path):
    """retry_stuck_mentions puede reprocesar la misma mención: el aviso no se
    duplica (mismo dedup por kv que like_post)."""
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "t.db")
    canal = CanalFalso()
    tool = b._make_generate_image_tool(RouterFalso(), canal)
    tool({"prompt": "x"}, _ctx_conversacion(conn))
    tool({"prompt": "x"}, _ctx_conversacion(conn))
    assert len(canal.enviados) == 1


def test_no_avisa_si_va_a_rebotar_por_el_tope(dir_tmp, monkeypatch):
    """Avisar 'ahí va' y después decir 'hoy no' es peor que callarse."""
    monkeypatch.setattr(b, "settings", {**b.settings,
                                        "IMAGE_GEN": {"aviso": ["ahí va"], "max_per_day": 1}})
    monkeypatch.setattr(b, "_generadas_hoy", lambda: 99)
    canal = CanalFalso()
    out = b._make_generate_image_tool(RouterFalso(), canal)({"prompt": "x"},
                                                            _ctx_conversacion())
    assert "para hoy" in out.text and canal.enviados == []


def test_tampoco_avisa_sin_modelo_configurado(dir_tmp, con_aviso):
    canal = CanalFalso()
    b._make_generate_image_tool(RouterFalso(rol=False), canal)({"prompt": "x"},
                                                               _ctx_conversacion())
    assert canal.enviados == []


# ─── Tope de peso: Bluesky rechaza blobs > 1 MB ──────────────────────────────
def test_abajo_del_tope_no_se_toca():
    assert b._achicar_imagen(b"x" * 100, 1000) == b"x" * 100


def test_sin_pillow_no_inventa(monkeypatch):
    """Pillow es opcional (no es dependencia del motor): sin ella no se
    recomprime nada y la tool avisa, en vez de mandar algo que el canal rechaza."""
    monkeypatch.setitem(sys.modules, "PIL", None)   # import PIL → ImportError
    assert b._achicar_imagen(b"x" * 5000, 1000) is None


def test_la_tool_avisa_si_no_puede_achicar(dir_tmp, monkeypatch):
    monkeypatch.setattr(b, "settings", {**b.settings, "IMAGE_GEN": {"max_bytes": 10}})
    monkeypatch.setattr(b, "_achicar_imagen", lambda blob, tope: None)
    out = b._make_generate_image_tool(RouterFalso())({"prompt": "x"}, _CTX)
    assert "demasiado pesada" in out.text and out.image_path is None


def test_recomprimida_se_guarda_como_jpg(dir_tmp, monkeypatch):
    jpeg = b"\xff\xd8\xff" + b"chico"
    monkeypatch.setattr(b, "settings", {**b.settings, "IMAGE_GEN": {"max_bytes": 100}})
    monkeypatch.setattr(b, "_achicar_imagen", lambda blob, tope: jpeg)
    out = b._make_generate_image_tool(RouterFalso())({"prompt": "x"}, _CTX)
    assert out.image_path.endswith(".jpg")          # la extensión sigue al contenido
    assert Path(out.image_path).read_bytes() == jpeg


def test_baja_calidad_hasta_entrar(monkeypatch):
    """La escalera es calidad y recién después tamaño: se prueba q88/75/60 antes
    de resignar píxeles."""
    intentos = []

    class _Buf(io.BytesIO):
        pass

    class _ImgFalsa:
        width = height = 1000
        def convert(self, modo):
            return self
        def resize(self, wh):
            nueva = _ImgFalsa()
            nueva.width, nueva.height = wh
            return nueva
        def save(self, buf, format=None, quality=None, optimize=None):
            intentos.append((quality, self.width))
            buf.write(b"\xff\xd8\xff" + b"z" * (quality * 10))

    fake_pil = types.ModuleType("PIL")
    fake_pil.Image = types.SimpleNamespace(open=lambda _: _ImgFalsa())
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_pil.Image)

    out = b._achicar_imagen(b"P" * 99_999, 700)
    assert out.startswith(b"\xff\xd8\xff")
    assert intentos[0][0] == 88 and intentos[-1][0] == 60   # bajó calidad
    assert all(w == 1000 for _, w in intentos)              # sin resignar tamaño


def test_nace_solo_admin():
    """Cada imagen se paga y el bot es público: ampliar a `reply` es opt-in de
    la instancia (settings → TOOLS.generate_image.scopes), como las de MCP."""
    reg = b.build_tool_registry({})
    tool = reg.get("generate_image")
    assert tool is not None and tool.scopes == frozenset({"admin"})


def test_la_instancia_puede_ampliar_el_scope():
    reg = b.build_tool_registry(
        {"generate_image": {"enabled": True, "scopes": ["reply", "feed_reflection", "admin"]}})
    assert "generate_image" in [t.name for t in reg.available(Scope.REPLY)]
