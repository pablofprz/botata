"""extract.py — extracción de datos genérica vía LLM.

Segundo pilar reusable del scraping: convierte el texto renderizado de CUALQUIER
página en structured output (pydantic), sin selectores CSS por sitio. Ese es el
precio y la virtud del enfoque LLM-driven: cuesta tokens, pero generaliza a IG,
Reddit, X o lo que venga sin escribir un parser por plataforma.
"""
from __future__ import annotations

import logging
from typing import Protocol, TypeVar

from pydantic import BaseModel

log = logging.getLogger("botata.extract")

T = TypeVar("T", bound=BaseModel)


class SupportsComplete(Protocol):
    """Interfaz mínima del cliente LLM (la cumple botata.LLMClient)."""
    def complete(self, system: str, user: str, response_model: type[BaseModel]) -> BaseModel: ...


_SYSTEM = (
    "Sos un extractor de datos estructurados. Te doy el texto renderizado de una "
    "página web y devolvés ÚNICAMENTE los campos del schema en JSON válido. "
    "Reglas: si un campo no aparece en la página, usá null (o lista vacía). "
    "No inventes datos, no completes con suposiciones, no agregues campos extra."
)


class LLMExtractor:
    """Página (texto) → instancia pydantic, vía LLM. Agnóstico de plataforma."""

    def __init__(self, llm: SupportsComplete, *, max_chars: int = 12_000) -> None:
        self.llm = llm
        self.max_chars = max_chars

    def extract(self, page_text: str, schema: type[T]) -> T | None:
        """Devuelve una instancia de `schema` extraída del texto, o None si falla."""
        text = page_text[: self.max_chars].strip()
        if not text:
            return None
        user = f"Contenido de la página:\n\n{text}"
        try:
            result = self.llm.complete(_SYSTEM, user, schema)
            return result  # type: ignore[return-value]
        except Exception as e:
            log.warning("extract falló para %s: %s", schema.__name__, e)
            return None
