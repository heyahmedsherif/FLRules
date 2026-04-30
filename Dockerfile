FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency resolution
RUN pip install uv

COPY pyproject.toml .
RUN uv pip install --system -e ".[dev]"

COPY . .
RUN uv pip install --system -e .

# Create data directory for SQLite
RUN mkdir -p /app/data

# Make the universal entrypoint executable
RUN chmod +x /app/scripts/entrypoint.sh

EXPOSE 8000

# Picks web vs cron mode via SERVICE_ROLE env var (default: web)
CMD ["/app/scripts/entrypoint.sh"]
