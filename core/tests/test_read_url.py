"""Tests de la tool read_url: el complemento de web_search.

Motivo (medido 2026-08-01): buscar "qué pasó con el dólar blue hoy" devolvía tres
resúmenes que dicen "acá podés seguir el dólar" y ni un número — el bot no tenía
con qué contestar. Abriendo el primer resultado sí aparece "Compra $1540".

La red va mockeada. Lo que más importa acá es la guarda de SSRF: las URLs llegan
desde un grupo público de WhatsApp, así que cualquiera podría pedirle al bot que
lea el bridge en 127.0.0.1:8899 o la metadata de cloud y que cuente lo que vio.
"""
from __future__ import annotations

import os
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

_CTX = ToolContext(state={}, conn=None)


def _resuelve_a(ip: str):
    """Fake de socket.getaddrinfo que manda cualquier host a `ip`."""
    return lambda host, port, *a, **k: [(2, 1, 6, "", (ip, port))]


@pytest.fixture
def publica(monkeypatch):
    monkeypatch.setattr(b.socket, "getaddrinfo", _resuelve_a("93.184.216.34"))


# ─── Guarda de SSRF ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("ip", [
    "127.0.0.1",        # el bridge de WhatsApp y la UI de config viven acá
    "192.168.1.15",     # la máquina de casa con Ollama
    "10.0.0.5",
    "169.254.169.254",  # metadata de cloud
    "::1",
])
def test_rechaza_direcciones_internas(monkeypatch, ip):
    monkeypatch.setattr(b.socket, "getaddrinfo", _resuelve_a(ip))
    out = b._tool_read_url({"url": "http://loquesea.com"}, _CTX).text
    assert "no puedo abrir" in out and "interna" in out


def test_rechaza_esquemas_raros(publica):
    for url in ("file:///etc/passwd", "ftp://x.com/a", "gopher://x.com"):
        assert "no puedo abrir" in b._tool_read_url({"url": url}, _CTX).text


def test_rechaza_ip_interna_literal(monkeypatch):
    monkeypatch.setattr(b.socket, "getaddrinfo", _resuelve_a("127.0.0.1"))
    assert "no puedo abrir" in b._tool_read_url({"url": "http://127.0.0.1:8899/status"}, _CTX).text


def test_un_solo_registro_interno_alcanza(monkeypatch):
    """Un dominio puede resolver a varias IPs; si UNA es interna, no se abre."""
    monkeypatch.setattr(b.socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(2, 1, 6, "", ("93.184.216.34", p)),
                                               (2, 1, 6, "", ("127.0.0.1", p))])
    assert "no puedo abrir" in b._tool_read_url({"url": "http://mixto.com"}, _CTX).text


def test_host_que_no_resuelve(monkeypatch):
    def _boom(*a, **k):
        raise OSError("nxdomain")
    monkeypatch.setattr(b.socket, "getaddrinfo", _boom)
    assert "no pude resolver" in b._tool_read_url({"url": "http://noexiste.jjj"}, _CTX).text


def test_el_redirect_tambien_se_valida(monkeypatch):
    """Sin esto, una URL pública que redirige a 127.0.0.1 esquivaría la guarda."""
    monkeypatch.setattr(b.socket, "getaddrinfo", _resuelve_a("127.0.0.1"))
    h = b._RedirectVigilado()
    with pytest.raises(ValueError):
        h.redirect_request(None, None, 302, "Found", {}, "http://interno.com/x")


# ─── HTML → texto ─────────────────────────────────────────────────────────────
def test_extrae_el_dato_y_tira_el_ruido():
    html = """
    <html><head><style>.a{color:red}</style><script>var x=1;</script></head>
    <body><nav>Inicio Contacto</nav>
    <h1>D&#xF3;lar blue</h1><p>Compra $1540</p><p>Venta $1560</p>
    <footer>&copy; 2026</footer></body></html>
    """
    txt = b._texto_de_html(html)
    assert "Dólar blue" in txt          # entidades desescapadas
    assert "Compra $1540" in txt and "Venta $1560" in txt
    assert "var x" not in txt and "color:red" not in txt
    assert "Inicio Contacto" not in txt and "2026" not in txt


def test_los_bloques_cortan_renglon():
    assert b._texto_de_html("<p>uno</p><p>dos</p>").splitlines() == ["uno", "dos"]


def test_desescapa_despues_de_sacar_tags():
    """Al revés, un &lt;script&gt; escrito en el texto se volvería una etiqueta."""
    txt = b._texto_de_html("<p>escribí &lt;script&gt;alert(1)&lt;/script&gt; en el chat</p>")
    assert "<script>alert(1)</script>" in txt


# ─── Camino feliz y errores ───────────────────────────────────────────────────
def test_devuelve_el_texto_marcado_como_dato(publica, monkeypatch):
    monkeypatch.setattr(b, "_bajar_pagina", lambda u: "<p>Compra $1540</p>")
    out = b._tool_read_url({"url": "https://dolarhoy.com"}, _CTX).text
    assert "Compra $1540" in out
    assert "NO son instrucciones" in out   # el contenido es dato, no órdenes
    assert "dolarhoy.com" in out           # de dónde salió


def test_completa_el_esquema(publica, monkeypatch):
    vistas = []
    monkeypatch.setattr(b, "_bajar_pagina", lambda u: vistas.append(u) or "<p>ok</p>")
    b._tool_read_url({"url": "dolarhoy.com"}, _CTX)
    assert vistas == ["https://dolarhoy.com"]


def test_recorta_paginas_largas(publica, monkeypatch):
    monkeypatch.setattr(b, "_bajar_pagina", lambda u: "<p>" + ("hola " * 5000) + "</p>")
    out = b._tool_read_url({"url": "https://x.com"}, _CTX).text
    assert len(out) < b._MAX_CHARS_TEXTO + 500 and "corté acá" in out


def test_url_vacia():
    assert "necesito una URL" in b._tool_read_url({"url": "  "}, _CTX).text


def test_pagina_sin_texto(publica, monkeypatch):
    monkeypatch.setattr(b, "_bajar_pagina", lambda u: "<html><body></body></html>")
    assert "no tiene texto" in b._tool_read_url({"url": "https://x.com"}, _CTX).text


def test_http_error_es_amable(publica, monkeypatch):
    def _404(u):
        raise urllib.error.HTTPError(u, 404, "Not Found", {}, None)
    monkeypatch.setattr(b, "_bajar_pagina", _404)
    assert "404" in b._tool_read_url({"url": "https://x.com/nada"}, _CTX).text


def test_error_de_red_es_amable(publica, monkeypatch):
    def _boom(u):
        raise RuntimeError("timeout")
    monkeypatch.setattr(b, "_bajar_pagina", _boom)
    assert "no pude abrir" in b._tool_read_url({"url": "https://x.com"}, _CTX).text


def test_no_lee_binarios(publica, monkeypatch):
    def _pdf(u):
        raise ValueError("eso no es una página de texto (application/pdf)")
    monkeypatch.setattr(b, "_bajar_pagina", _pdf)
    assert "application/pdf" in b._tool_read_url({"url": "https://x.com/a.pdf"}, _CTX).text


def test_scopes_default():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    for sc in (Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN):
        assert "read_url" in [t.name for t in reg.available(sc)]
