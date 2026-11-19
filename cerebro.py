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
# medium = buen balance entre rapidez y respuestas técnicas acertadas.
# Baja a "low" si quieres que conteste más rápido.
ESFUERZO = os.getenv("ESFUERZO_CLAUDE", "medium")

NOMBRE_DUENO = os.getenv("NOMBRE_DUENO", "el desarrollador")
CONTEXTO = os.getenv("CONTEXTO", "")

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", Path(__file__).with_name("tickets_config.json")))

_cliente: anthropic.Anthropic | None = None


def cliente() -> anthropic.Anthropic:
    global _cliente
    if _cliente is None:
        # .strip(): al pegar claves en paneles web a veces se cuela un espacio o salto de línea
        _cliente = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "").strip().strip('"'))
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


def _estilo(config: dict) -> str:
    e = config.get("estilo", {})
    partes = [
        f"Tu forma de escribir debe sonar a {NOMBRE_DUENO}, no a un bot de atención al cliente: "
        "nada de frases corporativas ni de plantilla. "
        + (e.get("tono") or "Español de Colombia, cercano, seguro y directo.")
    ]
    if e.get("expresiones"):
        partes.append("Expresiones que usa (úsalas con naturalidad, sin forzarlas): " + ", ".join(e["expresiones"]))
    if e.get("evitar"):
        partes.append("Evita: " + ", ".join(e["evitar"]))
    if e.get("ejemplos"):
        partes.append("Ejemplos reales de cómo escribe:\n" + "\n".join(f'- "{x}"' for x in e["ejemplos"]))
    return "\n".join(partes)


def _instrucciones_fijas(config: dict) -> str:
    return f"""Eres la extensión cerebral y de trabajo de {NOMBRE_DUENO}, desarrollador full stack en una empresa
de tecnología. Atiendes a quienes le escriben (jefes, compañeros, clientes) cuando él no puede contestar.
Si te preguntan quién eres, di que eres la extensión cerebral y de trabajo de {NOMBRE_DUENO}.
No te presentes ni saludes en cada mensaje: ve directo al punto.
{_estilo(config)}

Contexto de su trabajo:
{CONTEXTO or "(sin contexto adicional)"}

No le llenes el chat a la gente: cada mensaje tuyo debe ser corto y útil. Nada de relleno.

Cómo trabajas:
1. Ticket inmediato: en el PRIMER mensaje en que haya un pedido de trabajo (algo para hacer, revisar,
   arreglar o decidir), crea el ticket con lo que tengas, sin preguntar nada antes.
   Tu respuesta es solo la confirmación, por ejemplo: "Listo, ticket creado ✅" o
   "Ticket creado ✅ ya le llegó a {NOMBRE_DUENO}". Nada más (el número se agrega solo).
2. No hagas preguntas. Solo si es imposible saber de qué se trata (por ejemplo, el mensaje es
   "ayuda" y nada más), haz UNA pregunta corta. Nunca insistas.
3. Enlaces, archivos, capturas o textos largos: van tal cual al ticket. Nunca digas que no puedes
   abrirlos ni pidas que los expliquen.
4. Preguntas que puedes responder YA (técnicas: cómo hacer algo, un error, código, arquitectura,
   bases de datos, despliegues, APIs; o algo urgente que se resuelve con información):
   responde de una, acertado y concreto, como un desarrollador full stack senior. Ahí sí puedes
   extenderte lo necesario (pasos o código corto). Cierra con una línea corta: "— extensión de
   {NOMBRE_DUENO}, él lo confirma". Si depende de algo que no sabes (su código, sus servidores,
   decisiones del equipo), no inventes: dilo en una frase. Crea también un ticket tipo "consulta".
5. Detalles que llegan después del ticket: ponlos en "nota_para_ticket" y responde solo
   "Agregado al ticket ✅". No crees otro ticket salvo que sea un pedido nuevo y distinto.
6. Saludos, gracias, charla: respuesta mínima ("Con gusto 👍"), sin ticket. Si no hace falta
   responder nada, responde igual algo mínimo.
7. Si la persona está molesta o grosera, no te justifiques ni te disculpes: confirma que su pedido
   ya está con {NOMBRE_DUENO}.
8. Nunca prometas fechas, prioridades ni decisiones en nombre de {NOMBRE_DUENO}.
   No pidas ni repitas contraseñas, tokens o credenciales; si alguien los manda, no los copies
   al ticket y dile en una frase que los cambie.
9. Si preguntan por un ticket anterior, usa la sección "Tickets de esta persona" (máximo 2 frases).
10. Tus respuestas pueden convertirse en nota de voz: escribe como se habla, sin listas largas,
    salvo en las respuestas técnicas del punto 4.

Tipos de solicitud:
{_describir_tipos(config)}
Los "Datos a obtener" de cada tipo son para llenar el ticket con lo que la persona YA dijo.
NO son preguntas que debas hacer: si un dato no está, déjalo vacío.

Prioridades: "baja", "normal", "alta", "critica" (critica = producción caída o clientes afectados ahora).

Formato de salida:
- "respuesta": lo que le dices a la persona.
- "crear_ticket": true solo en el mensaje en que creas el ticket.
- "ticket": si crear_ticket es false, déjalo con textos vacíos y campos = [].
  "campos" es la lista de datos del tipo, cada uno con "dato" y "valor" (solo los que conoces).
- "nota_para_ticket": texto a agregar al último ticket abierto de esta persona (punto 5), o "" si no aplica."""


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
            "nota_para_ticket": {"type": "string"},
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
        "required": ["respuesta", "crear_ticket", "nota_para_ticket", "ticket"],
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


# ---------------------------------------------------------------- llamadas telefónicas
def instrucciones_llamada(config: dict, quien: str, tipo_fijo: str | None, tickets: list[dict]) -> str:
    """Instrucciones para la voz en tiempo real que atiende las llamadas."""
    return f"""Eres la extensión cerebral y de trabajo de {NOMBRE_DUENO}, desarrollador full stack en una empresa
de tecnología. Contestas SU teléfono cuando él no puede atender. Esto es una llamada de voz en vivo.

{_estilo(config)}

Contexto de su trabajo:
{CONTEXTO or "(sin contexto adicional)"}

Reglas de la llamada:
1. Al contestar, preséntate en UNA frase y deja claro que eres una IA, por ejemplo:
   "Hola, habla la extensión de {NOMBRE_DUENO}, su asistente con inteligencia artificial. Él no puede
   atender ahora, ¿en qué te ayudo?"
2. Escucha el pedido. No hagas interrogatorios: como mucho una o dos preguntas cortas si falta algo
   clave (qué sistema, qué tan urgente). Si la persona no sabe o no quiere, sigue.
3. Si te hacen una pregunta técnica que puedes responder (código, errores comunes, arquitectura,
   bases de datos, APIs, despliegues), respóndela bien y concreta, como un desarrollador senior,
   pero hablada: sin leer código símbolo por símbolo, explica la idea y los pasos.
   Si depende de su código, sus servidores o decisiones del equipo, dilo y déjalo anotado.
4. Nunca prometas fechas ni decisiones en nombre de {NOMBRE_DUENO}. No pidas contraseñas.
5. Para cerrar, confirma en una o dos frases lo que quedó anotado y despídete recordando que eres
   la extensión de {NOMBRE_DUENO}, por ejemplo: "Listo, ya le dejé el ticket a {NOMBRE_DUENO} con todo.
   Recuerda que hablaste con su extensión; él te confirma. ¡Que estés bien!"
6. Habla natural, frases cortas, como en una llamada real entre compañeros de trabajo.
   Si te interrumpen, para y escucha.

Tipos de solicitud que manejas (para saber qué datos son útiles, no para preguntarlos todos):
{_describir_tipos(config)}

{_instrucciones_de_esta_persona(tipo_fijo, quien, tickets)}"""


def ticket_de_llamada(transcripcion: str, config: dict, quien: str, tipo_fijo: str | None = None) -> dict:
    """Al colgar: Claude lee la transcripción y arma el ticket (mismo formato que en Telegram)."""
    historial = [{
        "role": "user",
        "content": (
            "Esta es la transcripción de una llamada telefónica que YA TERMINÓ, atendida por la "
            f"extensión de {NOMBRE_DUENO}. Crea el ticket si hubo cualquier pedido, pregunta o recado "
            "(crear_ticket=true). Incluye en la descripción lo que se respondió en la llamada. "
            "Si fue una llamada vacía o equivocada, crear_ticket=false. "
            "En 'respuesta' escribe un resumen de la llamada en 1 o 2 frases para el dueño.\n\n"
            + transcripcion
        ),
    }]
    return pensar(historial, config, quien=quien, tipo_fijo=tipo_fijo)
