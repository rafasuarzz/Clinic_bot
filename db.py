"""Capa de datos de la clínica sobre SQLite (clientes y citas)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import settings

ESTADOS = ("confirmada", "modificada", "cancelada")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS clientes (
    id                       TEXT PRIMARY KEY,          -- Telegram user_id
    nombre                   TEXT NOT NULL,
    email                    TEXT,
    telefono                 TEXT,
    consentimiento_marketing INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS citas (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id         TEXT NOT NULL REFERENCES clientes(id),
    calendar_event_id  TEXT NOT NULL UNIQUE,
    tratamiento        TEXT NOT NULL,
    fecha              TEXT NOT NULL,                   -- YYYY-MM-DD (hora local de la clínica)
    hora               TEXT NOT NULL,                   -- HH:MM
    estado             TEXT NOT NULL DEFAULT 'confirmada' CHECK (estado IN {ESTADOS}),
    fecha_anterior     TEXT,
    hora_anterior      TEXT,
    motivo_cancelacion TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    cancelled_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_citas_cliente_fecha ON citas (cliente_id, fecha);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Conexión con transacción: commit al salir, rollback si hay excepción."""
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # lecturas concurrentes mientras se escribe
        conn.executescript(SCHEMA)


def _una(sql: str, params: tuple) -> dict | None:
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _ejecutar(sql: str, params: tuple) -> None:
    with connect() as conn:
        conn.execute(sql, params)


# =============================================================================
# CLIENTES
# =============================================================================
def get_cliente(cliente_id: str) -> dict | None:
    return _una("SELECT * FROM clientes WHERE id = ?", (cliente_id,))


# =============================================================================
# CITAS
# =============================================================================
def registrar_reserva(
    cliente_id: str,
    nombre: str,
    email: str | None,
    consentimiento_marketing: bool,
    calendar_event_id: str,
    tratamiento: str,
    fecha: str,
    hora: str,
) -> None:
    """Crea o actualiza el cliente y añade la cita en una sola transacción."""
    ts = now()
    with connect() as conn:
        # Un cliente que vuelve no pierde su email si no se repite, ni un consentimiento ya dado
        conn.execute(
            """
            INSERT INTO clientes (id, nombre, email, consentimiento_marketing, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                nombre = excluded.nombre,
                email = COALESCE(excluded.email, clientes.email),
                consentimiento_marketing = MAX(clientes.consentimiento_marketing, excluded.consentimiento_marketing),
                updated_at = excluded.updated_at
            """,
            (cliente_id, nombre, email, int(consentimiento_marketing), ts, ts),
        )
        conn.execute(
            """
            INSERT INTO citas (cliente_id, calendar_event_id, tratamiento, fecha, hora, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cliente_id, calendar_event_id, tratamiento, fecha, hora, ts, ts),
        )


def get_cita(calendar_event_id: str) -> dict | None:
    return _una("SELECT * FROM citas WHERE calendar_event_id = ?", (calendar_event_id,))


def citas_activas(cliente_id: str, desde: str) -> list[dict]:
    """Citas no canceladas del cliente con fecha >= desde (YYYY-MM-DD), por orden cronológico."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT calendar_event_id, tratamiento, fecha, hora, estado
            FROM citas
            WHERE cliente_id = ? AND estado != 'cancelada' AND fecha >= ?
            ORDER BY fecha, hora
            """,
            (cliente_id, desde),
        ).fetchall()
    return [dict(r) for r in rows]


def actualizar_fecha_cita(calendar_event_id: str, fecha: str, hora: str) -> None:
    _ejecutar(
        """
        UPDATE citas
        SET fecha_anterior = fecha, hora_anterior = hora,
            fecha = ?, hora = ?, estado = 'modificada', updated_at = ?
        WHERE calendar_event_id = ?
        """,
        (fecha, hora, now(), calendar_event_id),
    )


def cancelar_cita(calendar_event_id: str, motivo: str) -> None:
    ts = now()
    _ejecutar(
        """
        UPDATE citas
        SET estado = 'cancelada', motivo_cancelacion = ?, cancelled_at = ?, updated_at = ?
        WHERE calendar_event_id = ?
        """,
        (motivo, ts, ts, calendar_event_id),
    )
