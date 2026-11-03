
# db.py
"""Capa de datos de la clínica sobre SQLite (clientes y citas)."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

DB_PATH = os.getenv("CLINICA_DB", str(Path(__file__).resolve().parent / "data" / "clinica.db"))

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Conexión con transacción: commit al salir, rollback si hay excepción."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
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


# =============================================================================
# CLIENTES
# =============================================================================
def get_cliente(cliente_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    return dict(row) if row else None


def upsert_cliente(
    conn: sqlite3.Connection,
    cliente_id: str,
    nombre: str,
    email: Optional[str],
    consentimiento_marketing: bool,
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO clientes (id, nombre, email, consentimiento_marketing, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            nombre = excluded.nombre,
            email = excluded.email,
            consentimiento_marketing = excluded.consentimiento_marketing,
            updated_at = excluded.updated_at
        """,
        (cliente_id, nombre, email, int(consentimiento_marketing), now, now),
    )


# =============================================================================
# CITAS
# =============================================================================
def registrar_reserva(
    cliente_id: str,
    nombre: str,
    email: Optional[str],
    consentimiento_marketing: bool,
    calendar_event_id: str,
    tratamiento: str,
    fecha: str,
    hora: str,
) -> None:
    """Crea/actualiza el cliente y añade la cita en una sola transacción."""
    now = _now()
    with connect() as conn:
        upsert_cliente(conn, cliente_id, nombre, email, consentimiento_marketing)
        conn.execute(
            """
            INSERT INTO citas (cliente_id, calendar_event_id, tratamiento, fecha, hora, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cliente_id, calendar_event_id, tratamiento, fecha, hora, now, now),
        )


def get_cita(calendar_event_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM citas WHERE calendar_event_id = ?", (calendar_event_id,)).fetchone()
    return dict(row) if row else None


def citas_activas(cliente_id: str, desde: str) -> List[Dict[str, Any]]:
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


def ultima_cita(cliente_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT tratamiento, fecha, hora FROM citas WHERE cliente_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (cliente_id,),
        ).fetchone()
    return dict(row) if row else None


def actualizar_fecha_cita(calendar_event_id: str, fecha: str, hora: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE citas
            SET fecha_anterior = fecha, hora_anterior = hora,
                fecha = ?, hora = ?, estado = 'modificada', updated_at = ?
            WHERE calendar_event_id = ?
            """,
            (fecha, hora, _now(), calendar_event_id),
        )


def cancelar_cita(calendar_event_id: str, motivo: str) -> None:
    now = _now()
    with connect() as conn:
        conn.execute(
            """
            UPDATE citas
            SET estado = 'cancelada', motivo_cancelacion = ?, cancelled_at = ?, updated_at = ?
            WHERE calendar_event_id = ?
            """,
            (motivo, now, now, calendar_event_id),
        )
