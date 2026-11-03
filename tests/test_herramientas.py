"""Herramientas MCP de extremo a extremo, llamadas por el protocolo MCP (cliente en memoria)."""

import json

import pytest

import db


async def llamar(mcp, herramienta: str, **args):
    """Devuelve (es_error, datos): el dict de la herramienta o el mensaje de error."""
    resultado = await mcp.call_tool(herramienta, args, raise_on_error=False)
    if resultado.is_error:
        return True, resultado.content[0].text
    return False, resultado.structured_content


async def reservar(mcp, dia, hora="10:00", cliente_id="cli_1", **extra):
    datos = {"nombre": "Ana", "tratamiento": "Botox", "email": "ana@example.com", **extra}
    return await llamar(mcp, "confirmar_reserva", cliente_id=cliente_id, fecha=dia.isoformat(), hora=hora, **datos)


# =============================================================================
# CONSULTAS
# =============================================================================
async def test_lista_las_seis_herramientas(mcp):
    nombres = {t.name for t in await mcp.list_tools()}
    assert nombres == {
        "info_tratamiento",
        "ver_disponibilidad",
        "confirmar_reserva",
        "modificar_cita",
        "cancelar_cita",
        "obtener_cliente",
    }


async def test_info_tratamiento(mcp):
    error, datos = await llamar(mcp, "info_tratamiento", nombre="botox")
    assert not error and datos["nombre"] == "Botox" and datos["precio"] == 250


async def test_ver_disponibilidad_normaliza_la_fecha(mcp, dia_laborable):
    error, datos = await llamar(mcp, "ver_disponibilidad", fecha=dia_laborable.strftime("%d/%m/%Y"))
    assert not error and datos["fecha"] == dia_laborable.isoformat() and "10:00" in datos["horas_disponibles"]


async def test_error_de_negocio_llega_como_error_mcp(mcp):
    error, mensaje = await llamar(mcp, "ver_disponibilidad", fecha="2020-01-07")
    assert error and "ya ha pasado" in mensaje


async def test_cliente_no_registrado(mcp):
    assert await llamar(mcp, "obtener_cliente", cliente_id="nadie") == (False, {"registrado": False})


# =============================================================================
# RESERVAS
# =============================================================================
async def test_reserva_crea_evento_guarda_cita_y_envia_email(mcp, google, dia_laborable):
    error, datos = await reservar(mcp, dia_laborable, tratamiento="botox")
    assert not error
    assert datos["tratamiento"] == "Botox"  # nombre canónico del catálogo
    assert datos["email_enviado"] is True
    assert google.llamadas == [("insert", datos["calendar_event_id"])]
    assert google.emails[0][0] == "ana@example.com"
    assert db.get_cita(datos["calendar_event_id"])["cliente_id"] == "cli_1"


async def test_reserva_sin_email_no_envia_nada(mcp, google, dia_laborable):
    error, datos = await reservar(mcp, dia_laborable, email=None)
    assert not error and datos["email_enviado"] is False and google.emails == []


async def test_no_reserva_una_hora_ocupada(mcp, google, dia_laborable):
    google.ocupar(dia_laborable, "10:00", "10:45")
    error, mensaje = await reservar(mcp, dia_laborable, "10:00")
    assert error and "no está disponible" in mensaje and "11:00" in mensaje
    assert google.llamadas == []  # no se ha creado ningún evento


async def test_no_reserva_fuera_del_horario(mcp, dia_laborable):
    error, mensaje = await reservar(mcp, dia_laborable, "13:00")
    assert error and "no está disponible" in mensaje


async def test_no_reserva_tratamiento_inexistente(mcp, google, dia_laborable):
    error, mensaje = await reservar(mcp, dia_laborable, tratamiento="Liposucción")
    assert error and "No existe el tratamiento" in mensaje and google.llamadas == []


async def test_obtener_cliente_tras_reservar(mcp, dia_laborable):
    _, reserva = await reservar(mcp, dia_laborable)
    error, datos = await llamar(mcp, "obtener_cliente", cliente_id="cli_1")
    assert not error and datos["registrado"] and datos["nombre"] == "Ana"
    assert [c["calendar_event_id"] for c in datos["citas_activas"]] == [reserva["calendar_event_id"]]


# =============================================================================
# CAMBIOS Y CANCELACIONES
# =============================================================================
async def test_modificar_cita(mcp, google, dia_laborable):
    _, reserva = await reservar(mcp, dia_laborable, "10:00")
    ev = reserva["calendar_event_id"]
    error, datos = await llamar(
        mcp,
        "modificar_cita",
        cliente_id="cli_1",
        calendar_event_id=ev,
        nueva_fecha=dia_laborable.isoformat(),
        nueva_hora="16:00",
    )
    assert not error and datos["hora"] == "16:00"
    assert ("patch", ev) in google.llamadas
    cita = db.get_cita(ev)
    assert (cita["hora"], cita["hora_anterior"], cita["estado"]) == ("16:00", "10:00", "modificada")


async def test_cancelar_cita(mcp, google, dia_laborable):
    _, reserva = await reservar(mcp, dia_laborable)
    ev = reserva["calendar_event_id"]
    error, datos = await llamar(mcp, "cancelar_cita", cliente_id="cli_1", calendar_event_id=ev, motivo="Viaje")
    assert not error and datos == {"cancelada": True, "email_enviado": True}
    assert ("delete", ev) in google.llamadas
    assert db.get_cita(ev)["motivo_cancelacion"] == "Viaje"


async def test_no_se_cancela_dos_veces(mcp, dia_laborable):
    _, reserva = await reservar(mcp, dia_laborable)
    args = {"cliente_id": "cli_1", "calendar_event_id": reserva["calendar_event_id"], "motivo": "x"}
    await llamar(mcp, "cancelar_cita", **args)
    error, mensaje = await llamar(mcp, "cancelar_cita", **args)
    assert error and "ya está cancelada" in mensaje


async def test_cancelar_cita_borrada_a_mano_en_calendar(mcp, google, dia_laborable):
    _, reserva = await reservar(mcp, dia_laborable)
    ev = reserva["calendar_event_id"]
    google.borrados_en_calendar.add(ev)
    error, _ = await llamar(mcp, "cancelar_cita", cliente_id="cli_1", calendar_event_id=ev, motivo="x")
    assert not error and db.get_cita(ev)["estado"] == "cancelada"


# =============================================================================
# SEGURIDAD: un cliente no puede tocar las citas de otro
# =============================================================================
@pytest.mark.parametrize(
    ("herramienta", "args"),
    [
        ("cancelar_cita", {"motivo": "x"}),
        ("modificar_cita", {"nueva_fecha": "2099-01-05", "nueva_hora": "10:00"}),
    ],
)
async def test_cita_ajena_no_se_puede_tocar(mcp, google, dia_laborable, herramienta, args):
    _, reserva = await reservar(mcp, dia_laborable, cliente_id="victima")
    llamadas_antes = list(google.llamadas)

    error, mensaje = await llamar(
        mcp, herramienta, cliente_id="intruso", calendar_event_id=reserva["calendar_event_id"], **args
    )

    assert error and "No se encontró esa cita" in mensaje
    assert google.llamadas == llamadas_antes  # ni siquiera se llega a Calendar
    assert db.get_cita(reserva["calendar_event_id"])["estado"] == "confirmada"


async def test_cita_ajena_e_inexistente_dan_el_mismo_error(mcp, dia_laborable):
    """Para no revelar qué IDs de evento existen."""
    _, reserva = await reservar(mcp, dia_laborable, cliente_id="victima")
    _, ajena = await llamar(
        mcp, "cancelar_cita", cliente_id="intruso", calendar_event_id=reserva["calendar_event_id"], motivo="x"
    )
    _, inexistente = await llamar(mcp, "cancelar_cita", cliente_id="intruso", calendar_event_id="no_existe", motivo="x")
    assert ajena == inexistente


async def test_respuesta_es_json_para_el_modelo(mcp):
    """El agente pasa a Claude el texto del resultado tal cual: debe ser JSON válido."""
    resultado = await mcp.call_tool("obtener_cliente", {"cliente_id": "nadie"})
    assert json.loads(resultado.content[0].text) == {"registrado": False}
