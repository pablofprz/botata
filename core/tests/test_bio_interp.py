"""La interpretación de bios se acota en el CÓDIGO, no en el prompt.

Caso real (2026-07-24, encontrado el 28/7): ante la bio "estupidez natural" el
modelo entró en un loop de razonamiento y devolvió **96.789 caracteres** de
monólogo interno. El guardrail era `interp.upper() in ("NADA","NOTHING")` —una
igualdad EXACTA—, así que una respuesta que divagaba y de paso decía NADA no lo
activó. Se guardó entera como bio del usuario y cada línea entró como un hecho
suyo: 262 filas basura, el 42% de `user_facts`, que además GANABAN el retrieval
(a "hola bot como andas" el bot recuperaba "WAIT.", "Just the word.").

La lección que fija este archivo: lo que un LLM escribe en la base se valida en
código. El prompt pide; el código comprueba.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402


# ─── Lo que sí es un dato del usuario ───────────────────────────────────────
def test_deja_pasar_bullets_legitimos():
    crudo = "- Vive en Rosario\n- Profesión: docente\n- Hincha de Central"
    assert b._sanear_bio_interp(crudo) == (
        "Vive en Rosario\nProfesión: docente\nHincha de Central")


def test_una_sola_linea_sin_viñeta_tambien_vale():
    assert b._sanear_bio_interp("Es peronista.") == "Es peronista."


# ─── Lo que es el modelo pensando en voz alta ───────────────────────────────
@pytest.mark.parametrize("linea", [
    "NADA", "nothing", "N/A", "",
    "*   Wait, is 'estupidez natural' a data point in itself?",
    "*   I will respond with NADA.",
    "*   Let's review.",
    "WAIT.",
    "Could it be an ideology?",
    "The instruction is very clear.",
    '"Bio de @fueraomasalla.bsky.social: estupidez natural"',
    "¿Será que se identifica como misántropo?",
])
def test_reconoce_el_razonamiento(linea):
    assert b._es_razonamiento(linea)


@pytest.mark.parametrize("linea", [
    "Vive en Rosario", "Profesión: docente", "Identidad: crítico de la estupidez humana",
])
def test_no_confunde_un_dato_con_razonamiento(linea):
    assert not b._es_razonamiento(linea)


# ─── Los topes duros ────────────────────────────────────────────────────────
def test_una_respuesta_desbordada_se_descarta_entera():
    """EL CASO REAL: 96k de monólogo. No se intenta rescatar líneas — que es
    justo como entraron las 262 basura."""
    desborde = "\n".join(f"*   Let's reconsider point {i}." for i in range(4000))
    assert len(desborde) > b._BIO_DESCARTE_MAX
    assert b._sanear_bio_interp(desborde) == ""


def test_corta_la_cantidad_de_lineas():
    crudo = "\n".join(f"- Dato número {i}" for i in range(30))
    assert len(b._sanear_bio_interp(crudo).splitlines()) == b.BIO_INTERP_MAX_LINEAS


def test_corta_el_largo_total():
    crudo = "\n".join("- Vive en Rosario y " + ("bla " * 40) for _ in range(5))
    assert 0 < len(b._sanear_bio_interp(crudo)) <= b.BIO_INTERP_MAX_CHARS


def test_una_linea_en_minuscula_se_descarta():
    """Un dato arranca en mayúscula; el pedazo del medio de un razonamiento no."""
    assert b._sanear_bio_interp("is appropriate. The bio is a subjective statement.") == ""


def test_mezcla_real_deja_solo_lo_util():
    """Como vino la respuesta que rompió todo, pero corta."""
    crudo = ("is appropriate. The bio is a subjective statement. NADA.\n"
             "*   Wait, is 'estupidez natural' a data point in itself?\n"
             "*   Let's formulate a response based on extraction:\n"
             "*   Identidad: Crítico de la estupidez humana.")
    assert b._sanear_bio_interp(crudo) == "Identidad: Crítico de la estupidez humana."


def test_vacio_y_none_no_rompen():
    assert b._sanear_bio_interp("") == ""
    assert b._sanear_bio_interp(None) == ""
    assert b._sanear_bio_interp("   \n\n  ") == ""
