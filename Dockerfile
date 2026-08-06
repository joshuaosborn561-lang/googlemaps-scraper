FROM python:3.12-slim

WORKDIR /app

# System deps for networking / health
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gmscraper ./gmscraper
COPY config ./config
COPY mcp_server ./mcp_server
COPY .env.example .

# Pre-create data dirs; ZIP list built on first ensure_zips / plan call
RUN mkdir -p data/approvals data/plans data/outputs data/jobs

ENV MCP_TRANSPORT=streamable-http
ENV HOST=0.0.0.0
ENV PORT=8000
ENV PYTHONUNBUFFERED=1
ENV MAPS_PLAN=ultra

EXPOSE 8000

CMD ["python", "-m", "mcp_server"]
