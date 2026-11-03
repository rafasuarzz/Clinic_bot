"""
Servidor MCP de la clínica: herramientas para consultar tratamientos y gestionar citas.

Los errores de negocio (fecha no válida, hora ocupada, cita ajena...) se lanzan como
ToolError: el cliente MCP los recibe como resultado de error con el mensaje para el modelo.
"""

import logging
import unicodedata
from datetime import date, datetime
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

import db
import google_api
import settings

log = logging.getLogger(__name__)

db.init_db()
mcp = FastMCP("Clinica MCP")

# Tipos con descripción: FastMCP los publica en el esquema que ve el modelo
Fecha = Annotated[str, Field(description="Fecha (YYYY-MM-DD o DD/MM/AAAA)")]
Hora = Annotated[str, Field(description="Hora en formato HH:MM")]
EventId = Annotated[str, Field(description="calendar_event_id de la cita (de 'citas_activas' en obtener_cliente)")]
ClienteId = Annotated[str, Field(description="Identificador estable del cliente (Telegram user_id)")]
Tratamiento = Annotated[str, Field(description="Nombre del tratamiento")]


# =============================================================================
# VALIDACIONES Y REGLAS DE NEGOCIO
# =============================================================================
def _normalizar(texto: str) -> str:
    """Minúsculas y sin acentos: 'Ácido hialurónico' == 'acido hialuronico'."""
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sin_acentos.lower().split())


def _buscar_tratamiento(nombre: str) -> dict:
    buscado = _normalizar(nombre)
    for t in settings.cargar_tratamientos():
        if _normalizar(t["nombre"]) == buscado:
            return t
    disponibles = ", ".join(t["nombre"] for t in settings.cargar_tratamientos())
    raise ToolError(f"No existe el tratamiento '{nombre}'. Disponibles: {disponibles}.")


def _validar_fecha(fecha: str) -> date:
    """Convierte la fecha a date y comprueba que sea un día laborable y no haya pasado."""
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            dia = datetime.strptime(fecha.strip(), formato).date()
            break
        except ValueError:
            continue
    else:
        raise ToolError(f"Fecha no válida: '{fecha}'. Usa YYYY-MM-DD o DD/MM/AAAA.")

    if dia.weekday() >= 5:
        raise ToolError("La clínica no abre fines de semana. Elige un día de lunes a viernes.")
    if dia < datetime.now(settings.TZ).date():
        raise ToolError(f"El {dia.isoformat()} ya ha pasado. Elige una fecha futura.")
    return dia


def _exigir_hora_libre(dia: date, hora: str) -> None:
    """Se comprueba justo antes de escribir en Calendar: la hora pudo ocuparse durante la conversación."""
    libres = google_api.horas_libres(dia)
    if hora not in libres:
        raise ToolError(
            f"La hora {hora} del {dia.isoformat()} no está disponible. "
            f"Horas libres ese día: {', '.join(libres) or 'ninguna'}."
        )


def _cita_del_cliente(calendar_event_id: str, cliente_id: str) -> dict:
    """
    Devuelve la cita si existe, es del cliente y no está cancelada. Mismo mensaje para
    "no existe" y "es de otro cliente", para no revelar qué IDs existen.
    """
    cita = db.get_cita(calendar_event_id)
    if not cita or cita["cliente_id"] != cliente_id:
        raise ToolError("No se encontró esa cita entre las citas del cliente.")
    if cita["estado"] == "cancelada":
        raise ToolError("Esa cita ya está cancelada.")
    return cita


def _notificar(email: str | None, asunto: str, cuerpo: str) -> bool:
    """Envía el email si hay dirección. Un fallo de Gmail no deshace la operación ya hecha."""
    if not email:
        return False
    try:
        google_api.enviar_email(email, asunto, cuerpo)
        return True
    except Exception:
        log.exception("No se pudo enviar el email a %s", email)
        return False


# =============================================================================
# HERRAMIENTAS MCP
# =============================================================================
@mcp.tool(
    name="info_tratamiento",
    description="Devuelve la ficha del tratamiento (duración, precio, descripción, indicaciones, contraindicaciones).",
)
def info_tratamiento(nombre: Tratamiento) -> dict:
    return _buscar_tratamiento(nombre)


@mcp.tool(
    name="ver_disponibilidad",
    description="Consulta en Google Calendar las horas libres (citas de 45 min) para una fecha.",
)
def ver_disponibilidad(fecha: Fecha) -> dict:
    dia = _validar_fecha(fecha)
    return {"fecha": dia.isoformat(), "horas_disponibles": google_api.horas_libres(dia)}


@mcp.tool(
    name="confirmar_reserva",
    description="Crea la cita en Calendar, registra al cliente y la cita, y envía la confirmación por email.",
)
def confirmar_reserva(
    cliente_id: ClienteId,
    nombre: Annotated[str, Field(description="Nombre del cliente")],
    tratamiento: Tratamiento,
    fecha: Fecha,
    hora: Hora,
    email: Annotated[str | None, Field(description="Email del cliente para la confirmación")] = None,
    consentimiento_marketing: Annotated[
        bool, Field(description="Solo true si el cliente ha aceptado expresamente recibir promociones")
    ] = False,
) -> dict:
    tratamiento = _buscar_tratamiento(tratamiento)["nombre"]  # nombre canónico del catálogo
    dia = _validar_fecha(fecha)
    _exigir_hora_libre(dia, hora)

    evento = google_api.crear_evento(
        dia, hora, titulo=f"🏥 {tratamiento}", descripcion=f"Cliente: {nombre}\nTratamiento: {tratamiento}"
    )
    db.registrar_reserva(
        cliente_id=cliente_id,
        nombre=nombre,
        email=email,
        consentimiento_marketing=consentimiento_marketing,
        calendar_event_id=evento["id"],
        tratamiento=tratamiento,
        fecha=dia.isoformat(),
        hora=hora,
    )
    email_enviado = _notificar(
        email,
        f"Confirmación de cita: {tratamiento} ({dia.isoformat()} {hora})",
        f"Hola {nombre}, tu cita para {tratamiento} queda confirmada.\n"
        f"📅 Fecha: {dia.isoformat()}\n"
        f"⏰ Hora: {hora}\n\n"
        f"Gracias por confiar en Clínica Pure.",
    )
    return {
        "calendar_event_id": evento["id"],
        "tratamiento": tratamiento,
        "fecha": dia.isoformat(),
        "hora": hora,
        "email_enviado": email_enviado,
    }


@mcp.tool(
    name="modificar_cita",
    description="Cambia la fecha/hora de una cita del cliente y envía email de confirmación del cambio.",
)
def modificar_cita(cliente_id: ClienteId, calendar_event_id: EventId, nueva_fecha: Fecha, nueva_hora: Hora) -> dict:
    cita = _cita_del_cliente(calendar_event_id, cliente_id)
    dia = _validar_fecha(nueva_fecha)
    _exigir_hora_libre(dia, nueva_hora)

    google_api.mover_evento(calendar_event_id, dia, nueva_hora)
    db.actualizar_fecha_cita(calendar_event_id, dia.isoformat(), nueva_hora)

    cliente = db.get_cliente(cliente_id)
    email_enviado = _notificar(
        cliente["email"],
        f"Confirmación de cambio de cita ({dia.isoformat()} {nueva_hora})",
        f"Hola {cliente['nombre']}, hemos modificado tu cita de {cita['tratamiento']}.\n"
        f"📝 Antes: {cita['fecha']} {cita['hora']}\n"
        f"✅ Ahora: {dia.isoformat()} {nueva_hora}\n\n"
        f"Gracias por confiar en Clínica Pure.",
    )
    return {
        "calendar_event_id": calendar_event_id,
        "fecha": dia.isoformat(),
        "hora": nueva_hora,
        "email_enviado": email_enviado,
    }


@mcp.tool(
    name="cancelar_cita",
    description="Cancela una cita del cliente, guarda el motivo y envía email de confirmación.",
)
def cancelar_cita(
    cliente_id: ClienteId,
    calendar_event_id: EventId,
    motivo: Annotated[str, Field(description="Motivo de cancelación proporcionado por el usuario")],
) -> dict:
    cita = _cita_del_cliente(calendar_event_id, cliente_id)

    google_api.borrar_evento(calendar_event_id)
    db.cancelar_cita(calendar_event_id, motivo)

    cliente = db.get_cliente(cliente_id)
    email_enviado = _notificar(
        cliente["email"],
        "Confirmación de cancelación de cita",
        f"Hola {cliente['nombre']}, tu cita ha sido cancelada.\n"
        f"📅 Fecha: {cita['fecha']}\n"
        f"⏰ Hora: {cita['hora']}\n"
        f"🏥 Tratamiento: {cita['tratamiento']}\n"
        f"🗒 Motivo: {motivo}\n\n"
        f"Si necesitas reprogramar, estaremos encantados de ayudarte.",
    )
    return {"cancelada": True, "email_enviado": email_enviado}


@mcp.tool(
    name="obtener_cliente",
    description="Indica si el cliente está registrado y devuelve sus datos y sus citas futuras con calendar_event_id.",
)
def obtener_cliente(cliente_id: ClienteId) -> dict:
    cliente = db.get_cliente(cliente_id)
    if not cliente:
        return {"registrado": False}
    hoy = datetime.now(settings.TZ).date().isoformat()
    return {
        "registrado": True,
        "nombre": cliente["nombre"],
        "email": cliente["email"],
        "consentimiento_marketing": bool(cliente["consentimiento_marketing"]),
        "citas_activas": db.citas_activas(cliente_id, desde=hoy),
    }


# =============================================================================
# HEALTHCHECK Y ARRANQUE
# =============================================================================
@mcp.custom_route("/health", methods=["GET"])
async def health_check(_: Request) -> JSONResponse:
    tools = await mcp.get_tools()
    return JSONResponse({"status": "healthy", "service": mcp.name, "tools": sorted(tools)})


if __name__ == "__main__":
    settings.configurar_logging()
    mcp.run(transport="http", host=settings.MCP_HOST, port=settings.MCP_PORT, path="/mcp")
