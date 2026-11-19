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
import tempfile

from dotenv import load_dotenv

load_dotenv()

import cerebro  # noqa: E402  (después de load_dotenv para que lea el .env)
import datos  # noqa: E402
import voz  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.constants import ChatAction  # noqa: E402
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters  # noqa: E402

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
async def atender(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, tipo: str):
    chat_id = update.effective_chat.id
    user = update.effective_user
    quien = f"{user.full_name} (@{user.username})" if user.username else user.full_name

    datos.guardar_mensaje(CANAL, chat_id, quien, "persona", tipo, texto)
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    config = cerebro.cargar_config()
    tipo_fijo = cerebro.tipo_de_contacto(config, user.username or "", str(user.id))

    try:
        r = cerebro.pensar(
            datos.historial(CANAL, chat_id),
            config,
            quien=quien,
            tipo_fijo=tipo_fijo,
            tickets_previos=datos.tickets_de(CANAL, chat_id),
        )
    except Exception:
        log.exception("Error con Claude")
        await update.message.reply_text(
            "Disculpa, tuve un problema técnico. Tu mensaje quedó guardado y se lo paso de inmediato."
        )
        await avisar_dueno(context, f"⚠️ Error del asistente. Mensaje de {quien}:\n{texto}")
        return

    respuesta = (r.get("respuesta") or "").strip() or "¿Me repites, por favor?"
    respuesta_voz = respuesta

    if r.get("crear_ticket") and r.get("ticket"):
        t = r["ticket"]
        numero = datos.crear_ticket(CANAL, chat_id, quien, t, config)
        respuesta += f"\n\n🎫 Ticket #{numero}"
        if config.get("tipos", {}).get(t.get("tipo"), {}).get("avisar", True):
            await avisar_dueno(context, "🆕 " + formato_ticket(numero, t, quien))

    datos.guardar_mensaje(CANAL, chat_id, "asistente", "asistente", "texto", respuesta)
    await update.message.reply_text(respuesta)

    if RESPONDER_CON_VOZ and tipo == "voz" and voz.disponible():
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
            with tempfile.TemporaryDirectory() as tmp:
                ruta = os.path.join(tmp, "respuesta.ogg")
                voz.texto_a_voz(respuesta_voz, ruta)
                with open(ruta, "rb") as f:
                    await update.message.reply_voice(f)
        except Exception:
            log.exception("No se pudo generar la voz")


async def on_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Si el dueño tocó /avance_N, su siguiente mensaje es el texto del avance
    if es_dueno(update) and "avance_pendiente" in context.user_data:
        n = context.user_data.pop("avance_pendiente")
        await _guardar_avance(update, context, n, update.message.text.strip())
        return
    await atender(update, context, update.message.text, "texto")


async def on_voz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not voz.disponible():
        await update.message.reply_text("Por ahora solo leo mensajes escritos. ¿Me lo escribes, por favor?")
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    archivo = await (update.message.voice or update.message.audio).get_file()
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "nota.ogg")
        await archivo.download_to_drive(ruta)
        try:
            texto = voz.voz_a_texto(ruta)
        except Exception:
            log.exception("Error transcribiendo")
            await update.message.reply_text("No pude escuchar bien el audio. ¿Me lo escribes, por favor?")
            return
    if not texto:
        await update.message.reply_text("No escuché nada en el audio. ¿Lo intentas de nuevo?")
        return
    await atender(update, context, texto, "voz")


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
    if t["canal"] == CANAL:
        try:
            await context.bot.send_message(t["conversacion"], texto)
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


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Falta TELEGRAM_TOKEN en el archivo .env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("Falta ANTHROPIC_API_KEY en el archivo .env")
    datos.crear_tablas()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for nombre, fn in [
        ("start", cmd_start),
        ("miid", cmd_miid),
        ("tickets", cmd_tickets),
        ("ticket", cmd_ticket),
        ("avance", cmd_avance),
        ("cerrar", cmd_cerrar),
        ("cancelar", cmd_cancelar),
    ]:
        app.add_handler(CommandHandler(nombre, fn))
    app.add_handler(MessageHandler(filters.Regex(ATAJO), atajo_numero))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voz))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_texto))
    log.info("Asistente encendido (Claude: %s). Ctrl+C para apagar.", cerebro.MODELO)
    app.run_polling()


if __name__ == "__main__":
    main()
