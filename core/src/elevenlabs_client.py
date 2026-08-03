"""elevenlabs_client.py — cliente mínimo de ElevenLabs (stdlib, sin deps).

Lo comparten la tool `generate_audio` (botata.py) y la UI de config (listar las
voces de la cuenta y probar la voz elegida). ElevenLabs no habla OpenAI, así
que no pasa por el router: endpoint propio con su propia key.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

API = "https://api.elevenlabs.io/v1"

# Ogg/Opus a propósito: es lo que WhatsApp exige para que el audio salga como
# NOTA DE VOZ (PTT) y lo que Telegram va a pedir en sendVoice (T40) — un solo
# formato sirve a los dos canales, sin transcodificar.
OUTPUT_FORMAT = "opus_48000_64"
DEFAULT_MODEL = "eleven_multilingual_v2"


def tts(texto: str, *, voice_id: str, api_key: str,
        model_id: str = DEFAULT_MODEL, timeout: float = 60) -> bytes:
    """POST al TTS. Devuelve el audio en Ogg/Opus. Levanta en error HTTP."""
    if not voice_id:
        raise ValueError("falta AUDIO_GEN.voice_id en el settings")
    url = f"{API}/text-to-speech/{urllib.parse.quote(voice_id)}?output_format={OUTPUT_FORMAT}"
    body = json.dumps({"text": texto, "model_id": model_id or DEFAULT_MODEL}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "xi-api-key": api_key or "", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def list_voices(api_key: str, timeout: float = 15) -> list[dict]:
    """Las voces que ESTA cuenta puede usar por API. Devuelve
    [{id, name, category, labels}]. Levanta en error HTTP.

    Importa porque el tier gratis solo permite las `premade` y las propias:
    un voice_id de la library compila bien y después el TTS da 402 — mejor
    elegir de una lista que adivinar un ID que no va a andar.
    """
    req = urllib.request.Request(f"{API}/voices", headers={"xi-api-key": api_key or ""})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return [{
        "id": v.get("voice_id"),
        "name": v.get("name"),
        "category": v.get("category"),
        "labels": v.get("labels") or {},
    } for v in data.get("voices") or []]


def error_legible(e: Exception) -> str:
    """Traduce los errores HTTP típicos de ElevenLabs a algo accionable."""
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        try:
            detalle = json.loads(e.read().decode("utf-8", "replace"))
            msg = (detalle.get("detail") or {}).get("message") or str(detalle)[:200]
        except Exception:
            msg = ""
        if e.code == 401:
            return "ElevenLabs rechazó la API key (401) — revisala en Credenciales"
        if e.code == 402:
            return f"ElevenLabs pide plan pago para eso (402): {msg}"
        if e.code == 429:
            return "ElevenLabs: cuota agotada o demasiadas llamadas (429)"
        return f"ElevenLabs devolvió HTTP {e.code}: {msg}"
    return f"no pude hablar con ElevenLabs: {e}"
