# Image sandbox tim backend untuk terminal.backend: docker (team.yaml → sandbox.image).
# Tujuannya: toolchain siap pakai, jadi agent tidak memasang ulang di tiap sandbox.
#
#   docker build -t hermes-team-sandbox:2026.11 docker/sandbox
#   team.yaml:  sandbox: { image: "hermes-team-sandbox:2026.11" }
#   lalu: hermes_team.py managed → pasang ulang /etc/hermes/config.yaml → /restart
#
# Pakai tag bertanggal (bukan :latest) supaya sandbox semua anggota sama dan rollback mudah.
# Jangan menaruh kredensial apa pun di image: secret masuk lewat /env-set per profile.
FROM debian:bookworm-slim

ARG NODE_MAJOR=22
ARG GO_VERSION=1.23.4
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 \
    PATH=/usr/local/go/bin:/root/go/bin:/root/.local/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git openssh-client gnupg unzip zip xz-utils \
        build-essential pkg-config make jq ripgrep fd-find less vim-tiny procps \
        python3 python3-pip python3-venv pipx \
        php-cli php-mbstring php-xml php-curl php-zip php-mysql php-pgsql composer \
        default-jdk-headless maven \
        default-mysql-client postgresql-client redis-tools sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Node.js (NodeSource) + pnpm/yarn lewat corepack
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && corepack enable \
    && rm -rf /var/lib/apt/lists/*

# Go
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz" | tar -C /usr/local -xz

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
CMD ["sleep", "infinity"]
