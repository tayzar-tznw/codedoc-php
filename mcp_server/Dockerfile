FROM python:3.14-slim

WORKDIR /app

# Install runtime deps first for better layer caching.
COPY mcp_server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the MCP server package and the agent package it imports.
# .env is intentionally NOT copied — Cloud Run env vars are the source of truth.
COPY mcp_server /app/mcp_server
COPY graph_query_agent /app/graph_query_agent

ENV PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

CMD ["python", "-m", "mcp_server"]
