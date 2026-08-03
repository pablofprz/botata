"""El bot no postea links que no leyó.

Caso real (2026-08-02 00:32): le pidieron una canción, NO llamó a `search_music`
—en el log no hay ninguna tool de música en ese flujo— y posteó igual
`open.spotify.com/track/<id>` con el id fabricado. Link roto, en público.

El prompt ya decía "nunca escribas una URL que no leíste ahí". Inventarla es
exactamente lo que hace un modelo cuando le falta el dato, así que el guard es
de código: se compara contra TODO lo que el modelo tuvo delante (system + user).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402

_REAL = "https://open.spotify.com/track/1xJk0K83KnHsH2ZPx6CwEr"
_FALSO = "https://open.spotify.com/track/6cB4PZinventado99"


def test_saca_el_link_inventado():
    visto = "Resultado de get_my_recent_posts: hoy ando nostálgico"
    out = b._sacar_links_inventados(f"me colgué y me olvidé de ponerla\n\n{_FALSO}", visto)
    assert _FALSO not in out
    assert "me colgué y me olvidé de ponerla" in out


def test_deja_el_link_que_trajo_la_tool():
    visto = f"Resultado de search_music: - Rock para los Dientes — Redondos ({_REAL})"
    texto = f"va un temazo de los redondos {_REAL}"
    assert b._sacar_links_inventados(texto, visto) == texto


def test_no_le_molesta_el_esquema():
    """La tool devuelve https:// y el bot puede escribirlo pelado (o al revés)."""
    visto = f"Resultado de search_music: {_REAL}"
    sin_esquema = _REAL.replace("https://", "")
    assert sin_esquema in b._sacar_links_inventados(f"escuchá esto {sin_esquema}", visto)


def test_un_link_del_hilo_vale():
    """Si alguien lo pegó en la conversación, el bot puede repetirlo."""
    visto = "ppolci.com: mirá esto https://www.youtube.com/watch?v=abc123"
    texto = "jajaja lo vi, https://www.youtube.com/watch?v=abc123 es un clásico"
    assert b._sacar_links_inventados(texto, visto) == texto


def test_un_dominio_pelado_no_se_toca():
    """Nombrar un dominio no es citar una fuente falsa, y borrarlo rompe la frase."""
    texto = "buscá en google.com que está lleno de eso"
    assert b._sacar_links_inventados(texto, "") == texto


def test_varios_links_mezclados():
    visto = f"Resultado de search_music: {_REAL}"
    out = b._sacar_links_inventados(f"este sí {_REAL} y este no {_FALSO}", visto)
    assert _REAL in out and _FALSO not in out


def test_sin_links_no_cambia_nada():
    texto = "no tengo el tema a mano, pero era de los redondos"
    assert b._sacar_links_inventados(texto, "") == texto


def test_no_deja_basura_de_espacios():
    out = b._sacar_links_inventados(f"escuchate esto:  {_FALSO}  ahora", "")
    assert "  " not in out and out.endswith("ahora")
