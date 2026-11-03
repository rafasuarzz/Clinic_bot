
# mcp_server.py
import os
import json
import pickle
import base64
from email.message import EmailMessage
from typing import Annotated, List, Dict, Any, Optional
from pathlib import Path
from datetime import datetime, timedelta, date as date_cls
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.requests import Request

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest

load_dotenv()

import db  # después de load_dotenv: lee CLINICA_DB del entorno

# =============================================================================
# CONFIG
# =============================================================================
# Scopes combinados: Calendar + Gmail send
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "data" / "credentials.json"
TOKEN_PATH = BASE_DIR / "data" / "token.pickle"

CALENDAR_ID = "primary"
TIMEZONE = "Atlantic/Canary"  # Canarias (WET/WEST)
HORAS_DISPONIBLES: List[str] = ["10:00", "11:00", "12:00", "16:00", "17:00"]
SLOT_MINUTES = 45

TRATAMIENTOS_JSON = BASE_DIR / "config" / "tratamientos.json"

db.init_db()
mcp = FastMCP("Clinica MCP")

# =============================================================================
# AUTH / CLIENTE GOOGLE
# =============================================================================
def _get_google_credentials():
    creds = None
    token_file = Path(TOKEN_PATH)
    if token_file.exists():
        with open(token_file, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "wb") as f:
            pickle.dump(creds, f)
    return creds

def get_calendar_service():
    creds = _get_google_credentials()
    return build("calendar", "v3", credentials=creds)

def get_gmail_service():
    creds = _get_google_credentials()
    return build("gmail", "v1", credentials=creds)

# =============================================================================
# UTILIDADES
# =============================================================================
def _read_json(path: str, default):
    try:
        if Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8"))
        return default
    except Exception:
        return default

def _parse_fecha_to_iso(fecha_str: str) -> str:
    """
    Acepta 'YYYY-MM-DD' o 'DD/MM/AAAA' y devuelve 'YYYY-MM-DD'.
    """
    fecha_str = (fecha_str or "").strip()
    if "/" in fecha_str:
        d, m, y = fecha_str.split("/")
        y, m, d = int(y), int(m), int(d)
        return f"{y:04d}-{m:02d}-{d:02d}"
    y, m, d = map(int, fecha_str.split("-"))
    return f"{y:04d}-{m:02d}-{d:02d}"

def _is_weekend(fecha_iso: str) -> bool:
    y, m, d = map(int, fecha_iso.split("-"))
    return date_cls(y, m, d).weekday() >= 5  # 5=Sat, 6=Sun

def _local_day_bounds(fecha_iso: str, tz_name: str):
    tz = ZoneInfo(tz_name)
    y, m, d = map(int, fecha_iso.split("-"))
    start_local = datetime(y, m, d, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local

def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end

# =============================================================================
# EMAIL (Gmail API)
# =============================================================================
def _send_email(to_email: str, subject: str, body_text: str) -> Dict[str, Any]:
    """
    Envío de correo vía Gmail API (users.messages.send).
    Requiere el scope https://www.googleapis.com/auth/gmail.send.
    """
    from_addr = os.getenv("GMAIL_FROM")  # opcional
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = to_email
        if from_addr:
            msg["From"] = from_addr
        msg.set_content(body_text)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service = get_gmail_service()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"ok": True, "id": sent.get("id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# =============================================================================
# MCP TOOLS
# =============================================================================
# Tipos con descripción: FastMCP los publica en el esquema que ve el modelo
Fecha = Annotated[str, Field(description="Fecha (YYYY-MM-DD o DD/MM/AAAA)")]
Hora = Annotated[str, Field(description="Hora en formato HH:MM")]
EventId = Annotated[str, Field(description="calendar_event_id de la cita (de 'citas_activas' en obtener_cliente)")]
ClienteId = Annotated[str, Field(description="Identificador estable del cliente (Telegram user_id)")]

@mcp.tool(
    name="info_tratamiento",
    description="Devuelve la ficha del tratamiento (duración, precio, descripción, indicaciones, contraindicaciones). Solo para consultas previas.",
)
def info_tratamiento(nombre: Annotated[str, Field(description="Nombre del tratamiento")]) -> dict:
    try:
        datos = _read_json(TRATAMIENTOS_JSON, default=[])
        for t in datos:
            if t.get("nombre", "").strip().lower() == nombre.strip().lower():
                return {"ok": True, "tratamiento": t, "mensaje": f"Información de '{t.get('nombre')}'"}
        return {
            "ok": False,
            "error": f"Tratamiento '{nombre}' no está en tratamientos.json",
            "tratamiento": {"nombre": nombre, "duracion": None, "precio": None, "descripcion": ""},
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool(
    name="ver_disponibilidad",
    description="Consulta en Google Calendar las horas libres (huecos de 45 min) para un tratamiento en una fecha.",
)
def ver_disponibilidad(
    tratamiento: Annotated[str, Field(description="Nombre del tratamiento")],
    fecha: Fecha,
) -> Dict[str, Any]:
    try:
        fecha_iso = _parse_fecha_to_iso(fecha)
        if _is_weekend(fecha_iso):
            return {
                "ok": False,
                "error": "La clínica no abre fines de semana. Elige un día de lunes a viernes.",
                "horas_disponibles": [],
                "fecha": fecha_iso,
                "tratamiento": tratamiento,
                "timezone": TIMEZONE,
            }

        service = get_calendar_service()
        day_start_local, day_end_local = _local_day_bounds(fecha_iso, TIMEZONE)
        time_min_utc = day_start_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
        time_max_utc = day_end_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")

        fb_body = {"timeMin": time_min_utc, "timeMax": time_max_utc, "timeZone": TIMEZONE, "items": [{"id": CALENDAR_ID}]}
        fb = service.freebusy().query(body=fb_body).execute()
        busy_list = fb.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])

        busy_intervals_local: List[tuple[datetime, datetime]] = []
        for b in busy_list:
            b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE))
            b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE))
            busy_intervals_local.append((b_start, b_end))

        libres: List[str] = []
        for hhmm in HORAS_DISPONIBLES:
            h, m = map(int, hhmm.split(":"))
            slot_start = day_start_local.replace(hour=h, minute=m, second=0, microsecond=0)
            slot_end = slot_start + timedelta(minutes=SLOT_MINUTES)
            ocupado = any(_overlaps(slot_start, slot_end, b0, b1) for (b0, b1) in busy_intervals_local)
            if not ocupado:
                libres.append(hhmm)

        return {
            "ok": True,
            "fecha": fecha_iso,
            "tratamiento": tratamiento,
            "horas_disponibles": libres,
            "timezone": TIMEZONE,
            "mensaje": f"Horas disponibles para {tratamiento} el {fecha_iso} ({TIMEZONE})",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "horas_disponibles": []}

def _comprobar_slot_libre(tratamiento: str, fecha_iso: str, hora: str) -> Optional[Dict[str, Any]]:
    """Devuelve None si la hora está libre; si no, un dict de error con las horas libres."""
    disp = ver_disponibilidad.fn(tratamiento=tratamiento, fecha=fecha_iso)
    if not disp.get("ok"):
        return {"ok": False, "error": disp.get("error", "No se pudo comprobar la disponibilidad")}
    libres = disp.get("horas_disponibles", [])
    if hora not in libres:
        return {
            "ok": False,
            "error": f"La hora {hora} del {fecha_iso} no está disponible.",
            "horas_disponibles": libres,
        }
    return None

# Interna (no es tool MCP): las reservas pasan siempre por confirmar_reserva,
# que además envía el email y registra al cliente.
def _crear_cita(nombre: str, tratamiento: str, fecha: str, hora: str) -> Dict[str, Any]:
    try:
        fecha_iso = _parse_fecha_to_iso(fecha)
        if _is_weekend(fecha_iso):
            return {"ok": False, "error": "No se pueden crear citas en sábado o domingo."}
        service = get_calendar_service()
        tz = ZoneInfo(TIMEZONE)
        h, m = map(int, hora.split(":"))
        y, mo, d = map(int, fecha_iso.split("-"))
        start_local = datetime(y, mo, d, h, m, tzinfo=tz)
        end_local = start_local + timedelta(minutes=SLOT_MINUTES)

        event = service.events().insert(
            calendarId=CALENDAR_ID,
            body={
                "summary": f"🏥 {tratamiento}",
                "description": f"Cliente: {nombre}\nTratamiento: {tratamiento}",
                "start": {"dateTime": start_local.isoformat(), "timeZone": TIMEZONE},
                "end": {"dateTime": end_local.isoformat(), "timeZone": TIMEZONE},
            },
        ).execute()

        return {
            "ok": True,
            "calendar_event_id": event["id"],
            "htmlLink": event.get("htmlLink"),
            "nombre": nombre,
            "tratamiento": tratamiento,
            "fecha": fecha_iso,
            "hora": hora,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _cita_del_cliente(calendar_event_id: str, cliente_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Devuelve (cita, None) si la cita existe, pertenece al cliente y no está cancelada;
    si no, (None, error). Mismo mensaje para "no existe" y "es de otro cliente",
    para no revelar qué IDs existen.
    """
    cita = db.get_cita(calendar_event_id)
    if not cita or cita["cliente_id"] != cliente_id:
        return None, {"ok": False, "error": "No se encontró esa cita entre las citas del cliente."}
    if cita["estado"] == "cancelada":
        return None, {"ok": False, "error": "Esa cita ya está cancelada."}
    return cita, None

@mcp.tool(
    name="modificar_cita",
    description="Cambia la fecha/hora de una cita del cliente (mismo evento de Calendar) y envía email de confirmación del cambio.",
)
def modificar_cita(cliente_id: ClienteId, calendar_event_id: EventId, nueva_fecha: Fecha, nueva_hora: Hora) -> Dict[str, Any]:
    try:
        cita, error = _cita_del_cliente(calendar_event_id, cliente_id)
        if error:
            return error

        fecha_iso = _parse_fecha_to_iso(nueva_fecha)
        if _is_weekend(fecha_iso):
            return {"ok": False, "error": "No se pueden mover citas a sábado o domingo."}

        error_slot = _comprobar_slot_libre(cita["tratamiento"], fecha_iso, nueva_hora)
        if error_slot:
            return error_slot

        service = get_calendar_service()
        tz = ZoneInfo(TIMEZONE)
        h, m = map(int, nueva_hora.split(":"))
        y, mo, d = map(int, fecha_iso.split("-"))
        start_local = datetime(y, mo, d, h, m, tzinfo=tz)
        end_local = start_local + timedelta(minutes=SLOT_MINUTES)

        updated = service.events().patch(
            calendarId=CALENDAR_ID,
            eventId=calendar_event_id,
            body={
                "start": {"dateTime": start_local.isoformat(), "timeZone": TIMEZONE},
                "end": {"dateTime": end_local.isoformat(), "timeZone": TIMEZONE},
            },
        ).execute()

        db.actualizar_fecha_cita(calendar_event_id, fecha_iso, nueva_hora)

        # Enviar email de confirmación de cambio si tenemos correo
        cliente = db.get_cliente(cliente_id) or {}
        notif_result = None
        if cliente.get("email"):
            cuerpo = (
                f"Hola {cliente.get('nombre') or ''}, hemos modificado tu cita de {cita['tratamiento']}.\n"
                f"📝 Antes: {cita['fecha']} {cita['hora']}\n"
                f"✅ Ahora: {fecha_iso} {nueva_hora}\n"
                f"🔗 Enlace del evento: {updated.get('htmlLink','(no disponible)')}\n\n"
                f"Gracias por confiar en Clínica Pure."
            )
            notif_result = _send_email(
                to_email=cliente["email"],
                subject=f"Confirmación de cambio de cita ({fecha_iso} {nueva_hora})",
                body_text=cuerpo,
            )

        return {
            "ok": True,
            "calendar_event_id": calendar_event_id,
            "htmlLink": updated.get("htmlLink"),
            "fecha": fecha_iso,
            "hora": nueva_hora,
            "notificacion": notif_result,
            "mensaje": "Cita modificada y confirmación enviada por email.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool(
    name="cancelar_cita",
    description="Cancela una cita del cliente, guarda el motivo en el historial y envía email de confirmación.",
)
def cancelar_cita(
    cliente_id: ClienteId,
    calendar_event_id: EventId,
    motivo: Annotated[str, Field(description="Motivo de cancelación proporcionado por el usuario")],
) -> Dict[str, Any]:
    try:
        cita, error = _cita_del_cliente(calendar_event_id, cliente_id)
        if error:
            return error

        service = get_calendar_service()
        try:
            service.events().delete(calendarId=CALENDAR_ID, eventId=calendar_event_id).execute()
        except HttpError as e:
            # Si ya no existe en Calendar (borrado a mano), basta con marcarla cancelada
            if e.resp.status not in (404, 410):
                raise

        db.cancelar_cita(calendar_event_id, motivo)

        # Enviar email de confirmación de cancelación si tenemos correo
        cliente = db.get_cliente(cliente_id) or {}
        notif_result = None
        if cliente.get("email"):
            cuerpo = (
                f"Hola {cliente.get('nombre') or ''}, tu cita ha sido cancelada.\n"
                f"📅 Fecha: {cita['fecha']}\n"
                f"⏰ Hora: {cita['hora']}\n"
                f"🏥 Tratamiento: {cita['tratamiento']}\n"
                f"🗒 Motivo: {motivo}\n\n"
                f"Si necesitas reprogramar, estaré encantado de ayudarte."
            )
            notif_result = _send_email(
                to_email=cliente["email"],
                subject="Confirmación de cancelación de cita",
                body_text=cuerpo,
            )

        return {"ok": True, "mensaje": "Cita cancelada, motivo registrado y email enviado.", "notificacion": notif_result}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool(
    name="confirmar_reserva",
    description="Crea la cita en Calendar, envía confirmación por email y registra al cliente y la cita.",
)
def confirmar_reserva(
    cliente_id: ClienteId,
    nombre: Annotated[str, Field(description="Nombre del cliente")],
    tratamiento: Annotated[str, Field(description="Nombre del tratamiento")],
    fecha: Fecha,
    hora: Hora,
    email: Annotated[Optional[str], Field(description="Email del cliente para la confirmación")] = None,
    consentimiento_marketing: bool = True,
) -> Dict[str, Any]:
    """
    1) Crea la cita en Calendar.
    2) Registra cliente y cita en la base de datos.
    3) Envía confirmación por email con datos de la cita.
    """
    try:
        fecha_iso = _parse_fecha_to_iso(fecha)
        if _is_weekend(fecha_iso):
            return {"ok": False, "error": "No se admiten reservas en fin de semana. Elige de lunes a viernes."}

        # Re-comprobar disponibilidad justo antes de crear (evita dobles reservas)
        error_slot = _comprobar_slot_libre(tratamiento, fecha_iso, hora)
        if error_slot:
            return error_slot

        cita = _crear_cita(nombre=nombre, tratamiento=tratamiento, fecha=fecha_iso, hora=hora)
        if not cita.get("ok"):
            return {"ok": False, "error": cita.get("error", "Error al crear cita")}

        db.registrar_reserva(
            cliente_id=cliente_id,
            nombre=nombre,
            email=email,
            consentimiento_marketing=consentimiento_marketing,
            calendar_event_id=cita["calendar_event_id"],
            tratamiento=tratamiento,
            fecha=fecha_iso,
            hora=hora,
        )

        notif_result = None
        if email:
            resumen = (
                f"Hola {nombre}, tu cita para {tratamiento} queda confirmada.\n"
                f"📅 Fecha: {fecha_iso}\n"
                f"⏰ Hora: {hora}\n"
                f"🔗 Enlace del evento: {cita.get('htmlLink','(no disponible)')}\n\n"
                f"Gracias por confiar en Clínica Pure."
            )
            notif_result = _send_email(
                to_email=email,
                subject=f"Confirmación de cita: {tratamiento} ({fecha_iso} {hora})",
                body_text=resumen,
            )

        return {
            "ok": True,
            "calendar_event_id": cita.get("calendar_event_id"),
            "htmlLink": cita.get("htmlLink"),
            "notificacion": notif_result,
            "cliente": {"id": cliente_id, "email": email},
            "mensaje": "Reserva confirmada y notificación enviada por email.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool(
    name="obtener_cliente",
    description="Devuelve los datos del cliente si está registrado, incluidas sus citas activas (futuras) con calendar_event_id.",
)
def obtener_cliente(cliente_id: ClienteId) -> Dict[str, Any]:
    try:
        cliente = db.get_cliente(cliente_id)
        if not cliente:
            return {"ok": False, "error": "Cliente no registrado."}
        # Citas futuras no canceladas: el modelo necesita el calendar_event_id para modificar/cancelar
        hoy = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
        return {
            "ok": True,
            "cliente": {
                "id": cliente["id"],
                "nombre": cliente["nombre"],
                "email": cliente["email"],
                "consentimiento_marketing": bool(cliente["consentimiento_marketing"]),
                "last_selected": db.ultima_cita(cliente_id) or {},
                "citas_activas": db.citas_activas(cliente_id, desde=hoy),
            }
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# =============================================================================
# HEALTHCHECK
# =============================================================================
@mcp.custom_route("/health", methods=["GET"])
async def health_check(_: Request):
    tools = await mcp.get_tools()
    return JSONResponse({
        "status": "healthy",
        "service": "Clinica MCP",
        "timezone": TIMEZONE,
        "tools": sorted(tools.keys()),
    })

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    # Solo localhost por defecto: el servidor no tiene autenticación
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "3333"))
    print(f"🚀 Servidor MCP corriendo en http://{host}:{port}/mcp")
    mcp.run(transport="http", host=host, port=port, path="/mcp")
