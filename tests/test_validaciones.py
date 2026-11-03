"""Reglas de negocio puras: fechas y catálogo de tratamientos."""

from datetime import datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError

import settings
from mcp_server import _buscar_tratamiento, _normalizar, _validar_fecha


def _proximo(weekday: int):
    hoy = datetime.now(settings.TZ).date()
    return hoy + timedelta(days=(weekday - hoy.weekday()) % 7 or 7)


class TestValidarFecha:
    def test_acepta_formato_iso(self, dia_laborable):
        assert _validar_fecha(dia_laborable.isoformat()) == dia_laborable

    def test_acepta_formato_espanol(self, dia_laborable):
        assert _validar_fecha(dia_laborable.strftime("%d/%m/%Y")) == dia_laborable

    def test_ignora_espacios(self, dia_laborable):
        assert _validar_fecha(f"  {dia_laborable.isoformat()} ") == dia_laborable

    @pytest.mark.parametrize("weekday", [5, 6], ids=["sabado", "domingo"])
    def test_rechaza_fin_de_semana(self, weekday):
        with pytest.raises(ToolError, match="fines de semana"):
            _validar_fecha(_proximo(weekday).isoformat())

    def test_rechaza_fecha_pasada(self):
        with pytest.raises(ToolError, match="ya ha pasado"):
            _validar_fecha("2020-01-07")

    @pytest.mark.parametrize("fecha", ["mañana", "31/02/2027", "2027-13-01", "", "10-11-2027"])
    def test_rechaza_fechas_no_validas(self, fecha):
        with pytest.raises(ToolError, match="Fecha no válida"):
            _validar_fecha(fecha)


class TestTratamientos:
    def test_normalizar_quita_acentos_mayusculas_y_espacios(self):
        assert _normalizar("  Ácido   HIALURÓNICO ") == "acido hialuronico"

    @pytest.mark.parametrize("nombre", ["acido hialuronico", "ÁCIDO HIALURÓNICO", "Ácido hialurónico"])
    def test_busca_sin_importar_acentos(self, nombre):
        assert _buscar_tratamiento(nombre)["nombre"] == "Ácido hialurónico"

    def test_tratamiento_inexistente_lista_los_disponibles(self):
        with pytest.raises(ToolError, match="Disponibles: .*Botox"):
            _buscar_tratamiento("Liposucción")

    def test_catalogo_tiene_los_campos_necesarios(self):
        for t in settings.cargar_tratamientos():
            assert {"nombre", "duracion", "precio"} <= t.keys(), t
