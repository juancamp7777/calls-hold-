"""
Voz: pasar audio a texto y texto a audio.

Claude no procesa audio, así que esta parte usa OpenAI (Whisper y TTS).
Es opcional: sin OPENAI_API_KEY el asistente funciona solo con texto.
"""

import os

MODELO_VOZ_A_TEXTO = os.getenv("MODELO_VOZ_A_TEXTO", "whisper-1")
MODELO_TEXTO_A_VOZ = os.getenv("MODELO_TEXTO_A_VOZ", "gpt-4o-mini-tts")
VOZ = os.getenv("VOZ", "coral")
ESTILO_VOZ = os.getenv(
    "ESTILO_VOZ",
    "Eres una mujer joven de Bogotá hablando por nota de voz con un compañero de trabajo. "
    "Habla natural y relajado, como en una conversación real: ritmo ágil, entonación variada, "
    "pausas cortas donde va una coma, sonrisa en la voz. Acento colombiano suave. "
    "Nada de tono de locutor ni robótico.",
)

_cliente = None


def disponible() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def _openai():
    global _cliente
    if _cliente is None:
        from openai import OpenAI

        _cliente = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip().strip('"'))
    return _cliente


def voz_a_texto(ruta_audio: str) -> str:
    with open(ruta_audio, "rb") as f:
        r = _openai().audio.transcriptions.create(model=MODELO_VOZ_A_TEXTO, file=f, language="es")
    return r.text.strip()


def texto_a_voz(texto: str, ruta_salida: str) -> None:
    """Genera un audio OGG/Opus (el formato de las notas de voz de Telegram)."""
    with _openai().audio.speech.with_streaming_response.create(
        model=MODELO_TEXTO_A_VOZ, voice=VOZ, input=texto, instructions=ESTILO_VOZ, response_format="opus"
    ) as r:
        r.stream_to_file(ruta_salida)
