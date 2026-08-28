FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# docker CLI + compose 插件：网页端"更新与卸载"需调用宿主机 Docker
# （trixie 仓库无 docker-compose-v2 包，改用官方静态二进制）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates curl \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-27.5.1.tgz" | tar -xz -C /tmp \
    && install -m 0755 /tmp/docker/docker /usr/local/bin/docker \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v2.33.1/docker-compose-linux-x86_64" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && rm -rf /var/lib/apt/lists/* /tmp/docker \
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
