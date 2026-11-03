
# agent.py
import os
import json
import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, List, Tuple
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from fastmcp import Client

# Lee ANTHROPIC_API_KEY del entorno (.env). La URL va fija para que un ANTHROPIC_BASE_URL
# heredado de otra herramienta (p. ej. Claude Code) no desvíe las peticiones.
client = AsyncAnthropic(base_url="https://api.anthropic.com")

# Prefijo CLINICA_ para no chocar con variables de otras herramientas (p. ej. CLAUDE_* de Claude Code)
MODEL = os.getenv("CLINICA_MODEL", "claude-sonnet-5-5")
EFFORT = os.getenv("CLINICA_EFFORT", "low")  # low | medium | high: más alto = más razonamiento y más coste

MCP_URL = os.getenv("MCP_URL", f"http://localhost:{os.getenv('MCP_PORT', '3333')}/mcp")
TIMEZONE = os.getenv("TIMEZONE", "Atlantic/Canary")

def build_system_prompt(tratamientos: List[str], cliente_id: str) -> str:
    # fecha actual en zona horaria de la clínica
    hoy = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    tratamientos_txt = ", ".join(tratamientos)
    return f"""
Eres el asistente virtual de la Clínica Pure.
INFORMACIÓN:
- Fecha actual: {hoy}
- Zona horaria: {TIMEZONE}
- Tratamientos disponibles: {tratamientos_txt}
- cliente_id actual (ID estable del usuario): {cliente_id}

REGLAS:
0) Antes de pedir datos personales, llama a la herramienta 'obtener_cliente' con el cliente_id.
   - Si el cliente existe, NO vuelvas a pedir nombre ni contacto. Reutiliza su nombre y su email.
   - La confirmación y promociones se envían SOLO por email.

1) Fechas:
   - Acepta 'YYYY-MM-DD' o 'DD/MM/AAAA'. Internamente usa 'YYYY-MM-DD'.
   - No agendes fines de semana (sábado o domingo). Si el servidor lo rechaza, pide otra fecha hábil.

2) Información previa:
   - Si preguntan por un tratamiento (precio, duración, descripción, contraindicaciones),
     usa 'info_tratamiento' (solo para consultas previas).

3) Reserva:
   - Pide tratamiento y fecha → llama 'ver_disponibilidad' → propone horas.
   - Para confirmar, si NO hay registro previo pide nombre y email. Si SÍ hay, no los pidas.

4) Confirmación:
   - Llama a 'confirmar_reserva' SIEMPRE incluyendo 'cliente_id', 'nombre', 'tratamiento', 'fecha', 'hora' y 'email'.
   - Incluye en la respuesta fecha/hora y el nombre del tratamiento.

5) Cambios:
   - El 'calendar_event_id' de cada cita lo obtienes de 'citas_activas' en 'obtener_cliente'.
     Nunca lo pidas al usuario ni lo inventes. Si tiene varias citas, pregunta cuál.
   - Para CAMBIAR una cita existente, usa 'modificar_cita' con 'calendar_event_id', 'nueva_fecha', 'nueva_hora'.
     (El servidor enviará email de confirmación del cambio.)
   - Para CANCELAR una cita, PREGUNTA primero el MOTIVO de cancelación (obligatorio) y luego usa 'cancelar_cita'
     con 'calendar_event_id' y 'motivo'. El servidor enviará email de confirmación de cancelación.

6) Tono profesional, respuestas claras y breves. Escribe texto plano (es un chat de Telegram, sin Markdown).
"""


# ===============================
# HERRAMIENTAS (se cargan del servidor MCP)
# ===============================
def _mcp_tool_to_claude(tool) -> Dict[str, Any]:
    """Convierte una tool MCP (list_tools) al formato de herramientas de la API de Claude."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }

def _result_to_payload(result) -> Dict[str, Any]:
    """Convierte un CallToolResult de MCP en el dict que ve el modelo."""
    text = "\n".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")
    if result.is_error:
        return {"ok": False, "error": text or "Error en la herramienta"}
    if isinstance(result.structured_content, dict):
        return result.structured_content
    try:
        return json.loads(text)
    except Exception:
        return {"ok": True, "data": text}

class TurnoFallido(Exception):
    """Turno que hay que deshacer, con el mensaje que verá el usuario."""

def _final_text(response) -> str:
    """Une los bloques de texto de la respuesta (ignora thinking y tool_use)."""
    return "\n".join(b.text for b in response.content if b.type == "text").strip()

class ClinicaAgent:
    def __init__(self, tratamientos: List[str]):
        self.tratamientos = tratamientos
        self.context: Dict[str, List[Dict[str, Any]]] = {}
        # Un mensaje a la vez por usuario, para no mezclar su historial
        self.locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _call_tool(self, mcp: Client, name: str, args: Dict[str, Any], user_id: str) -> Tuple[Dict[str, Any], bool]:
        """Ejecuta la tool en el servidor MCP. Devuelve (payload, is_error)."""
        # El cliente_id lo pone el bot, no el modelo: así nadie puede leer o reservar como otro usuario
        if "cliente_id" in args:
            args["cliente_id"] = user_id
        try:
            result = await mcp.call_tool(name, args, raise_on_error=False, timeout=30)
            return _result_to_payload(result), bool(result.is_error)
        except Exception as e:
            return {"ok": False, "error": f"Fallo al llamar a la herramienta MCP '{name}': {e}"}, True

    async def process_message(self, user_message: str, user_id: str) -> str:
        """
        1) Enviamos historial + mensaje del usuario → Claude puede responder con bloques tool_use.
        2) Ejecutamos cada tool en el servidor MCP y devolvemos los tool_result en un único mensaje 'user'.
        3) Repetimos hasta que Claude termine (stop_reason != 'tool_use') y devolvemos su texto.
        """
        async with self.locks[user_id]:
            history = self.context.setdefault(user_id, [])
            checkpoint = len(history)
            history.append({"role": "user", "content": user_message})
            try:
                async with Client(MCP_URL) as mcp:
                    tools = [_mcp_tool_to_claude(t) for t in await mcp.list_tools()]
                    system = build_system_prompt(self.tratamientos, user_id)
                    return await self._run(mcp, tools, system, history, user_id)
            except TurnoFallido as e:
                del history[checkpoint:]
                print(f"⚠️ Turno deshecho para {user_id}: {e.__cause__ or e}")
                return str(e)
            except Exception as e:
                # Deshacer el turno: un tool_use sin su tool_result rompería las siguientes peticiones
                del history[checkpoint:]
                print(f"❌ Error procesando mensaje de {user_id}: {e!r}")
                return "Lo siento, ahora mismo no puedo acceder al sistema de reservas. Inténtalo de nuevo en unos minutos."

    async def _run(self, mcp: Client, tools: List[Dict[str, Any]], system: str, history: List[Dict[str, Any]], user_id: str) -> str:
        max_iterations = 7
        for _ in range(max_iterations):
            rsp = await client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=history,
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},  # reutiliza prompt + tools + historial entre pasos
                # Si un filtro de seguridad rechaza la petición por error, la reintenta con otro modelo
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )

            if rsp.stop_reason == "refusal":
                print(f"⚠️ Rechazo del modelo: {rsp.stop_details}")
                raise TurnoFallido("Lo siento, no puedo ayudarte con eso. ¿Puedo ayudarte con algún tratamiento o cita?")

            # Guardar el contenido completo (incluye thinking y tool_use, que deben volver tal cual)
            history.append({"role": "assistant", "content": rsp.content})

            tool_uses = [b for b in rsp.content if b.type == "tool_use"]
            if rsp.stop_reason != "tool_use" or not tool_uses:
                return _final_text(rsp) or "¿Podrías reformular tu solicitud?"

            tool_results = []
            for tu in tool_uses:
                args = dict(tu.input or {})
                print(f"🔧 Llamando a {tu.name} con {args}")
                payload, is_error = await self._call_tool(mcp, tu.name, args, user_id)
                print(f"🧩 Resultado MCP {tu.name} → {payload}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(payload, ensure_ascii=False),
                    "is_error": is_error,
                })
            # Todos los resultados en un único mensaje
            history.append({"role": "user", "content": tool_results})

        # Se agotaron los pasos con un tool_use pendiente: deshacerlo para no romper el historial
        raise TurnoFallido("Lo siento, necesité más pasos de los previstos. ¿Puedes repetir tu última solicitud?")
