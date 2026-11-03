"""Agente de la clínica: bucle de tool use de Claude sobre las herramientas del servidor MCP."""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime

from anthropic import AsyncAnthropic
from fastmcp import Client

import settings

log = logging.getLogger(__name__)

# Lee ANTHROPIC_API_KEY del entorno. La URL va fija para que un ANTHROPIC_BASE_URL
# heredado de otra herramienta (p. ej. Claude Code) no desvíe las peticiones.
client = AsyncAnthropic(base_url="https://api.anthropic.com")

MAX_PASOS = 7  # llamadas al modelo por mensaje del usuario

# Parámetro que rellena siempre el bot con el ID real de Telegram: se oculta al modelo
# para que nadie pueda leer, reservar, cambiar ni cancelar como otro usuario.
PARAM_CLIENTE = "cliente_id"

ERROR_SISTEMA = "Lo siento, ahora mismo no puedo acceder al sistema de reservas. Inténtalo de nuevo en unos minutos."

REGLAS = """
REGLAS:
1) Antes de pedir datos personales, llama a 'obtener_cliente'.
   - Si está registrado, NO vuelvas a pedir nombre ni email: reutilízalos.
   - Las confirmaciones se envían SOLO por email.
2) Si preguntan por un tratamiento (precio, duración, contraindicaciones...), usa 'info_tratamiento'.
3) Reserva: pide tratamiento y fecha → 'ver_disponibilidad' → propone las horas libres.
   - Si no está registrado, pide nombre y email antes de confirmar.
   - Llama a 'confirmar_reserva' y responde con el tratamiento, la fecha y la hora.
   - Envía consentimiento_marketing=true solo si el cliente pide expresamente recibir promociones.
4) Cambios y cancelaciones:
   - El 'calendar_event_id' sale de 'citas_activas' en 'obtener_cliente'. Nunca lo pidas ni lo inventes;
     si tiene varias citas, pregunta cuál.
   - Para cambiar una cita, usa 'modificar_cita'.
   - Para cancelar, PREGUNTA primero el motivo y después usa 'cancelar_cita'.
5) Si una herramienta devuelve un error, explícaselo al cliente con tus palabras y propón una alternativa.
6) Tono profesional, respuestas claras y breves. Texto plano, sin Markdown (es un chat de Telegram).
"""


def build_system_prompt() -> str:
    hoy = datetime.now(settings.TZ).strftime("%A %Y-%m-%d")
    tratamientos = ", ".join(t["nombre"] for t in settings.cargar_tratamientos())
    return (
        "Eres el asistente virtual de la Clínica Pure.\n"
        f"- Fecha actual: {hoy} (zona horaria {settings.TIMEZONE})\n"
        f"- Tratamientos disponibles: {tratamientos}\n"
        "- El sistema identifica automáticamente al usuario: las herramientas ya saben quién es.\n" + REGLAS
    )


def preparar_tools(mcp_tools) -> tuple[list[dict], set[str]]:
    """
    Convierte las tools MCP al formato de Claude, quitando cliente_id de los esquemas.
    Devuelve también los nombres de las tools que lo reciben, para que el bot lo inyecte.
    """
    tools, con_cliente = [], set()
    for t in mcp_tools:
        schema = dict(t.inputSchema)
        propiedades = schema.get("properties", {})
        if PARAM_CLIENTE in propiedades:
            con_cliente.add(t.name)
            schema["properties"] = {k: v for k, v in propiedades.items() if k != PARAM_CLIENTE}
            schema["required"] = [r for r in schema.get("required", []) if r != PARAM_CLIENTE]
        tools.append({"name": t.name, "description": t.description or "", "input_schema": schema})
    return tools, con_cliente


def _texto(bloques) -> str:
    return "\n".join(b.text for b in bloques if getattr(b, "type", "") == "text").strip()


class TurnoFallido(Exception):
    """Turno que hay que deshacer, con el mensaje que verá el usuario."""


class ClinicaAgent:
    def __init__(self):
        self.historiales: dict[str, list[dict]] = {}
        # Un mensaje a la vez por usuario, para no mezclar su historial
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def process_message(self, mensaje: str, user_id: str) -> str:
        async with self.locks[user_id]:
            historial = self.historiales.setdefault(user_id, [])
            checkpoint = len(historial)
            historial.append({"role": "user", "content": mensaje})
            try:
                async with Client(settings.MCP_URL) as mcp:
                    tools, con_cliente = preparar_tools(await mcp.list_tools())
                    return await self._run(mcp, tools, con_cliente, historial, user_id)
            except TurnoFallido as e:
                del historial[checkpoint:]
                return str(e)
            except Exception:
                # Deshacer el turno: un tool_use sin su tool_result rompería las siguientes peticiones
                del historial[checkpoint:]
                log.exception("Error procesando el mensaje de %s", user_id)
                return ERROR_SISTEMA

    async def _run(
        self, mcp: Client, tools: list[dict], con_cliente: set[str], historial: list[dict], user_id: str
    ) -> str:
        """
        Bucle de tool use: mientras Claude pida herramientas, se ejecutan en el servidor MCP
        y sus resultados vuelven en un único mensaje 'user'. Al terminar, devuelve su texto.
        """
        system = build_system_prompt()
        for _ in range(MAX_PASOS):
            rsp = await client.beta.messages.create(
                model=settings.MODEL,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=historial,
                output_config={"effort": settings.EFFORT},
                cache_control={"type": "ephemeral"},  # reutiliza prompt + tools + historial entre pasos
                # Si un filtro de seguridad rechaza la petición por error, la reintenta con otro modelo
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if rsp.stop_reason == "refusal":
                log.warning("Rechazo del modelo para %s: %s", user_id, rsp.stop_details)
                raise TurnoFallido("Lo siento, no puedo ayudarte con eso. ¿Te ayudo con algún tratamiento o cita?")

            # Se guarda el contenido completo: thinking y tool_use deben volver tal cual
            historial.append({"role": "assistant", "content": rsp.content})

            tool_uses = [b for b in rsp.content if b.type == "tool_use"]
            if rsp.stop_reason != "tool_use" or not tool_uses:
                return _texto(rsp.content) or "¿Podrías reformular tu solicitud?"

            resultados = [await self._call_tool(mcp, tu, con_cliente, user_id) for tu in tool_uses]
            historial.append({"role": "user", "content": resultados})

        raise TurnoFallido("Lo siento, necesité más pasos de los previstos. ¿Puedes repetir tu última solicitud?")

    async def _call_tool(self, mcp: Client, tool_use, con_cliente: set[str], user_id: str) -> dict:
        """Ejecuta un bloque tool_use en el servidor MCP y devuelve su bloque tool_result."""
        args = {k: v for k, v in (tool_use.input or {}).items() if k != PARAM_CLIENTE}
        if tool_use.name in con_cliente:
            args[PARAM_CLIENTE] = user_id
        log.info("🔧 %s(%s)", tool_use.name, args)
        try:
            # El resultado MCP ya viene como texto (JSON o mensaje de error) listo para el modelo
            result = await mcp.call_tool(tool_use.name, args, raise_on_error=False, timeout=30)
            contenido, is_error = _texto(result.content) or "(sin contenido)", result.is_error
        except Exception as e:
            contenido, is_error = f"No se pudo ejecutar la herramienta '{tool_use.name}': {e}", True
        log.info("🧩 %s → %s%s", tool_use.name, "ERROR " if is_error else "", contenido[:300])
        return {"type": "tool_result", "tool_use_id": tool_use.id, "content": contenido, "is_error": is_error}
