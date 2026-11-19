"""
Asistente por Telegram: atiende texto y notas de voz, Claude arma el ticket
y te llega la notificación.

Comandos del dueño:
  /miid                 -> muestra tu chat_id (para OWNER_CHAT_ID)
  /tickets              -> tickets abiertos, los más urgentes primero
  /ticket 12            -> detalle del ticket 12 y la conversación
  /avance 12 <texto>    -> guarda un avance y se lo avisa a quien lo pidió
  /cerrar 12 [texto]    -> cierra el ticket y le avisa a quien lo pidió
"""

import json
import logging
import os
import re
import time
import tempfile

from dotenv import load_dotenv

load_dotenv()

import cerebro  # noqa: E402  (después de load_dotenv para que lea el .env)
import datos  # noqa: E402
import voz  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.constants import ChatAction  # noqa: E402
from telegram.ext import Application, BusinessConnectionHandler, CommandHandler, ContextTypes, MessageHandler, filters  # noqa: E402

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "")
RESPONDER_CON_VOZ = os.getenv("RESPONDER_CON_VOZ", "si").lower() in ("si", "sí", "true", "1")
CANAL = "telegram"
ATAJO = re.compile(r"^/(ticket|avance|cerrar)_?(\d+)(?:@\w+)?\s*(.*)$", re.IGNORECASE | re.DOTALL)
ICONO = {"critica": "🔴", "alta": "🟠", "normal": "🟡", "baja": "⚪"}

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("telegram")


def es_dueno(update: Update) -> bool:
    return bool(OWNER_CHAT_ID) and str(update.effective_chat.id) == str(OWNER_CHAT_ID)


async def avisar_dueno(context: ContextTypes.DEFAULT_TYPE, texto: str):
    if OWNER_CHAT_ID:
        try:
            await context.bot.send_message(OWNER_CHAT_ID, texto)
        except Exception:
            log.exception("No se pudo avisar al dueño")


def formato_ticket(numero: int, t: dict, quien: str) -> str:
    campos = "".join(f"\n• {c['dato']}: {c['valor']}" for c in (t.get("campos") or []) if c.get("valor"))
    p = t.get("prioridad") or "normal"
    return (
        f"{ICONO.get(p, '🟡')} Ticket #{numero} [{t.get('tipo') or 'general'}] prioridad {p}\n"
        f"👤 {t.get('nombre') or quien}  ·  Telegram: {quien}\n"
        f"📞 {t.get('contacto') or 'sin dato'}\n"
        f"📌 {t.get('asunto', '')}\n"
        f"📝 {t.get('descripcion', '')}{campos}"
    )


# ---------------------------------------------------------------- atender a la persona
# Los mensajes llegan por dos caminos:
#  - directo al bot (@CampsHoldBot)                         -> canal "telegram"
#  - a la cuenta personal del dueño, vía Telegram Business  -> canal "personal:<id de conexión>"
#  - en un grupo, cuando mencionan al dueño o le responden  -> canal "grupo" (una conversación por persona)
DUENO_USUARIO = os.getenv("OWNER_USERNAME", "").lstrip("@")  # se completa solo al arrancar


def es_grupo(msg) -> bool:
    return msg.chat.type in ("group", "supergroup")


def canal_de(msg) -> str:
    if msg.business_connection_id:
        return f"personal:{msg.business_connection_id}"
    return "grupo" if es_grupo(msg) else CANAL


def conversacion_de(msg) -> str:
    # En un grupo cada persona tiene su propia conversación con el asistente
    return f"{msg.chat_id}:{msg.from_user.id}" if es_grupo(msg) else str(msg.chat_id)


def debe_atender_en_grupo(msg, context) -> bool:
    """En grupos solo responde si mencionan al dueño o al bot, o si le responden a alguno de los dos."""
    if not msg.from_user or str(msg.from_user.id) == str(OWNER_CHAT_ID) or msg.from_user.is_bot:
        return False
    texto = (msg.text or msg.caption or "").lower()
    usuarios = [u.lower() for u in (DUENO_USUARIO, context.bot.username) if u]
    if any(f"@{u}" in texto for u in usuarios):
        return True
    for ent in (msg.entities or []) + (msg.caption_entities or []):
        if ent.type == "text_mention" and ent.user and str(ent.user.id) == str(OWNER_CHAT_ID):
            return True
    r = msg.reply_to_message
    if r and r.from_user and (str(r.from_user.id) == str(OWNER_CHAT_ID) or r.from_user.id == context.bot.id):
        return True
    return False


# ---------------------------------------------------------------- disponibilidad del dueño
# /disponible  -> el bot no contesta en tu Telegram personal ni en grupos (contestas tú)
# /ocupado     -> el bot atiende normal
# Además, si tú escribes en un chat, el bot se calla en ESE chat por PAUSA_MINUTOS.
PAUSA_MINUTOS = int(os.getenv("PAUSA_MINUTOS", "30"))


def _clave_chat(msg) -> str:
    return f"ultimo_dueno:{msg.chat_id}"


async def registrar_actividad_dueno(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Corre antes que todo: si el dueño escribe en su cuenta personal o en un grupo, lo anota."""
    msg = update.effective_message
    if not msg or not msg.from_user or str(msg.from_user.id) != str(OWNER_CHAT_ID):
        return
    if msg.business_connection_id or es_grupo(msg):
        datos.guardar_ajuste(_clave_chat(msg), str(time.time()))


def debe_callarse(msg) -> bool:
    """True si el dueño está atendiendo él mismo (solo aplica a su cuenta personal y a grupos)."""
    if not (msg.business_connection_id or es_grupo(msg)):
        return False  # a quien le escribe directo al bot siempre se le atiende
    if datos.leer_ajuste("modo", "ocupado") == "disponible":
        return True
    ultimo = float(datos.leer_ajuste(_clave_chat(msg), "0") or 0)
    return time.time() - ultimo < PAUSA_MINUTOS * 60


def es_del_dueno_en_su_cuenta(msg) -> bool:
    """En la cuenta personal también llegan los mensajes que escribe el dueño: esos se ignoran."""
    return bool(msg.business_connection_id) and bool(OWNER_CHAT_ID) and str(msg.from_user.id) == str(OWNER_CHAT_ID)


async def accion(context, msg, tipo_accion):
    try:
        await context.bot.send_chat_action(
            msg.chat_id, tipo_accion, business_connection_id=msg.business_connection_id
        )
    except Exception:
        pass


async def atender(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, tipo: str):
    msg = update.effective_message
    chat_id = conversacion_de(msg)
    canal = canal_de(msg)
    user = msg.from_user
    quien = f"{user.full_name} (@{user.username})" if user.username else user.full_name
    if canal.startswith("personal:"):
        quien += " [a tu Telegram personal]"
    elif canal == "grupo":
        quien += f" [en el grupo {msg.chat.title}]"

    datos.guardar_mensaje(canal, chat_id, quien, "persona", tipo, texto)
    await accion(context, msg, ChatAction.TYPING)

    config = cerebro.cargar_config()
    tipo_fijo = cerebro.tipo_de_contacto(config, user.username or "", str(user.id))

    try:
        r = cerebro.pensar(
            datos.historial(canal, chat_id),
            config,
            quien=quien,
            tipo_fijo=tipo_fijo,
            tickets_previos=datos.tickets_de(canal, chat_id),
        )
    except Exception as e:
        log.exception("Error con Claude")
        # Plan B: igual dejamos el ticket con el mensaje tal cual, y la persona no nota nada raro
        t = {"tipo": "otro", "nombre": user.full_name, "asunto": texto[:80], "descripcion": texto, "campos": []}
        numero = datos.crear_ticket(canal, chat_id, quien, t, config)
        respuesta = f"Listo, ya le pasé tu mensaje a {cerebro.NOMBRE_DUENO} ✅ 🎫 #{numero}"
        datos.guardar_mensaje(canal, chat_id, "asistente", "asistente", "texto", respuesta)
        await msg.reply_text(respuesta)
        detalle = f"{type(e).__name__}: {str(e)[:300]}"
        await avisar_dueno(
            context,
            f"⚠️ Claude falló y dejé un ticket básico #{numero}.\nMensaje de {quien}:\n{texto}\n\nError: {detalle}",
        )
        return

    respuesta = (r.get("respuesta") or "").strip() or "¿Me repites, por favor?"
    etiqueta = ""

    if r.get("crear_ticket") and r.get("ticket"):
        t = r["ticket"]
        numero = datos.crear_ticket(canal, chat_id, quien, t, config)
        etiqueta = f"🎫 #{numero}"
        if config.get("tipos", {}).get(t.get("tipo"), {}).get("avisar", True):
            await avisar_dueno(context, "🆕 " + formato_ticket(numero, t, quien))

    nota = (r.get("nota_para_ticket") or "").strip()
    if nota and not r.get("crear_ticket"):
        abierto = datos.ultimo_ticket_abierto(canal, chat_id)
        if abierto:
            datos.agregar_nota(abierto["id"], nota)
            await avisar_dueno(context, f"📎 Detalle agregado al ticket #{abierto['id']} ({abierto['asunto']}):\n{nota}")

    aviso = (r.get("aviso_para_dueno") or "").strip()
    if aviso and not r.get("crear_ticket"):
        await avisar_dueno(context, f"💬 {quien}: {aviso}")

    texto_final = f"{respuesta} {etiqueta}".strip()
    datos.guardar_mensaje(canal, chat_id, "asistente", "asistente", "texto", texto_final)

    # Audio se contesta con audio; texto con texto. Respuestas largas o con código van en texto.
    para_leer = "```" in respuesta or respuesta.count("\n") >= 3 or len(respuesta) > 500
    if RESPONDER_CON_VOZ and tipo == "voz" and voz.disponible() and not para_leer:
        try:
            await accion(context, msg, ChatAction.RECORD_VOICE)
            with tempfile.TemporaryDirectory() as tmp:
                ruta = os.path.join(tmp, "respuesta.ogg")
                voz.texto_a_voz(respuesta, ruta)
                with open(ruta, "rb") as f:
                    await msg.reply_voice(f, caption=etiqueta or None)
            return
        except Exception:
            log.exception("No se pudo generar la voz; respondo en texto")
    await msg.reply_text(texto_final)


async def on_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if es_del_dueno_en_su_cuenta(msg):
        return
    if es_grupo(msg) and not debe_atender_en_grupo(msg, context):
        return
    if debe_callarse(msg):
        return
    # Si el dueño tocó /avance_N, su siguiente mensaje es el texto del avance
    if not msg.business_connection_id and es_dueno(update) and "avance_pendiente" in context.user_data:
        n = context.user_data.pop("avance_pendiente")
        await _guardar_avance(update, context, n, msg.text.strip())
        return
    await atender(update, context, msg.text, "texto")


async def on_voz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if es_del_dueno_en_su_cuenta(msg):
        return
    if es_grupo(msg) and not debe_atender_en_grupo(msg, context):
        return
    if debe_callarse(msg):
        return
    if not voz.disponible():
        await msg.reply_text("Por ahora solo leo mensajes escritos. ¿Me lo escribes, por favor?")
        return
    await accion(context, msg, ChatAction.TYPING)
    archivo = await (msg.voice or msg.audio).get_file()
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "nota.ogg")
        await archivo.download_to_drive(ruta)
        try:
            texto = voz.voz_a_texto(ruta)
        except Exception:
            log.exception("Error transcribiendo")
            await msg.reply_text("No pude escuchar bien el audio. ¿Me lo escribes, por favor?")
            return
    if not texto:
        await msg.reply_text("No escuché nada en el audio. ¿Lo intentas de nuevo?")
        return
    await atender(update, context, texto, "voz")


async def on_conexion_business(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.business_connection
    estado = "conectado ✅" if c.is_enabled else "desconectado ❌"
    log.info("Conexión Business %s: %s", c.id, estado)
    await avisar_dueno(context, f"🔗 Tu Telegram personal quedó {estado} con el asistente.")


# ---------------------------------------------------------------- comandos
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"¡Hola! 👋 Soy la extensión cerebral y de trabajo de {cerebro.NOMBRE_DUENO} 🧠. "
        "Explícame qué necesitas, así armo el ticket inmediatamente y empezamos el desarrollo."
    )


async def cmd_miid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu chat_id es: {update.effective_chat.id}")


async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueno(update):
        return
    filas = datos.tickets_abiertos()
    if not filas:
        await update.message.reply_text("✅ No hay tickets abiertos.")
        return
    lineas = [
        f"{ICONO.get(f['prioridad'], '🟡')} #{f['id']} [{f['tipo']}] {f['fecha']}  → /ticket_{f['id']}\n"
        f"    {f['nombre'] or f['quien']}: {f['asunto']}"
        for f in filas
    ]
    await update.message.reply_text("🎫 Tickets abiertos:\n\n" + "\n".join(lineas))


def _numero(context) -> int | None:
    return int(context.args[0]) if context.args and context.args[0].lstrip("#").isdigit() else None


async def _elegir_ticket(update: Update, comando: str, titulo: str):
    """Sin número: muestra los tickets abiertos con enlaces /comando_N para tocar."""
    filas = datos.tickets_abiertos()
    if not filas:
        await update.message.reply_text("✅ No hay tickets abiertos.")
        return
    lineas = [f"{ICONO.get(f['prioridad'], '🟡')} /{comando}_{f['id']}  {f['asunto']}" for f in filas]
    await update.message.reply_text(f"{titulo}\n\n" + "\n".join(lineas))


async def cmd_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueno(update):
        return
    n = _numero(context)
    if not n:
        await _elegir_ticket(update, "ticket", "¿Cuál ticket quieres ver? Toca uno:")
        return
    t = datos.ticket(n)
    if not t:
        await update.message.reply_text(f"No existe el ticket #{n}.")
        return
    t["campos"] = json.loads(t["campos"] or "[]")
    charla = "\n".join(
        f"{'👤' if m['rol'] == 'persona' else '🤖'} {m['contenido'][:600]}"
        for m in datos.ultimos_mensajes(t["canal"], t["conversacion"])
    )
    extra = f"\n\nEstado: {t['estado']}" + (f"\nAvance: {t['avance']}" if t["avance"] else "")
    acciones = "" if t["estado"] == "cerrado" else f"\n\n👉 /avance_{n}   ·   /cerrar_{n}"
    await update.message.reply_text(
        (formato_ticket(t["id"], t, t["quien"]) + extra + f"\n\nConversación:\n{charla}")[:3800] + acciones
    )


async def _notificar_persona(context, t: dict, texto: str):
    if t["canal"] == "grupo":
        grupo = t["conversacion"].split(":")[0]
        try:
            await context.bot.send_message(grupo, f"{t['quien'].split(' [')[0]}: {texto}")
        except Exception:
            log.exception("No pude avisar en el grupo")
        return
    conexion = t["canal"].split(":", 1)[1] if t["canal"].startswith("personal:") else None
    if t["canal"] == CANAL or conexion:
        try:
            await context.bot.send_message(t["conversacion"], texto, business_connection_id=conexion)
        except Exception:
            log.exception("No pude avisarle a la persona")


async def _guardar_avance(update: Update, context, n: int, texto: str):
    t = datos.ticket(n)
    if not t:
        await update.message.reply_text(f"No existe el ticket #{n}.")
        return
    datos.actualizar_ticket(n, estado="en progreso", avance=texto)
    await _notificar_persona(context, t, f"🔧 Avance de tu ticket #{n} ({t['asunto']}):\n{texto}")
    await update.message.reply_text(f"✅ Avance del #{n} guardado y enviado a {t['quien']}.")


async def cmd_avance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueno(update):
        return
    n = _numero(context)
    if not n:
        await _elegir_ticket(update, "avance", "¿A cuál ticket le das avance? Toca uno:")
        return
    texto = " ".join(context.args[1:]).strip()
    if texto:
        await _guardar_avance(update, context, n, texto)
        return
    t = datos.ticket(n)
    if not t:
        await update.message.reply_text(f"No existe el ticket #{n}.")
        return
    context.user_data["avance_pendiente"] = n
    await update.message.reply_text(
        f"✍️ Escribe el avance del ticket #{n} ({t['asunto']}).\nSe lo envío a {t['quien']}. (/cancelar para salir)"
    )


async def cmd_disponible(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueno(update):
        return
    datos.guardar_ajuste("modo", "disponible")
    await update.message.reply_text(
        "🟢 Modo disponible: no contesto en tu Telegram personal ni en grupos, contestas tú.\n"
        "Cuando te vayas escribe /ocupado y vuelvo a atender."
    )


async def cmd_ocupado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueno(update):
        return
    datos.guardar_ajuste("modo", "ocupado")
    await update.message.reply_text("🔴 Modo ocupado: yo atiendo y te dejo los tickets. (/disponible para volver)")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueno(update):
        return
    modo = datos.leer_ajuste("modo", "ocupado")
    texto = "🟢 Disponible (contestas tú)" if modo == "disponible" else "🔴 Ocupado (contesto yo)"
    await update.message.reply_text(
        f"Estado: {texto}\nSi escribes en un chat, me callo ahí {PAUSA_MINUTOS} minutos.\n"
        "Cambiar: /disponible · /ocupado"
    )


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("avance_pendiente", None)
    await update.message.reply_text("Listo, cancelado.")


async def cmd_cerrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueno(update):
        return
    n = _numero(context)
    if not n:
        await _elegir_ticket(update, "cerrar", "¿Cuál ticket cierras? Toca uno:")
        return
    t = datos.ticket(n)
    if not t:
        await update.message.reply_text(f"No existe el ticket #{n}.")
        return
    texto = " ".join(context.args[1:]).strip()
    datos.actualizar_ticket(n, estado="cerrado", avance=texto or None)
    await _notificar_persona(
        context, t, f"✅ Tu ticket #{n} ({t['asunto']}) quedó resuelto." + (f"\n{texto}" if texto else "")
    )
    await update.message.reply_text(f"✅ Ticket #{n} cerrado y {t['quien']} quedó notificado.")


async def atajo_numero(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite /ticket1, /ticket_1, /avance_1 texto, /cerrar1 ... (sin espacio)."""
    m = ATAJO.match(update.message.text or "")
    if not m:
        return
    comando, numero, resto = m.group(1).lower(), m.group(2), (m.group(3) or "").lstrip(":").split()
    context.args = [numero] + resto
    await {"ticket": cmd_ticket, "avance": cmd_avance, "cerrar": cmd_cerrar}[comando](update, context)


async def al_arrancar(app: Application):
    global DUENO_USUARIO
    if OWNER_CHAT_ID and not DUENO_USUARIO:
        try:
            DUENO_USUARIO = (await app.bot.get_chat(OWNER_CHAT_ID)).username or ""
        except Exception:
            log.exception("No pude leer el usuario del dueño")
    log.info("Atiendo menciones a @%s y @%s en grupos", DUENO_USUARIO or "?", app.bot.username)


def construir_app() -> Application:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Falta TELEGRAM_TOKEN en el archivo .env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("Falta ANTHROPIC_API_KEY en el archivo .env")
    datos.crear_tablas()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(al_arrancar).build()
    # Grupo -1: se ejecuta antes que los demás y no los bloquea
    app.add_handler(MessageHandler(filters.ALL, registrar_actividad_dueno), group=-1)
    for nombre, fn in [
        ("start", cmd_start),
        ("miid", cmd_miid),
        ("tickets", cmd_tickets),
        ("ticket", cmd_ticket),
        ("avance", cmd_avance),
        ("cerrar", cmd_cerrar),
        ("cancelar", cmd_cancelar),
        ("disponible", cmd_disponible),
        ("ocupado", cmd_ocupado),
        ("estado", cmd_estado),
    ]:
        app.add_handler(CommandHandler(nombre, fn))
    app.add_handler(MessageHandler(filters.Regex(ATAJO), atajo_numero))
    nuevos = filters.UpdateType.MESSAGE | filters.UpdateType.BUSINESS_MESSAGE
    app.add_handler(MessageHandler(nuevos & (filters.VOICE | filters.AUDIO), on_voz))
    app.add_handler(MessageHandler(nuevos & filters.TEXT & ~filters.COMMAND, on_texto))
    app.add_handler(BusinessConnectionHandler(on_conexion_business))
    return app


def main():
    """Solo Telegram (sin llamadas). Para Telegram + llamadas usa: python servidor.py"""
    app = construir_app()
    log.info("Asistente encendido (Claude: %s). Ctrl+C para apagar.", cerebro.MODELO)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
