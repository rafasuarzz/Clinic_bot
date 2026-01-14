
# agent.py
import os
import json
import requests
from datetime import datetime, date
from typing import Dict, Any, List
from zoneinfo import ZoneInfo              # <-- NUEVO
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

MODEL = "gpt-4.1"

MCP_URL = f"http://localhost:{os.getenv('MCP_PORT', '3333')}"
TIMEZONE = os.getenv("TIMEZONE", "Atlantic/Canary")  # <-- NUEVO (alineado con mcp_server.py)

def build_system_prompt(tratamientos: List[str], cliente_id: str) -> str:
    # fecha actual en zona horaria de la clínica
    hoy = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()   # <-- FIX
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
   - Para CAMBIAR una cita existente, usa 'modificar_cita' con 'calendar_event_id', 'nueva_fecha', 'nueva_hora'.
     (El servidor enviará email de confirmación del cambio.)
   - Para CANCELAR una cita, PREGUNTA primero el MOTIVO de cancelación (obligatorio) y luego usa 'cancelar_cita'
     con 'calendar_event_id' y 'motivo'. El servidor enviará email de confirmación de cancelación.

6) Tono profesional, respuestas claras y breves.
"""


# ===============================
# HERRAMIENTAS (formato Responses API)
# ===============================
TOOLS = [
    {
        "type": "function",
        "name": "info_tratamiento",
        "description": "Devuelve la ficha del tratamiento (duración, precio, descripción) desde tratamientos.json.",
        "parameters": {
            "type": "object",
            "properties": {"nombre": {"type": "string"}},
            "required": ["nombre"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "ver_disponibilidad",
        "description": "Consulta las horas disponibles en Google Calendar para un tratamiento en una fecha específica",
        "parameters": {
            "type": "object",
            "properties": {
                "tratamiento": {"type": "string", "description": "Nombre del tratamiento"},
                "fecha": {"type": "string", "description": "Fecha (YYYY-MM-DD o DD/MM/AAAA)"},
            },
            "required": ["tratamiento", "fecha"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "confirmar_reserva",
        "description": "Crea la cita, envía confirmación por email, y registra al cliente.",
        "parameters": {
            "type": "object",
            "properties": {
                "cliente_id": {"type": "string", "description": "Identificador estable del cliente (p.ej. Telegram user_id)"},
                "nombre": {"type": "string"},
                "tratamiento": {"type": "string"},
                "fecha": {"type": "string", "description": "YYYY-MM-DD"},
                "hora": {"type": "string", "description": "HH:MM"},
                "email": {"type": ["string", "null"]},
                "consentimiento_marketing": {"type": "boolean"},
            },
            "required": ["cliente_id", "nombre", "tratamiento", "fecha", "hora", "email"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "modificar_cita",
        "description": "Modifica una cita existente cambiando fecha/hora (actualiza evento de Calendar y envía email).",
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_event_id": {"type": "string"},
                "nueva_fecha": {"type": "string", "description": "YYYY-MM-DD o DD/MM/AAAA"},
                "nueva_hora": {"type": "string", "description": "HH:MM"},
            },
            "required": ["calendar_event_id", "nueva_fecha", "nueva_hora"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "obtener_cliente",
        "description": "Devuelve el registro de clientes.json por cliente_id si existe.",
        "parameters": {
            "type": "object",
            "properties": {
                "cliente_id": {"type": "string", "description": "Identificador estable del cliente (p.ej. Telegram user_id)"}
            },
            "required": ["cliente_id"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "cancelar_cita",
        "description": "Cancela una cita existente por calendar_event_id y guarda el motivo. Envía email de confirmación.",
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_event_id": {"type": "string"},
                "motivo": {"type": "string", "description": "Motivo de cancelación proporcionado por el usuario"},
            },
            "required": ["calendar_event_id", "motivo"],
            "additionalProperties": False,
        },
        "strict": False,
    },
]

def _get_last_assistant_text(response) -> str:
    """Devuelve el texto del ÚLTIMO item 'message' generado por el modelo."""
    try:
        msg_items = [it for it in response.output if getattr(it, "type", "") == "message"]
        if not msg_items:
            return (response.output_text or "").strip()
        last_msg = msg_items[-1]
        parts = []
        for c in getattr(last_msg, "content", []):
            if getattr(c, "type", "") == "output_text":
                parts.append(getattr(c, "text", "") or "")
        return "\n".join(p for p in parts if p).strip()
    except Exception:
        return ""

class ClinicaAgent:
    def __init__(self, tratamientos: List[str]):
        self.tratamientos = tratamientos
        self.context: Dict[str, List[Any]] = {}

    def _init_user(self, user_id: str):
        if user_id not in self.context:
            self.context[user_id] = [{"role": "system", "content": build_system_prompt(self.tratamientos, user_id)}]

    def _call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]):
        """Llama al servidor MCP por HTTP en /tools/{tool_name}."""
        try:
            url = f"{MCP_URL}/tools/{tool_name}"
            response = requests.post(url, json=arguments, headers={"Content-Type": "application/json"}, timeout=20)
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"Error del servidor MCP: {response.status_code}", "details": response.text}
        except Exception as e:
            return {"error": f"No se pudo conectar al MCP: {str(e)}"}

    def process_message(self, user_message: str, user_id: str) -> str:
        """
        1) Enviamos contexto + mensaje del usuario → el modelo puede devolver function_call(s).
        2) Ejecutamos las funciones y añadimos function_call_output con el call_id correcto.
        3) Repetimos hasta que no haya más function_call; devolvemos el último 'message'.
        """
        self._init_user(user_id)
        self.context[user_id].append({"role": "user", "content": user_message})

        max_iterations = 7
        for _ in range(max_iterations):
            rsp = client.responses.create(
                model=MODEL,
                input=self.context[user_id],
                tools=TOOLS,
                temperature=0.2,
                parallel_tool_calls=False,
                tool_choice={
                    "type": "allowed_tools",
                    "mode": "auto",
                    "tools": [
                        {"type": "function", "name": "info_tratamiento"},
                        {"type": "function", "name": "ver_disponibilidad"},
                        {"type": "function", "name": "confirmar_reserva"},
                        {"type": "function", "name": "modificar_cita"},
                        {"type": "function", "name": "obtener_cliente"},
                        {"type": "function", "name": "cancelar_cita"},
                    ],
                },
            )

            # Añadimos lo generado por el modelo al contexto
            self.context[user_id] += rsp.output

            # ¿Hubo llamadas a función?
            function_calls = [it for it in rsp.output if getattr(it, "type", "") == "function_call"]
            if not function_calls:
                final_text = _get_last_assistant_text(rsp) or "¿Podrías reformular tu solicitud?"
                self.context[user_id].append({"role": "assistant", "content": final_text})
                return final_text

            # Ejecutamos cada tool y devolvemos su output enlazándolo por call_id
            for fc in function_calls:
                fn_name = fc.name
                try:
                    args = fc.arguments if isinstance(fc.arguments, dict) else json.loads(fc.arguments or "{}")
                except Exception:
                    args = {}
                print(f"🔧 Llamando a {fn_name} con {args}")
                result = self._call_mcp_tool(fn_name, args)
                print(f"🧩 Resultado MCP {fn_name} → {result}")
                payload = {"ok": False, "error": result.get("error")} if isinstance(result, dict) and "error" in result \
                         else {"ok": True, "data": result}
                self.context[user_id].append({
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": json.dumps(payload, ensure_ascii=False),
                })

        return "Lo siento, necesité más pasos de los previstos. ¿Puedes repetir tu última solicitud?"
