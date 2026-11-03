
"""
Autoriza el acceso a Google Calendar y Gmail (OAuth) y guarda el token en data/token.pickle.

Uso (desde la raíz del proyecto), la primera vez o cuando el token caduque:
    python scripts/autorizar_google.py
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_server  # noqa: E402

from google.auth.exceptions import RefreshError  # noqa: E402

if __name__ == "__main__":
    try:
        mcp_server._get_google_credentials()
    except RefreshError:
        # Token caducado o revocado: se borra y se pide autorización de nuevo
        Path(mcp_server.TOKEN_PATH).unlink(missing_ok=True)
        mcp_server._get_google_credentials()
    print(f"✅ Token guardado en {mcp_server.TOKEN_PATH}")
