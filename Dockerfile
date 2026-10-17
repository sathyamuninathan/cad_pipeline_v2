FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libexpat1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY standards ./standards
RUN python -m compileall -q /app \
 && python -c "import build123d, mcp" \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin cad \
 && mkdir /out && chown cad /out

ENV CAD_OUTPUT_DIR=/out \
    HOME=/tmp \
    MALLOC_ARENA_MAX=2 \
    OMP_NUM_THREADS=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER cad
CMD ["python", "mcp_server.py"]
