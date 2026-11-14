# Build from the repository root with initialized pinned submodules:
#   docker build -f containers/Dockerfile -t doc-sentinel:local .
FROM rust:1.88-slim-bookworm AS chk_defaced
WORKDIR /src
COPY third_party/chk_defaced-rs/ ./
# AGPL-3.0-only: this is a separate executable, never linked into doc-sentinel.
RUN cargo build --release --locked

# The official latest c2patool binary needs glibc 2.38+, newer than bookworm.
# Isolate Debian's newer loader/libc to that one inspection-only executable;
# the rest of the image continues to use bookworm's system libraries.
FROM --platform=linux/amd64 debian:trixie-slim AS c2patool_libc

# Keep verified download archives and extraction tools out of the runtime image.
FROM python:3.12-slim-bookworm AS downloads
COPY --from=c2patool_libc /usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /usr/lib/x86_64-linux-gnu/libc.so.6 /out/c2patool-lib/
ARG TARGETARCH
ARG NODE_VERSION=22.15.0
ARG NODE_X64_SHA256=dafe2e8f82cb97de1bd10db9e2ec4c07bbf53389b0799b1e095a918951e78fd4
ARG NODE_ARM64_SHA256=d68adf72c531f1118bee75b20ffbc5911accfda5e73454a798625464b40a4adf
ARG C2PATOOL_URL=https://github.com/contentauth/c2pa-rs/releases/download/c2patool-v0.27.23/c2patool-v0.27.23-x86_64-unknown-linux-gnu.tar.gz
ARG C2PATOOL_SHA256=7b8a09f85eeb4c8fe96ce00ffb6985209ebbb05fc39d6c13224a8b33547ba2a4
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) arch=x64; sha="$NODE_X64_SHA256" ;; \
      arm64) arch=arm64; sha="$NODE_ARM64_SHA256" ;; \
      *) echo "Unsupported Node architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 2 "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz" -o /tmp/node.tar.xz; \
    echo "$sha  /tmp/node.tar.xz" | sha256sum -c -; \
    mkdir -p /out/node /out/bin; \
    tar -xJf /tmp/node.tar.xz --strip-components=1 -C /out/node; \
    if [ "$TARGETARCH" = amd64 ]; then \
      curl -fsSL --retry 2 "$C2PATOOL_URL" -o /tmp/c2patool.tar.gz; \
      echo "$C2PATOOL_SHA256  /tmp/c2patool.tar.gz" | sha256sum -c -; \
      mkdir -p /tmp/c2patool; \
      tar -xzf /tmp/c2patool.tar.gz -C /tmp/c2patool; \
      find /tmp/c2patool -type f -name c2patool -exec install -m 0755 '{}' /out/c2patool-lib/c2patool \;; \
      test -x /out/c2patool-lib/c2patool; \
      /out/c2patool-lib/ld-linux-x86-64.so.2 --library-path /out/c2patool-lib /out/c2patool-lib/c2patool --version; \
      printf '%s\n' '#!/bin/sh' 'exec /opt/c2patool-lib/ld-linux-x86-64.so.2 --library-path /opt/c2patool-lib /opt/c2patool-lib/c2patool "$@"' > /out/bin/c2patool; \
      chmod 755 /out/bin/c2patool; \
    else \
      echo "WARNING: upstream c2patool v0.27.23 has no Linux arm64 release; C2PA inspection will be unavailable." >&2; \
    fi

FROM python:3.12-slim-bookworm
# The checkout has no published source URL in project metadata; callers may supply one.
ARG SOURCE_URL=file:///opt/doc-sentinel
LABEL org.opencontainers.image.title="doc-sentinel" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.licenses="MIT; third_party components keep their own licenses incl. AGPL-3.0-only chk_defaced (separate executable)"
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    DOC_SENTINEL_HOME=/opt/doc-sentinel \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    NPM_CONFIG_CACHE=/tmp/.npm \
    XDG_CACHE_HOME=/tmp/.cache
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng qpdf poppler-utils \
      libimage-exiftool-perl util-linux ca-certificates curl git
COPY --from=downloads /out/node/ /usr/local/
COPY --from=downloads /out/bin/ /usr/local/bin/
COPY --from=downloads /out/c2patool-lib/ /opt/c2patool-lib/
RUN pip install uv
WORKDIR /opt/doc-sentinel
# Allowlist the source needed at runtime instead of copying arbitrary root files.
COPY pyproject.toml README.md LICENSE pins.lock.json ./
COPY src/ ./src/
COPY third_party/ ./third_party/
COPY --from=chk_defaced /src/target/release/chk_defaced /opt/doc-sentinel/bin/chk_defaced
# Prompt Guard, torch and Layer B are intentionally not installed.
RUN UV_NO_CACHE=1 uv pip install --system -e '.[clean,detect]' \
      -e 'third_party/perlecta-mcp[rendu]' -e third_party/unicode-smuggling-guard \
    && (cd third_party/subtext-scan && npm ci --ignore-scripts --omit=dev --no-audit --no-fund)
RUN groupadd --gid 10001 sentinel && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home sentinel
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["doc-sentinel"]
CMD ["--help"]
