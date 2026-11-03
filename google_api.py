"""Acceso a Google Calendar y Gmail con OAuth de usuario."""

import base64
import pickle
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from functools import cache

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import settings

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]


# =============================================================================
# CREDENCIALES
# =============================================================================
def get_credentials(interactive: bool = False):
    """
    Carga el token guardado y lo renueva si ha caducado.
    Solo con interactive=True abre el navegador para autorizar (scripts/autorizar_google.py):
    el servidor nunca debe quedarse esperando a un navegador en mitad de una petición.
    """
    creds = None
    if settings.TOKEN_PATH.exists():
        creds = pickle.loads(settings.TOKEN_PATH.read_bytes())
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            if not interactive:
                raise
            creds = None  # token revocado: hay que autorizar de nuevo

    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError("Google no está autorizado. Ejecuta: python scripts/autorizar_google.py")
        flow = InstalledAppFlow.from_client_secrets_file(str(settings.CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)

    settings.TOKEN_PATH.write_bytes(pickle.dumps(creds))
    return creds


# Un cliente por API para todo el proceso: las credenciales se renuevan solas al caducar
@cache
def _calendar():
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


@cache
def _gmail():
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


# =============================================================================
# CALENDAR
# =============================================================================
def _intervalo(fecha: date, hora: str) -> tuple[datetime, datetime]:
    inicio = datetime.combine(fecha, time.fromisoformat(hora), tzinfo=settings.TZ)
    return inicio, inicio + timedelta(minutes=settings.DURACION_CITA_MIN)


def _horario_evento(fecha: date, hora: str) -> dict:
    inicio, fin = _intervalo(fecha, hora)
    return {
        "start": {"dateTime": inicio.isoformat(), "timeZone": settings.TIMEZONE},
        "end": {"dateTime": fin.isoformat(), "timeZone": settings.TIMEZONE},
    }


def _parse_rfc3339(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))  # Python 3.10 no acepta "Z"


def horas_libres(fecha: date) -> list[str]:
    """Horas de HORAS_DISPONIBLES de ese día que no chocan con ningún evento ni han pasado ya."""
    inicio_dia = datetime.combine(fecha, time.min, tzinfo=settings.TZ)
    respuesta = (
        _calendar()
        .freebusy()
        .query(
            body={
                "timeMin": inicio_dia.isoformat(),
                "timeMax": (inicio_dia + timedelta(days=1)).isoformat(),
                "items": [{"id": settings.CALENDAR_ID}],
            }
        )
        .execute()
    )
    ocupados = [
        (_parse_rfc3339(b["start"]), _parse_rfc3339(b["end"]))
        for b in respuesta["calendars"][settings.CALENDAR_ID].get("busy", [])
    ]

    ahora = datetime.now(settings.TZ)
    libres = []
    for hora in settings.HORAS_DISPONIBLES:
        inicio, fin = _intervalo(fecha, hora)
        if inicio > ahora and not any(inicio < b_fin and b_inicio < fin for b_inicio, b_fin in ocupados):
            libres.append(hora)
    return libres


def crear_evento(fecha: date, hora: str, titulo: str, descripcion: str) -> dict:
    body = {"summary": titulo, "description": descripcion, **_horario_evento(fecha, hora)}
    return _calendar().events().insert(calendarId=settings.CALENDAR_ID, body=body).execute()


def mover_evento(event_id: str, fecha: date, hora: str) -> dict:
    return (
        _calendar()
        .events()
        .patch(calendarId=settings.CALENDAR_ID, eventId=event_id, body=_horario_evento(fecha, hora))
        .execute()
    )


def borrar_evento(event_id: str) -> None:
    try:
        _calendar().events().delete(calendarId=settings.CALENDAR_ID, eventId=event_id).execute()
    except HttpError as e:
        # Ya no existe en Calendar (p. ej. borrado a mano): el resultado es el mismo
        if e.resp.status not in (404, 410):
            raise


# =============================================================================
# GMAIL
# =============================================================================
def enviar_email(destinatario: str, asunto: str, cuerpo: str) -> str:
    """Envía un email de texto plano y devuelve el id del mensaje."""
    msg = EmailMessage()
    msg["To"] = destinatario
    msg["Subject"] = asunto
    if settings.GMAIL_FROM:
        msg["From"] = settings.GMAIL_FROM
    msg.set_content(cuerpo)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return _gmail().users().messages().send(userId="me", body={"raw": raw}).execute()["id"]
