"""
Contestadora de llamadas telefónicas.

Flujo:
  1. Alguien llama a tu celular y no contestas -> WOM desvía la llamada al número de Twilio.
  2. Twilio pide instrucciones a POST /llamada -> le decimos que abra un canal de audio (WebSocket).
  3. WebSocket /audio/<secreto>: puenteamos el audio entre Twilio y la voz en tiempo real de OpenAI.
  4. Al colgar, Claude lee la transcripción y arma el ticket; te llega a Telegram.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from xml.sax.saxutils import escape

import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

import cerebro
import datos

log = logging.getLogger("llamadas")
router = APIRouter()

CANAL = "llamada"
MODELO_VOZ_EN_VIVO = os.getenv("MODELO_VOZ_EN_VIVO", "gpt-realtime")
VOZ = os.getenv("VOZ", "coral")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
MAX_MINUTOS = int(os.getenv("MAX_MINUTOS_LLAMADA", "10"))

# Se fija desde servidor.py: función async para avisarle al dueño por Telegram
avisar_dueno = None


def _secreto() -> str:
    """Parte secreta de la URL del audio, para que solo Twilio (que la recibe de nosotros) se conecte."""
    base = TWILIO_AUTH_TOKEN or os.getenv("TELEGRAM_TOKEN", "sin-secreto")
    return hashlib.sha256(("audio:" + base).encode()).hexdigest()[:32]


def _firma_valida(request: Request, form: dict) -> bool:
    """Verifica que la petición venga de Twilio (cabecera X-Twilio-Signature)."""
    if not TWILIO_AUTH_TOKEN:
        return True  # sin token configurado no se puede verificar (solo para pruebas)
    firma = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    # Detrás del proxy de Railway la URL interna es http; Twilio firmó la pública https
    proto = request.headers.get("x-forwarded-proto")
    if proto:
        url = url.replace(url.split(":", 1)[0] + ":", proto + ":", 1)
    datos_firma = url + "".join(k + form[k] for k in sorted(form))
    esperado = base64.b64encode(
        hmac.new(TWILIO_AUTH_TOKEN.encode(), datos_firma.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(esperado, firma)


@router.get("/")
async def salud():
    return PlainTextResponse("calls-hold OK")


@router.post("/llamada")
async def entrante(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not _firma_valida(request, form):
        log.warning("Petición de llamada con firma inválida")
        return Response(status_code=403)

    numero = form.get("From", "desconocido")
    # Si la llamada llegó desviada, Twilio suele mandar el número original en From
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    url_audio = f"wss://{host}/audio/{_secreto()}"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{escape(url_audio)}">
      <Parameter name="numero" value="{escape(numero)}"/>
    </Stream>
  </Connect>
</Response>"""
    log.info("Llamada entrante de %s", numero)
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/audio/{secreto}")
async def audio(ws: WebSocket, secreto: str):
    if not hmac.compare_digest(secreto, _secreto()):
        await ws.close(code=1008)
        return
    await ws.accept()

    estado = {"stream": None, "numero": "desconocido", "lineas": []}
    config = cerebro.cargar_config()

    try:
        # Esperar el evento "start" de Twilio para saber quién llama
        while estado["stream"] is None:
            ev = json.loads(await ws.receive_text())
            if ev.get("event") == "start":
                estado["stream"] = ev["start"]["streamSid"]
                estado["numero"] = ev["start"].get("customParameters", {}).get("numero", "desconocido")
    except WebSocketDisconnect:
        return

    numero = estado["numero"]
    tipo_fijo = cerebro.tipo_de_contacto(config, numero, numero.lstrip("+"))
    quien = f"Llamada de {numero}"
    instrucciones = cerebro.instrucciones_llamada(
        config, quien, tipo_fijo, datos.tickets_de(CANAL, numero)
    )

    url = f"wss://api.openai.com/v1/realtime?model={MODELO_VOZ_EN_VIVO}"
    clave = os.environ["OPENAI_API_KEY"].strip().strip('"')
    cabeceras = {"Authorization": f"Bearer {clave}"}
    try:
        async with websockets.connect(url, additional_headers=cabeceras) as ia:
            await ia.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": MODELO_VOZ_EN_VIVO,
                    "instructions": instrucciones,
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcmu"},  # el mismo formato que usa Twilio
                            "turn_detection": {"type": "server_vad", "silence_duration_ms": 700},
                            "transcription": {"model": "whisper-1", "language": "es"},
                        },
                        "output": {"format": {"type": "audio/pcmu"}, "voice": VOZ},
                    },
                },
            }))
            # Que conteste primero (saludo + aviso de que es una IA)
            await ia.send(json.dumps({"type": "response.create"}))

            async def de_twilio_a_ia():
                while True:
                    ev = json.loads(await ws.receive_text())
                    if ev.get("event") == "media":
                        await ia.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": ev["media"]["payload"],
                        }))
                    elif ev.get("event") == "stop":
                        return

            async def de_ia_a_twilio():
                async for crudo in ia:
                    ev = json.loads(crudo)
                    t = ev.get("type")
                    if t == "response.output_audio.delta":
                        await ws.send_text(json.dumps({
                            "event": "media",
                            "streamSid": estado["stream"],
                            "media": {"payload": ev["delta"]},
                        }))
                    elif t == "input_audio_buffer.speech_started":
                        # La persona interrumpió: callar lo que se estaba diciendo
                        await ws.send_text(json.dumps({"event": "clear", "streamSid": estado["stream"]}))
                        await ia.send(json.dumps({"type": "response.cancel"}))
                    elif t == "conversation.item.input_audio_transcription.completed":
                        if ev.get("transcript", "").strip():
                            estado["lineas"].append(("persona", ev["transcript"].strip()))
                    elif t == "response.output_audio_transcript.done":
                        if ev.get("transcript", "").strip():
                            estado["lineas"].append(("asistente", ev["transcript"].strip()))
                    elif t == "error" and ev.get("error", {}).get("code") != "response_cancel_not_active":
                        log.warning("Error de la voz en vivo: %s", ev.get("error"))

            tareas = [asyncio.create_task(de_twilio_a_ia()), asyncio.create_task(de_ia_a_twilio())]
            try:
                await asyncio.wait(tareas, timeout=MAX_MINUTOS * 60, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tareas:
                    t.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Falló la llamada de %s", numero)
    finally:
        asyncio.create_task(_cerrar_llamada(numero, estado["lineas"], config, tipo_fijo))


async def _cerrar_llamada(numero: str, lineas: list, config: dict, tipo_fijo):
    """Guarda la conversación, arma el ticket con Claude y avisa al dueño."""
    quien = f"Llamada de {numero}"
    if not lineas:
        if avisar_dueno:
            await avisar_dueno(f"📞 Llamada perdida de {numero} (colgó sin hablar).")
        return
    for rol, texto in lineas:
        datos.guardar_mensaje(CANAL, numero, quien, rol, "voz", texto)
    transcripcion = "\n".join(f"{'Persona' if r == 'persona' else 'Extensión'}: {t}" for r, t in lineas)

    try:
        r = await asyncio.to_thread(cerebro.ticket_de_llamada, transcripcion, config, quien, tipo_fijo)
    except Exception:
        log.exception("No pude armar el ticket de la llamada")
        r = {"respuesta": "", "crear_ticket": False}

    texto = f"📞 Llamada de {numero}\n{(r.get('respuesta') or '').strip()}"
    if r.get("crear_ticket") and r.get("ticket"):
        t = r["ticket"]
        n = datos.crear_ticket(CANAL, numero, quien, t, config)
        campos = "".join(f"\n• {c['dato']}: {c['valor']}" for c in (t.get("campos") or []) if c.get("valor"))
        texto += (
            f"\n\n🆕 Ticket #{n} [{t.get('tipo') or 'general'}] prioridad {t.get('prioridad') or 'normal'}"
            f"\n📌 {t.get('asunto', '')}\n📝 {t.get('descripcion', '')}{campos}"
        )
    resumen = transcripcion if len(transcripcion) < 2500 else transcripcion[:2500] + "…"
    texto += f"\n\n🗒️ Conversación:\n{resumen}"
    if avisar_dueno:
        await avisar_dueno(texto[:4000])
