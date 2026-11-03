
"""
Migra data/clientes.json (formato antiguo) a la base de datos SQLite.

Uso (desde la raíz del proyecto):
    python3 scripts/migrar_json_a_sqlite.py [ruta/a/clientes.json]

Es idempotente: los clientes y citas que ya existen no se duplican.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402


def main(json_path: str = "data/clientes.json") -> None:
    datos = json.loads(Path(json_path).read_text(encoding="utf-8"))
    db.init_db()

    n_clientes = n_citas = 0
    with db.connect() as conn:
        for i, c in enumerate(datos):
            # Registros antiguos sin id de Telegram: se conservan con un id sintético
            cliente_id = str(c.get("id") or f"legacy-{i}")
            creado = c.get("created_at") or db._now()
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO clientes
                    (id, nombre, email, telefono, consentimiento_marketing, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cliente_id,
                    c.get("nombre") or "(sin nombre)",
                    c.get("email"),
                    c.get("telefono"),
                    int(bool(c.get("consentimiento_marketing"))),
                    creado,
                    c.get("updated_at") or creado,
                ),
            )
            n_clientes += cur.rowcount

            for h in c.get("historial", []):
                if not h.get("calendar_event_id"):
                    continue
                anterior = h.get("anterior") or {}
                h_creado = h.get("created_at") or creado
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO citas
                        (cliente_id, calendar_event_id, tratamiento, fecha, hora, estado,
                         fecha_anterior, hora_anterior, motivo_cancelacion,
                         created_at, updated_at, cancelled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cliente_id,
                        h["calendar_event_id"],
                        h.get("tratamiento") or "(desconocido)",
                        h.get("fecha"),
                        h.get("hora"),
                        h.get("status") if h.get("status") in db.ESTADOS else "confirmada",
                        anterior.get("fecha"),
                        anterior.get("hora"),
                        h.get("motivo_cancelacion"),
                        h_creado,
                        h.get("updated_at") or h_creado,
                        h.get("cancelled_at"),
                    ),
                )
                n_citas += cur.rowcount

    print(f"✅ Migración completada en {db.DB_PATH}: {n_clientes} clientes y {n_citas} citas nuevas.")


if __name__ == "__main__":
    main(*sys.argv[1:])
