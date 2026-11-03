"""Bot de Telegram de la clínica: recibe mensajes y responde con el agente."""

import logging

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import settings
from agent import ERROR_SISTEMA, ClinicaAgent

log = logging.getLogger(__name__)
agent = ClinicaAgent()

BIENVENIDA = (
    "Hola 👋 Bienvenido a la Clínica Pure.\n\n"
    "Puedo ayudarte a:\n"
    "• Informarte sobre tratamientos\n"
    "• Reservar, cambiar o cancelar una cita\n\n"
    "¿Qué te gustaría hacer?"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(BIENVENIDA)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    await update.effective_chat.send_action("typing")
    try:
        respuesta = await agent.process_message(update.message.text, user_id)
    except Exception:
        # El detalle va al log; al usuario nunca se le muestran errores internos
        log.exception("Error inesperado atendiendo a %s", user_id)
        respuesta = ERROR_SISTEMA
    await update.message.reply_text(respuesta)


def main() -> None:
    settings.configurar_logging()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en el .env")

    # Varios usuarios a la vez (el agente serializa los mensajes de cada usuario)
    app = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("🤖 Bot de Telegram corriendo")
    app.run_polling()


if __name__ == "__main__":
    main()
