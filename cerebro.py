"""
El cerebro del asistente: Claude decide qué responder y arma el ticket.

Este módulo no sabe nada de Telegram ni de llamadas. Recibe la conversación
y devuelve un dict con la respuesta y, si aplica, el ticket. Así el mismo
cerebro sirve para el bot de Telegram y, más adelante, para las llamadas.
"""

import json
import logging
import os
from pathlib import Path

import anthropic

log = logging.getLogger("cerebro")

MODELO = os.getenv("MODELO_CLAUDE", "claude-opus-5-5")
# low = respuestas rápidas (importante cuando se contesta con voz).
# Sube a "medium" si quieres que analice más cada caso.
ESFUERZO = os.getenv("ESFUERZO_CLAUDE", "low")

NOMBRE_DUENO = os.getenv("NOMBRE_DUENO", "el desarrollador")
CONTEXTO = os.getenv("CONTEXTO", "")

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", Path(__file__).with_name("tickets_config.json")))

_cliente: anthropic.Anthropic | None = None


def cliente() -> anthropic.Anthropic:
    global _cliente
    if _cliente is None:
        _cliente = anthropic.Anthropic()  # lee ANTHROPIC_API_KEY del entorno
    return _cliente


# ---------------------------------------------------------------- configuración
def cargar_config() -> dict:
    """Lee tickets_config.json en cada mensaje: se puede editar sin reiniciar."""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("No pude leer %s, uso configuración básica", CONFIG_PATH)
        return {
            "tipos": {
                "general": {
                    "descripcion": "Cualquier solicitud",
                    "campos": ["nombre", "que necesita"],
                    "prioridad": "normal",
                }
            },
            "contactos": {},
        }


def tipo_de_contacto(config: dict, *claves: str) -> str | None:
    """Busca a la persona en 'contactos' por usuario de Telegram, id o teléfono."""
    contactos = {
        k.lower().lstrip("@"): v
        for k, v in config.get("contactos", {}).items()
        if not k.startswith("_")
    }
    for clave in claves:
        clave = (clave or "").lower().lstrip("@")
        if clave and clave in contactos:
            return contactos[clave]
    return None


# ---------------------------------------------------------------- instrucciones
def _describir_tipos(config: dict) -> str:
    lineas = []
    for nombre, t in config.get("tipos", {}).items():
        linea = (
            f'- "{nombre}": {t.get("descripcion", "")}\n'
            f'  Datos a obtener: {", ".join(t.get("campos", []))}\n'
            f'  Prioridad por defecto: {t.get("prioridad", "normal")}'
        )
        if t.get("instrucciones"):
            linea += f'\n  Cómo atenderlo: {t["instrucciones"]}'
        lineas.append(linea)
    return "\n".join(lineas)


def _instrucciones_fijas(config: dict) -> str:
    return f"""Eres la extensión cerebral y de trabajo de {NOMBRE_DUENO}, desarrollador full stack en una empresa
de tecnología. Atiendes a las personas que lo buscan (sobre todo sus jefes) cuando él no puede contestar.
Si te preguntan quién eres, di que eres la extensión cerebral y de trabajo de {NOMBRE_DUENO}: tu trabajo es
entender bien lo que necesitan y armar el ticket de inmediato para que el desarrollo arranque.
Hablas español de Colombia: cordial, profesional y breve. Máximo 2 o 3 frases por respuesta,
porque muchas veces tu respuesta se escucha como nota de voz o en una llamada.

Contexto de su trabajo:
{CONTEXTO or "(sin contexto adicional)"}

Tu objetivo es dejarle a {NOMBRE_DUENO} un ticket con todo lo necesario para que pueda actuar
sin tener que volver a llamar a la persona a preguntar.

Cómo trabajas:
1. Identifica qué tipo de solicitud es (lista de abajo) según lo que diga la persona.
2. Pide los datos que faltan de ese tipo, de a uno o dos por mensaje, con naturalidad.
   No hagas interrogatorios: si la persona ya dio un dato, no lo vuelvas a pedir.
3. Cuando tengas los datos importantes (o la persona no quiera dar más), crea el ticket
   y confírmale que {NOMBRE_DUENO} ya fue notificado.
4. Un ticket por solicitud. Si ya creaste uno en esta conversación, no lo repitas salvo
   que llegue una solicitud nueva y distinta.
5. Nunca prometas fechas de entrega, soluciones técnicas ni decisiones en nombre de {NOMBRE_DUENO}.
   Tampoco pidas ni repitas contraseñas, tokens o credenciales: si alguien los manda,
   no los copies al ticket.
6. Si preguntan por un ticket anterior, usa la sección "Tickets de esta persona".

Tipos de solicitud:
{_describir_tipos(config)}

Prioridades: "baja", "normal", "alta", "critica" (critica = producción caída o clientes afectados ahora).

Formato de salida:
- "respuesta": lo que le dices a la persona.
- "crear_ticket": true solo en el mensaje en que creas el ticket.
- "ticket": si crear_ticket es false, déjalo con textos vacíos y campos = [].
  "campos" es la lista de datos del tipo, cada uno con "dato" (nombre del dato) y "valor"."""


def _instrucciones_de_esta_persona(tipo_fijo: str | None, quien: str, tickets: list[dict]) -> str:
    partes = [f"Estás hablando con: {quien}."]
    if tipo_fijo:
        partes.append(
            f'Esta persona está registrada como "{tipo_fijo}": usa ese tipo y no le preguntes quién es.'
        )
    if tickets:
        partes.append("Tickets de esta persona:")
        for t in tickets:
            linea = f'- #{t["id"]} ({t["estado"]}) {t["asunto"]}'
            if t.get("avance"):
                linea += f' | Último avance de {NOMBRE_DUENO}: {t["avance"]}'
            partes.append(linea)
    return "\n".join(partes)


def _esquema(config: dict) -> dict:
    tipos = list(config.get("tipos", {}).keys()) or ["general"]
    return {
        "type": "object",
        "properties": {
            "respuesta": {"type": "string"},
            "crear_ticket": {"type": "boolean"},
            "ticket": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": tipos + [""]},
                    "nombre": {"type": "string"},
                    "contacto": {"type": "string"},
                    "asunto": {"type": "string"},
                    "descripcion": {"type": "string"},
                    "prioridad": {"type": "string", "enum": ["baja", "normal", "alta", "critica", ""]},
                    "campos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"dato": {"type": "string"}, "valor": {"type": "string"}},
                            "required": ["dato", "valor"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["tipo", "nombre", "contacto", "asunto", "descripcion", "prioridad", "campos"],
                "additionalProperties": False,
            },
        },
        "required": ["respuesta", "crear_ticket", "ticket"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------- llamada a Claude
def pensar(
    historial: list[dict],
    config: dict,
    quien: str,
    tipo_fijo: str | None = None,
    tickets_previos: list[dict] | None = None,
) -> dict:
    """
    historial: [{"role": "user"|"assistant", "content": "..."}], del más viejo al más nuevo.
    Devuelve {"respuesta": str, "crear_ticket": bool, "ticket": {...}}.
    """
    # La conversación enviada a Claude debe empezar con un mensaje de la persona
    while historial and historial[0]["role"] != "user":
        historial = historial[1:]

    respuesta = cliente().beta.messages.create(
        model=MODELO,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        # Si Claude rechaza la solicitud por seguridad, la reintenta con otro modelo
        fallbacks="default",
        output_config={
            "effort": ESFUERZO,
            "format": {"type": "json_schema", "schema": _esquema(config)},
        },
        system=[
            # Parte fija (se reutiliza entre mensajes gracias al caché)
            {"type": "text", "text": _instrucciones_fijas(config), "cache_control": {"type": "ephemeral"}},
            # Parte que cambia según la persona
            {"type": "text", "text": _instrucciones_de_esta_persona(tipo_fijo, quien, tickets_previos or [])},
        ],
        messages=historial,
    )

    if respuesta.stop_reason == "refusal":
        log.warning("Claude no quiso responder: %s", respuesta.stop_details)
        return {
            "respuesta": f"Disculpa, eso no lo puedo gestionar por aquí. Le dejo tu mensaje a {NOMBRE_DUENO}.",
            "crear_ticket": False,
            "ticket": None,
        }

    texto = next((b.text for b in respuesta.content if b.type == "text"), "")
    datos = json.loads(texto)

    if tipo_fijo and datos.get("ticket"):
        datos["ticket"]["tipo"] = tipo_fijo
    return datos
