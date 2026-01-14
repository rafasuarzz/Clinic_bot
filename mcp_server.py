
# mcp_server.py
import os
import json
import pickle
import base64
from email.message import EmailMessage
from typing import List, Dict, Any, Optional
from pathlib import Path
from datetime import datetime, timedelta, date as date_cls
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.requests import Request

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest

# =============================================================================
# CONFIG
# =============================================================================
# Scopes combinados: Calendar + Gmail send
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]

CREDENTIALS_PATH = "data/credentials.json"
TOKEN_PATH = "token.pickle"

CALENDAR_ID = "primary"
TIMEZONE = "Atlantic/Canary"  # Canarias (WET/WEST)
HORAS_DISPONIBLES: List[str] = ["10:00", "11:00", "12:00", "16:00", "17:00"]
SLOT_MINUTES = 45

TRATAMIENTOS_JSON = "data/tratamientos.json"
CLIENTES_JSON = "data/clientes.json"

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

def _write_json(path: str, data) -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False

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
@mcp.tool(name="info_tratamiento", description="Devuelve información del tratamiento desde tratamientos.json.")
def info_tratamiento(nombre: str) -> dict:
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

@mcp.tool(name="ver_disponibilidad", description="Devuelve horas libres (slots de 45 min) para un tratamiento y fecha.")
def ver_disponibilidad(tratamiento: str, fecha: str) -> Dict[str, Any]:
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

@mcp.tool(name="crear_cita", description="Crea una cita en Google Calendar (45 min por defecto).")
def crear_cita(nombre: str, tratamiento: str, fecha: str, hora: str) -> Dict[str, Any]:
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

@mcp.tool(name="modificar_cita", description="Modifica una cita existente (cambia fecha/hora en el mismo evento) y envía email.")
def modificar_cita(calendar_event_id: str, nueva_fecha: str, nueva_hora: str) -> Dict[str, Any]:
    try:
        fecha_iso = _parse_fecha_to_iso(nueva_fecha)
        if _is_weekend(fecha_iso):
            return {"ok": False, "error": "No se pueden mover citas a sábado o domingo."}

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

        # Actualizar historial y obtener datos del cliente (nombre/email) para notificación
        db = _read_json(CLIENTES_JSON, default=[])
        now_iso = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        cliente_email = None
        cliente_nombre = None
        prev_fecha, prev_hora = None, None

        for c in db:
            for hst in c.get("historial", []):
                if hst.get("calendar_event_id") == calendar_event_id:
                    prev_fecha = hst.get("fecha")
                    prev_hora = hst.get("hora")
                    hst["fecha"] = fecha_iso
                    hst["hora"] = nueva_hora
                    hst["status"] = "modificada"
                    hst["updated_at"] = now_iso
                    hst["anterior"] = {"fecha": prev_fecha, "hora": prev_hora}
                    cliente_email = c.get("email")
                    cliente_nombre = c.get("nombre")
                    c["updated_at"] = now_iso

        _write_json(CLIENTES_JSON, db)

        # Enviar email de confirmación de cambio si tenemos correo
        notif_result = None
        if cliente_email:
            cuerpo = (
                f"Hola {cliente_nombre or ''}, hemos modificado tu cita.\n"
                f"📝 Antes: {prev_fecha} {prev_hora}\n"
                f"✅ Ahora: {fecha_iso} {nueva_hora}\n"
                f"🔗 Enlace del evento: {updated.get('htmlLink','(no disponible)')}\n\n"
                f"Gracias por confiar en Clínica Pure."
            )
            notif_result = _send_email(
                to_email=cliente_email,
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

@mcp.tool(name="cancelar_cita", description="Cancela una cita existente por eventId y actualiza historial con motivo. Envía email.")
def cancelar_cita(calendar_event_id: str, motivo: Optional[str] = None) -> Dict[str, Any]:
    try:
        service = get_calendar_service()
        service.events().delete(calendarId=CALENDAR_ID, eventId=calendar_event_id).execute()

        # Marcar en historial como cancelada (con motivo) y preparar notificación
        db = _read_json(CLIENTES_JSON, default=[])
        now_iso = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        cliente_email = None
        cliente_nombre = None
        cita_info = None

        for c in db:
            for hst in c.get("historial", []):
                if hst.get("calendar_event_id") == calendar_event_id:
                    hst["status"] = "cancelada"
                    hst["cancelled_at"] = now_iso
                    if motivo:
                        hst["motivo_cancelacion"] = motivo
                    c["updated_at"] = now_iso
                    cliente_email = c.get("email")
                    cliente_nombre = c.get("nombre")
                    cita_info = {"fecha": hst.get("fecha"), "hora": hst.get("hora"), "tratamiento": hst.get("tratamiento")}
        _write_json(CLIENTES_JSON, db)

        # Enviar email de confirmación de cancelación si tenemos correo
        notif_result = None
        if cliente_email:
            cuerpo = (
                f"Hola {cliente_nombre or ''}, tu cita ha sido cancelada.\n"
                f"📅 Fecha: {cita_info.get('fecha') if cita_info else '(desconocida)'}\n"
                f"⏰ Hora: {cita_info.get('hora') if cita_info else '(desconocida)'}\n"
                f"🏥 Tratamiento: {cita_info.get('tratamiento') if cita_info else '(desconocido)'}\n"
                f"🗒 Motivo: {motivo or '(no especificado)'}\n\n"
                f"Si necesitas reprogramar, estaré encantado de ayudarte."
            )
            notif_result = _send_email(
                to_email=cliente_email,
                subject="Confirmación de cancelación de cita",
                body_text=cuerpo,
            )

        return {"ok": True, "mensaje": "Cita cancelada, motivo registrado y email enviado.", "notificacion": notif_result}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool(
    name="confirmar_reserva",
    description="Crea la cita, envía confirmación por email y registra al cliente en clientes.json.",
)
def confirmar_reserva(
    cliente_id: str,
    nombre: str,
    tratamiento: str,
    fecha: str,  # YYYY-MM-DD o DD/MM/AAAA
    hora: str,   # HH:MM
    email: Optional[str] = None,
    consentimiento_marketing: bool = True,
) -> Dict[str, Any]:
    """
    1) Crea la cita en Calendar.
    2) Envía confirmación por email con datos de la cita.
    3) Actualiza clientes.json (status 'confirmada') y añade entrada a historial.
    """
    try:
        fecha_iso = _parse_fecha_to_iso(fecha)
        if _is_weekend(fecha_iso):
            return {"ok": False, "error": "No se admiten reservas en fin de semana. Elige de lunes a viernes."}

        cita = crear_cita.fn(nombre=nombre, tratamiento=tratamiento, fecha=fecha_iso, hora=hora)
        if not cita.get("ok"):
            return {"ok": False, "error": cita.get("error", "Error al crear cita")}

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

        db = _read_json(CLIENTES_JSON, default=[])
        now_iso = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

        cliente = next((c for c in db if c.get("id") == cliente_id), None)
        if not cliente:
            cliente = {
                "id": cliente_id,
                "nombre": nombre,
                "email": email,
                "consentimiento_marketing": consentimiento_marketing,
                "historial": [],
                "last_selected": {},
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            db.append(cliente)
        else:
            cliente["nombre"] = nombre
            cliente["email"] = email
            cliente["consentimiento_marketing"] = consentimiento_marketing
            cliente["updated_at"] = now_iso

        cliente.setdefault("historial", [])
        cliente["historial"].append({
            "tratamiento": tratamiento,
            "fecha": fecha_iso,
            "hora": hora,
            "calendar_event_id": cita.get("calendar_event_id"),
            "status": "confirmada",
            "created_at": now_iso,
        })
        cliente["last_selected"] = {"tratamiento": tratamiento, "fecha": fecha_iso, "hora": hora}

        _write_json(CLIENTES_JSON, db)

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

@mcp.tool(name="obtener_cliente", description="Devuelve el registro del cliente si existe en clientes.json.")
def obtener_cliente(cliente_id: str) -> Dict[str, Any]:
    try:
        db = _read_json(CLIENTES_JSON, default=[])
        cliente = next((c for c in db if c.get("id") == cliente_id), None)
        if not cliente:
            return {"ok": False, "error": "Cliente no registrado."}
        return {
            "ok": True,
            "cliente": {
                "id": cliente.get("id"),
                "nombre": cliente.get("nombre"),
                "email": cliente.get("email"),
                "consentimiento_marketing": cliente.get("consentimiento_marketing", False),
                "last_selected": cliente.get("last_selected", {}),
            }
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# =============================================================================
# RUTAS PERSONALIZADAS (REST)
# =============================================================================
@mcp.custom_route("/tools/info_tratamiento", methods=["POST"])
async def route_info_tratamiento(request: Request):
    try:
        data = await request.json()
        result = info_tratamiento.fn(**data)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

@mcp.custom_route("/tools/ver_disponibilidad", methods=["POST"])
async def route_ver_disponibilidad(request: Request):
    try:
        data = await request.json()
        result = ver_disponibilidad.fn(**data)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

@mcp.custom_route("/tools/crear_cita", methods=["POST"])
async def route_crear_cita(request: Request):
    try:
        data = await request.json()
        result = crear_cita.fn(**data)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

@mcp.custom_route("/tools/modificar_cita", methods=["POST"])
async def route_modificar_cita(request: Request):
    try:
        data = await request.json()
        result = modificar_cita.fn(**data)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

@mcp.custom_route("/tools/cancelar_cita", methods=["POST"])
async def route_cancelar_cita(request: Request):
    try:
        data = await request.json()
        result = cancelar_cita.fn(**data)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

@mcp.custom_route("/tools/confirmar_reserva", methods=["POST"])
async def route_confirmar_reserva(request: Request):
    try:
        data = await request.json()
        result = confirmar_reserva.fn(**data)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

@mcp.custom_route("/tools/obtener_cliente", methods=["POST"])
async def route_obtener_cliente(request: Request):
    try:
        data = await request.json()
        result = obtener_cliente.fn(**data)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

# =============================================================================
# HEALTHCHECK
# =============================================================================
@mcp.custom_route("/health", methods=["GET"])
async def health_check(_: Request):
    return JSONResponse({
        "status": "healthy",
        "service": "Clinica MCP",
        "timezone": TIMEZONE,
        "tools": [
            "info_tratamiento",
            "ver_disponibilidad",
            "crear_cita",
            "modificar_cita",
            "cancelar_cita",
            "confirmar_reserva",
            "obtener_cliente"
        ],
    })

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", "3333"))
    print(f"🚀 MCP HTTP server corriendo en http://localhost:{port}")
    print("📋 Herramientas: info_tratamiento, ver_disponibilidad, crear_cita, modificar_cita, cancelar_cita, confirmar_reserva, obtener_cliente")
    mcp.run(transport="http", host="0.0.0.0", port=port, path="/mcp")
