FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# kdocs-cli (official Kingsoft Docs Skill CLI) for the personal-token backend.
# Pinned version; bump deliberately. Rebuilt rarely — keep this layer early.
ARG KDOCS_CLI_VERSION=2.7.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && case "$(uname -m)" in \
         x86_64|amd64) CLI_ARCH=amd64 ;; \
         aarch64|arm64) CLI_ARCH=arm64 ;; \
         *) echo "unsupported arch" && exit 1 ;; \
       esac \
    && curl -fsSL "https://wpsai.wpscdn.cn/skillhub/pro/v${KDOCS_CLI_VERSION}/releases/kdocs-cli-${KDOCS_CLI_VERSION}-linux-${CLI_ARCH}.tar.gz" \
       -o /tmp/kdocs-cli.tar.gz \
    && tar -xzf /tmp/kdocs-cli.tar.gz -C /tmp \
    && find /tmp -type f -name kdocs-cli -exec mv {} /usr/local/bin/kdocs-cli \; \
    && chmod +x /usr/local/bin/kdocs-cli \
    && rm -rf /tmp/kdocs-cli* \
    && kdocs-cli version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8300

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]
