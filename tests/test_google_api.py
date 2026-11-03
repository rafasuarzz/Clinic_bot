"""Cálculo de huecos libres y operaciones de Calendar (con Google simulado)."""

from datetime import datetime, timedelta

import google_api
import settings


def test_dia_libre_devuelve_todo_el_horario(dia_laborable):
    assert google_api.horas_libres(dia_laborable) == list(settings.HORAS_DISPONIBLES)


def test_excluye_horas_que_se_solapan_con_eventos(google, dia_laborable):
    google.ocupar(dia_laborable, "11:30", "12:15")  # pisa la cita de 11:00 (hasta 11:45) y la de 12:00
    assert google_api.horas_libres(dia_laborable) == ["10:00", "16:00", "17:00"]


def test_evento_que_acaba_justo_al_empezar_no_bloquea(google, dia_laborable):
    google.ocupar(dia_laborable, "09:00", "10:00")
    assert "10:00" in google_api.horas_libres(dia_laborable)


def test_dia_pasado_no_tiene_horas_libres():
    ayer = datetime.now(settings.TZ).date() - timedelta(days=1)
    assert google_api.horas_libres(ayer) == []


def test_borrar_evento_que_ya_no_existe_no_falla(google):
    google.borrados_en_calendar.add("ev_borrado_a_mano")
    google_api.borrar_evento("ev_borrado_a_mano")  # 410 Gone: no debe lanzar excepción
    assert ("delete", "ev_borrado_a_mano") in google.llamadas
