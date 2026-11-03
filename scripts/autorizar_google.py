"""
Autoriza el acceso a Google Calendar y Gmail (OAuth) y guarda el token en data/token.pickle.

Uso (desde la raíz del proyecto), la primera vez o si el token se revoca:
    python scripts/autorizar_google.py
"""

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)  # aviso de google-api-core sobre Python 3.10
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import google_api  # noqa: E402
import settings  # noqa: E402

if __name__ == "__main__":
    google_api.get_credentials(interactive=True)
    print(f"✅ Token guardado en {settings.TOKEN_PATH}")
