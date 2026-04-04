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

EXPOSE 8000

# Default: run the web dashboard + scheduler
CMD ["uvicorn", "flrules.web:app", "--host", "0.0.0.0", "--port", "8000"]
