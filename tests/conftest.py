"""
Fixtures comunes. Ningún test toca servicios reales:
- cada test usa una base de datos SQLite temporal y vacía;
- Google Calendar y Gmail se sustituyen por FakeGoogle, que guarda las llamadas recibidas.
"""

import os
import tempfile

# Antes de importar el proyecto: mcp_server inicializa la BD al importarse y no debe tocar la real
os.environ["CLINICA_DB"] = os.path.join(tempfile.mkdtemp(), "import.db")

from datetime import date, datetime, timedelta  # noqa: E402

import httplib2  # noqa: E402
import pytest  # noqa: E402
from fastmcp import Client  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402

import db  # noqa: E402
import google_api  # noqa: E402
import mcp_server  # noqa: E402
import settings  # noqa: E402


class _Peticion:
    """Imita las peticiones de googleapiclient, que se ejecutan con .execute()."""

    def __init__(self, resultado):
        self._resultado = resultado

    def execute(self):
        return self._resultado


class FakeGoogle:
    """Calendar y Gmail en memoria con la misma interfaz que usa google_api."""

    def __init__(self):
        self.llamadas: list[tuple[str, str]] = []
        self.emails: list[tuple[str, str]] = []
        self.ocupados: dict[str, list[tuple[str, str]]] = {}  # "YYYY-MM-DD" -> [("HH:MM", "HH:MM")]
        self.borrados_en_calendar: set[str] = set()  # eventos que ya no existen en Calendar
        self._siguiente_id = 0

    def ocupar(self, dia: date, inicio: str, fin: str) -> None:
        self.ocupados.setdefault(dia.isoformat(), []).append((inicio, fin))

    # --- Calendar ---
    def freebusy(self):
        return self

    def query(self, body):
        dia = date.fromisoformat(body["timeMin"][:10])

        def rfc3339(hora: str) -> str:
            return datetime.combine(dia, datetime.strptime(hora, "%H:%M").time(), tzinfo=settings.TZ).isoformat()

        busy = [{"start": rfc3339(i), "end": rfc3339(f)} for i, f in self.ocupados.get(dia.isoformat(), [])]
        return _Peticion({"calendars": {settings.CALENDAR_ID: {"busy": busy}}})

    def events(self):
        return self

    def insert(self, calendarId, body):
        self._siguiente_id += 1
        event_id = f"evento_{self._siguiente_id}"
        self.llamadas.append(("insert", event_id))
        return _Peticion({"id": event_id})

    def patch(self, calendarId, eventId, body):
        self.llamadas.append(("patch", eventId))
        return _Peticion({"id": eventId})

    def delete(self, calendarId, eventId):
        self.llamadas.append(("delete", eventId))
        if eventId in self.borrados_en_calendar:
            raise HttpError(httplib2.Response({"status": 410}), b"Resource has been deleted")
        return _Peticion({})

    # --- Gmail ---
    def enviar_email(self, destinatario: str, asunto: str, cuerpo: str) -> str:
        self.emails.append((destinatario, asunto))
        return "mensaje_1"


@pytest.fixture(autouse=True)
def bd_temporal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "clinica.db")
    db.init_db()


@pytest.fixture(autouse=True)
def google(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(google_api, "_calendar", lambda: fake)
    monkeypatch.setattr(google_api, "enviar_email", fake.enviar_email)
    return fake


@pytest.fixture
def dia_laborable() -> date:
    """Un día laborable dentro de una semana: futuro y con todas las horas por delante."""
    dia = datetime.now(settings.TZ).date() + timedelta(days=7)
    while dia.weekday() >= 5:
        dia += timedelta(days=1)
    return dia


@pytest.fixture
async def mcp():
    """Cliente MCP conectado en memoria al servidor (mismo protocolo que por HTTP)."""
    async with Client(mcp_server.mcp) as cliente:
        yield cliente
