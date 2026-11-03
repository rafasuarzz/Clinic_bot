"""Bucle del agente con Claude simulado y el servidor MCP real en memoria."""

import json
from types import SimpleNamespace as NS

import pytest
from fastmcp import Client

import agent
import mcp_server


class FakeClaude:
    """Sustituye a AsyncAnthropic: devuelve respuestas guionizadas y guarda cada petición."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.peticiones: list[dict] = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kwargs):
        # Copia del historial tal como se envió: el agente sigue añadiendo mensajes a la misma lista
        self.peticiones.append({**kwargs, "messages": list(kwargs["messages"])})
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


def usa_herramientas(*llamadas: tuple[str, dict]):
    bloques = [NS(type="tool_use", id=f"tu_{i}", name=n, input=a) for i, (n, a) in enumerate(llamadas)]
    return NS(stop_reason="tool_use", stop_details=None, content=bloques)


def responde(texto: str):
    return NS(stop_reason="end_turn", stop_details=None, content=[NS(type="text", text=texto)])


@pytest.fixture
def claude(monkeypatch):
    def instalar(*respuestas) -> FakeClaude:
        fake = FakeClaude(*respuestas)
        monkeypatch.setattr(agent, "client", fake)
        return fake

    return instalar


@pytest.fixture(autouse=True)
def mcp_en_memoria(monkeypatch):
    monkeypatch.setattr(agent, "Client", lambda url: Client(mcp_server.mcp))


def resultados_de(peticion: dict) -> list[dict]:
    """Bloques tool_result del último mensaje enviado a Claude."""
    return peticion["messages"][-1]["content"]


# =============================================================================
# ESQUEMAS DE LAS HERRAMIENTAS
# =============================================================================
async def test_cliente_id_oculto_al_modelo():
    async with Client(mcp_server.mcp) as mcp:
        tools, con_cliente = agent.preparar_tools(await mcp.list_tools())
    for t in tools:
        assert "cliente_id" not in t["input_schema"]["properties"], t["name"]
        assert "cliente_id" not in t["input_schema"].get("required", []), t["name"]
    assert con_cliente == {"obtener_cliente", "confirmar_reserva", "modificar_cita", "cancelar_cita"}


# =============================================================================
# BUCLE DE TOOL USE
# =============================================================================
async def test_respuesta_directa_sin_herramientas(claude):
    fake = claude(responde("¡Hola!"))
    assert await agent.ClinicaAgent().process_message("hola", "cli_1") == "¡Hola!"
    assert "Fecha actual" in fake.peticiones[0]["system"] and "Botox" in fake.peticiones[0]["system"]


async def test_varias_herramientas_en_un_paso(claude, dia_laborable):
    fake = claude(
        usa_herramientas(("obtener_cliente", {}), ("ver_disponibilidad", {"fecha": dia_laborable.isoformat()})),
        responde("Hay huecos."),
    )
    assert await agent.ClinicaAgent().process_message("¿hay hueco?", "cli_1") == "Hay huecos."

    resultados = resultados_de(fake.peticiones[1])
    assert [r["tool_use_id"] for r in resultados] == ["tu_0", "tu_1"]  # todos en un solo mensaje
    assert not any(r["is_error"] for r in resultados)
    assert json.loads(resultados[0]["content"]) == {"registrado": False}
    assert "10:00" in json.loads(resultados[1]["content"])["horas_disponibles"]


async def test_el_modelo_no_puede_elegir_el_cliente(claude, dia_laborable):
    """Aunque el modelo intente pasar otro cliente_id, se usa siempre el del remitente."""
    fake = claude(
        usa_herramientas(
            (
                "confirmar_reserva",
                {
                    "cliente_id": "victima",
                    "nombre": "Ana",
                    "tratamiento": "Botox",
                    "fecha": dia_laborable.isoformat(),
                    "hora": "10:00",
                },
            )
        ),
        responde("Reservado."),
    )
    await agent.ClinicaAgent().process_message("reserva", "remitente_real")

    reserva = json.loads(resultados_de(fake.peticiones[1])[0]["content"])
    assert mcp_server.db.get_cita(reserva["calendar_event_id"])["cliente_id"] == "remitente_real"
    assert mcp_server.db.get_cliente("victima") is None


async def test_error_de_herramienta_llega_al_modelo_como_error(claude):
    fake = claude(usa_herramientas(("ver_disponibilidad", {"fecha": "2020-01-07"})), responde("Esa fecha ya pasó."))
    await agent.ClinicaAgent().process_message("¿el 7 de enero de 2020?", "cli_1")
    resultado = resultados_de(fake.peticiones[1])[0]
    assert resultado["is_error"] and "ya ha pasado" in resultado["content"]


async def test_el_historial_se_conserva_entre_mensajes(claude):
    fake = claude(responde("Hola, Ana."), responde("Sigo aquí."))
    bot = agent.ClinicaAgent()
    await bot.process_message("Soy Ana", "cli_1")
    await bot.process_message("¿Sigues?", "cli_1")
    roles = [m["role"] for m in fake.peticiones[1]["messages"]]
    assert roles == ["user", "assistant", "user"]


async def test_historiales_separados_por_usuario(claude):
    fake = claude(responde("a"), responde("b"))
    bot = agent.ClinicaAgent()
    await bot.process_message("mensaje de A", "cli_a")
    await bot.process_message("mensaje de B", "cli_b")
    assert len(fake.peticiones[1]["messages"]) == 1  # B no ve la conversación de A


# =============================================================================
# RECUPERACIÓN ANTE FALLOS: el turno se deshace y el historial queda válido
# =============================================================================
async def test_error_de_la_api_deshace_el_turno(claude):
    claude(responde("Hola."), RuntimeError("API caída"))
    bot = agent.ClinicaAgent()
    await bot.process_message("hola", "cli_1")
    antes = list(bot.historiales["cli_1"])

    assert await bot.process_message("otra cosa", "cli_1") == agent.ERROR_SISTEMA
    assert bot.historiales["cli_1"] == antes


async def test_rechazo_del_modelo(claude):
    claude(NS(stop_reason="refusal", stop_details={"category": "test"}, content=[]))
    bot = agent.ClinicaAgent()
    respuesta = await bot.process_message("algo raro", "cli_1")
    assert "no puedo ayudarte" in respuesta and bot.historiales["cli_1"] == []


async def test_demasiados_pasos(claude):
    claude(*[usa_herramientas(("obtener_cliente", {}))] * agent.MAX_PASOS)
    bot = agent.ClinicaAgent()
    respuesta = await bot.process_message("bucle", "cli_1")
    assert "más pasos de los previstos" in respuesta and bot.historiales["cli_1"] == []
