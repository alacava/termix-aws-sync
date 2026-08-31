# syntax=docker/dockerfile:1
FROM python:3.14-slim

ARG AWSCLI_VERSION=2.17.62
ARG NODE_MAJOR=20

# --- system deps: curl/unzip to fetch awscli v2, Node.js 20 LTS for the
# termix CLI, procps for the HEALTHCHECK's pgrep ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        ca-certificates \
        gnupg \
        procps \
    && rm -rf /var/lib/apt/lists/*

# --- awscli v2 (not on PyPI; installed from the official zip installer) ---
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) AWS_ARCH="x86_64" ;; \
         arm64) AWS_ARCH="aarch64" ;; \
         *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWSCLI_VERSION}.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws

# --- Node.js 20 LTS + the termix CLI ---
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @termix-cli/cli \
    && rm -rf /var/lib/apt/lists/*

# --- the package itself ---
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# --- non-root runtime user ---
RUN useradd --create-home --home-dir /home/app --shell /usr/sbin/nologin app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER app
WORKDIR /home/app
ENV HOME=/home/app

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD pgrep -f termix-aws-sync || exit 1

ENTRYPOINT ["entrypoint.sh"]
