"""Capa de datos SQLite."""

import sqlite3

import pytest

import db


def _reservar(event_id="ev_1", cliente_id="cli_1", email="ana@example.com", consentimiento=False, fecha="2099-01-05"):
    db.registrar_reserva(cliente_id, "Ana", email, consentimiento, event_id, "Botox", fecha, "10:00")


def test_registrar_reserva_crea_cliente_y_cita():
    _reservar()
    assert db.get_cliente("cli_1")["email"] == "ana@example.com"
    cita = db.get_cita("ev_1")
    assert (cita["cliente_id"], cita["tratamiento"], cita["estado"]) == ("cli_1", "Botox", "confirmada")


def test_cliente_que_vuelve_sin_email_lo_conserva():
    _reservar("ev_1")
    _reservar("ev_2", email=None)
    assert db.get_cliente("cli_1")["email"] == "ana@example.com"


def test_consentimiento_ya_dado_no_se_pierde():
    _reservar("ev_1", consentimiento=True)
    _reservar("ev_2", consentimiento=False)
    assert db.get_cliente("cli_1")["consentimiento_marketing"] == 1


def test_consentimiento_por_defecto_desactivado():
    _reservar()
    assert db.get_cliente("cli_1")["consentimiento_marketing"] == 0


def test_citas_activas_excluye_canceladas_y_pasadas():
    _reservar("ev_futura", fecha="2099-01-05")
    _reservar("ev_pasada", fecha="2020-01-07")
    _reservar("ev_cancelada", fecha="2099-01-06")
    db.cancelar_cita("ev_cancelada", "motivo")
    activas = db.citas_activas("cli_1", desde="2026-01-01")
    assert [c["calendar_event_id"] for c in activas] == ["ev_futura"]


def test_citas_activas_ordenadas_por_fecha():
    _reservar("ev_b", fecha="2099-03-01")
    _reservar("ev_a", fecha="2099-02-01")
    assert [c["calendar_event_id"] for c in db.citas_activas("cli_1", desde="2026-01-01")] == ["ev_a", "ev_b"]


def test_actualizar_fecha_guarda_la_anterior():
    _reservar()
    db.actualizar_fecha_cita("ev_1", "2099-01-06", "12:00")
    cita = db.get_cita("ev_1")
    assert (cita["fecha"], cita["hora"], cita["estado"]) == ("2099-01-06", "12:00", "modificada")
    assert (cita["fecha_anterior"], cita["hora_anterior"]) == ("2099-01-05", "10:00")


def test_cancelar_guarda_motivo_y_fecha():
    _reservar()
    db.cancelar_cita("ev_1", "Viaje")
    cita = db.get_cita("ev_1")
    assert cita["estado"] == "cancelada" and cita["motivo_cancelacion"] == "Viaje" and cita["cancelled_at"]


def test_event_id_duplicado_no_deja_reserva_a_medias():
    """La reserva es una transacción: si la cita falla, tampoco se crea el cliente."""
    _reservar("ev_1", cliente_id="cli_1")
    with pytest.raises(sqlite3.IntegrityError):
        _reservar("ev_1", cliente_id="cli_2")
    assert db.get_cliente("cli_2") is None


def test_estado_invalido_rechazado_por_la_bd():
    _reservar()
    with pytest.raises(sqlite3.IntegrityError):
        db._ejecutar("UPDATE citas SET estado = 'perdida' WHERE calendar_event_id = ?", ("ev_1",))
