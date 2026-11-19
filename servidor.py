"""
Servidor completo: bot de Telegram + contestadora de llamadas en un solo proceso.

  python servidor.py

En Railway (o cualquier hosting) este es el comando de arranque. Usa la variable PORT.
"""

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from telegram import Update  # noqa: E402

import bot_telegram  # noqa: E402
import llamadas  # noqa: E402

log = logging.getLogger("servidor")


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    tg = bot_telegram.construir_app()
    await tg.initialize()
    await bot_telegram.al_arrancar(tg)
    await tg.start()
    await tg.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def avisar(texto: str):
        if bot_telegram.OWNER_CHAT_ID:
            try:
                await tg.bot.send_message(bot_telegram.OWNER_CHAT_ID, texto)
            except Exception:
                log.exception("No pude avisar al dueño")

    llamadas.avisar_dueno = avisar
    log.info("Telegram y llamadas encendidos")
    try:
        yield
    finally:
        await tg.updater.stop()
        await tg.stop()
        await tg.shutdown()


app = FastAPI(lifespan=ciclo_de_vida)
app.include_router(llamadas.router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), proxy_headers=True, forwarded_allow_ips="*")
