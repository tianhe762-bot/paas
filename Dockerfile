FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# docker-compose（v2 独立二进制）：网页端"更新与卸载"需调用宿主机 Docker
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata docker-compose \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash paas \
    && mkdir -p /app/data /app/logs \
    && chown -R paas:paas /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY paas /app/paas
COPY scripts /app/scripts

USER paas

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "paas.main:app", "--host", "0.0.0.0", "--port", "8000"]
