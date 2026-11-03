# Una sola imagen para los dos servicios (servidor MCP y bot); docker-compose elige el comando.
FROM python:3.12-slim

ENV TZ=Atlantic/Canary \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias primero: esta capa se reutiliza mientras no cambie requirements.txt
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY settings.py db.py google_api.py mcp_server.py agent.py telegram_bot.py ./
COPY config/ config/
COPY scripts/ scripts/

# Sin privilegios de root; data/ se monta como volumen (BD, token y credenciales de Google)
RUN useradd --create-home --uid 1000 clinica && mkdir -p data && chown clinica:clinica data
USER clinica

CMD ["python", "mcp_server.py"]
