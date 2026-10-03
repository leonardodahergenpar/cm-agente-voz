FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=America/Belem
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tini && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src

RUN useradd -m agente && chown -R agente /app
USER agente
WORKDIR /app/src

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "agente.py", "start"]
