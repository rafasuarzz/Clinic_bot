"""Configuración central: rutas, horario de la clínica y variables de entorno (.env)."""

import json
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Rutas ---
DATA_DIR = BASE_DIR / "data"  # ficheros de ejecución, ignorados por git
CREDENTIALS_PATH = DATA_DIR / "credentials.json"
TOKEN_PATH = DATA_DIR / "token.pickle"
DB_PATH = Path(os.getenv("CLINICA_DB") or DATA_DIR / "clinica.db")
TRATAMIENTOS_PATH = BASE_DIR / "config" / "tratamientos.json"

# --- Clínica ---
TIMEZONE = os.getenv("TIMEZONE", "Atlantic/Canary")
TZ = ZoneInfo(TIMEZONE)
HORAS_DISPONIBLES = ("10:00", "11:00", "12:00", "16:00", "17:00")
DURACION_CITA_MIN = 45

# --- Google ---
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GMAIL_FROM = os.getenv("GMAIL_FROM") or None

# --- Servidor MCP (solo localhost por defecto: no tiene autenticación) ---
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT") or 3333)
MCP_URL = os.getenv("MCP_URL") or f"http://localhost:{MCP_PORT}/mcp"

# --- Claude y Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Prefijo CLINICA_ para no chocar con variables de otras herramientas (p. ej. CLAUDE_* de Claude Code)
MODEL = os.getenv("CLINICA_MODEL") or "claude-sonnet-5-5"
EFFORT = os.getenv("CLINICA_EFFORT") or "low"  # low | medium | high


def cargar_tratamientos() -> list[dict]:
    return json.loads(TRATAMIENTOS_PATH.read_text(encoding="utf-8"))


class _SinRuidoSSE(logging.Filter):
    """Oculta el ClosedResourceError que la librería mcp registra al cerrar cada sesión (inofensivo)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "standalone SSE writer" not in record.getMessage()


def configurar_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # Una línea por cada petición HTTP / sesión MCP: solo se muestran avisos y errores
    for ruidoso in ("httpx", "httpx2", "mcp.client.streamable_http"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
    logging.getLogger("mcp.server.streamable_http").addFilter(_SinRuidoSSE())
