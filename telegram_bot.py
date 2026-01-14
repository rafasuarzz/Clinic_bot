
import os
import json
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from agent import ClinicaAgent

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRATAMIENTOS_FILE = Path("data/tratamientos.json")


def cargar_tratamientos() -> list[str]:
    if not TRATAMIENTOS_FILE.exists():
        return []
    data = json.loads(TRATAMIENTOS_FILE.read_text(encoding="utf-8"))
    return [t["nombre"] for t in data]


tratamientos = cargar_tratamientos()
agent = ClinicaAgent(tratamientos)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "Hola 👋 Bienvenido a la Clínica Pure.\n\n"
        "Puedo ayudarte a:\n"
        "• Informarte sobre tratamientos\n"
        "• Reservar una cita paso a paso\n\n"
        "¿Qué te gustaría hacer?"
    )
    await update.message.reply_text(welcome_text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_message = update.message.text
        user_id = str(update.message.from_user.id)
        response_text = agent.process_message(user_message, user_id)
        await update.message.reply_text(response_text)
    except Exception as e:
        await update.message.reply_text(f"Ha ocurrido un error: {e}\nPor favor, intenta de nuevo.")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en .env")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot de Telegram corriendo...")
    app.run_polling()
