# =============================================================================
# JoinMarket-NG XBT + Jam: one image (jmwalletd + Jam web UI + orderbook
# watcher + Tor). Build with xbt-docker/build.sh (context = this repository,
# named context "jam" = the Jam XBT fork at a fixed commit).
#
# Everything is pinned: base image by digest, Node.js tarball by sha256,
# Debian packages by snapshot, Python deps by hash (--require-hashes through
# the hashed requirements.txt files), Jam deps by package-lock.json integrity.
# Reproducible: build.sh passes SOURCE_DATE_EPOCH (= the commit time) and
# exports with rewrite-timestamp=true, so two builds of the same commits give
# the same image digest (see XBT.md, "Reproducible image").
# =============================================================================
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
ARG SETUPTOOLS_VERSION=84.0.0
ARG WHEEL_VERSION=0.48.0
ARG DEBIAN_SNAPSHOT=20261012T082414Z
ARG NODE_VERSION=26.7.0
# sha256 of the official tarballs (nodejs.org/dist/v26.7.0/SHASUMS256.txt, signed)
ARG NODE_SHA256_AMD64=982aa24dd8be4c889c6a8ab337ddff3b0896645b20f4239356e80552c16277ee
ARG NODE_SHA256_ARM64=afc7a004018485092ac8985b817b0d5684472bd9472e0b57d2ab88737e50090d

# =============================================================================
# Stage 1: Jam (static web UI)
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS jamui
ARG TARGETARCH
ARG SOURCE_DATE_EPOCH
ARG NODE_VERSION
ARG NODE_SHA256_AMD64
ARG NODE_SHA256_ARM64
ARG DEBIAN_SNAPSHOT
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh xz-utils ca-certificates libatomic1
ADD --checksum=sha256:${NODE_SHA256_AMD64} https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz /tmp/node-amd64.tar.xz
ADD --checksum=sha256:${NODE_SHA256_ARM64} https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz /tmp/node-arm64.tar.xz
RUN mkdir /opt/node && \
    tar -xJf /tmp/node-${TARGETARCH}.tar.xz -C /opt/node --strip-components=1 && \
    rm /tmp/node-*.tar.xz
ENV PATH="/opt/node/bin:$PATH"
WORKDIR /src
COPY --from=jam package.json package-lock.json ./
# Lockfile only, integrity-checked; no install scripts.
RUN --mount=type=cache,target=/root/.npm,id=jam-xbt-npm \
    npm ci --ignore-scripts --no-audit --no-fund
COPY --from=jam . .
RUN npm run build && test -f dist/index.html

# =============================================================================
# Stage 2: Python environment (same as upstream jmwalletd/Dockerfile)
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS builder
WORKDIR /build
ARG DEBIAN_SNAPSHOT
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh build-essential libffi-dev libsodium-dev pkg-config
ARG SOURCE_DATE_EPOCH
ARG JOINMARKET_BUILD_COMMIT
ARG JOINMARKET_BUILD_REF
ENV JOINMARKET_BUILD_COMMIT=${JOINMARKET_BUILD_COMMIT}
ENV JOINMARKET_BUILD_REF=${JOINMARKET_BUILD_REF}
ARG SETUPTOOLS_VERSION
ARG WHEEL_VERSION
RUN echo "setuptools==${SETUPTOOLS_VERSION}" > /build/constraints.txt && \
    echo "wheel==${WHEEL_VERSION}" >> /build/constraints.txt
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY jmcore/requirements.txt /build/requirements-jmcore.txt
COPY jmwallet/requirements.txt /build/requirements-jmwallet.txt
COPY maker/requirements.txt /build/requirements-maker.txt
COPY taker/requirements.txt /build/requirements-taker.txt
COPY orderbook_watcher/requirements.txt /build/requirements-obwatcher.txt
COPY jmwalletd/requirements.txt /build/requirements-jmwalletd.txt
RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-xbt-pip-${SETUPTOOLS_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --require-hashes \
        -r /build/requirements-jmcore.txt \
        -r /build/requirements-jmwallet.txt \
        -r /build/requirements-maker.txt \
        -r /build/requirements-taker.txt \
        -r /build/requirements-obwatcher.txt \
        -r /build/requirements-jmwalletd.txt
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml jmcore/setup.py /build/jmcore/
COPY jmwallet/src /build/jmwallet/src
COPY jmwallet/pyproject.toml /build/jmwallet/
COPY maker/src /build/maker/src
COPY maker/pyproject.toml /build/maker/
COPY taker/src /build/taker/src
COPY taker/pyproject.toml /build/taker/
COPY orderbook_watcher/src /build/orderbook_watcher/src
COPY orderbook_watcher/pyproject.toml /build/orderbook_watcher/
COPY jmwalletd/src /build/jmwalletd/src
COPY jmwalletd/pyproject.toml /build/jmwalletd/
COPY tumbler/src /build/tumbler/src
COPY tumbler/pyproject.toml /build/tumbler/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /build/jmcore /build/jmwallet /build/maker /build/taker \
             /build/orderbook_watcher /build/jmwalletd /build/tumbler \
             -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
    fi
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --no-deps --build-constraint /build/constraints.txt \
        /build/jmcore /build/jmwallet /build/maker /build/taker \
        /build/orderbook_watcher /build/tumbler /build/jmwalletd \
    && rm -rf \
        /opt/venv/bin/pip* \
        /opt/venv/lib/python3.*/site-packages/pip \
        /opt/venv/lib/python3.*/site-packages/pip-*.dist-info

# =============================================================================
# Stage 3: runtime
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS runtime
ARG DEBIAN_SNAPSHOT
# Exported to every RUN: useradd/adduser (tor's postinst too) then write
# SOURCE_DATE_EPOCH, not today, as the "last password change" in /etc/shadow.
ARG SOURCE_DATE_EPOCH
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh libsecp256k1-2 libsodium23 tini tor \
    && rm -rf \
        /usr/local/bin/pip* \
        /usr/local/lib/python3.*/ensurepip \
        /usr/local/lib/python3.*/site-packages/pip \
        /usr/local/lib/python3.*/site-packages/pip-*.dist-info

COPY --from=builder /opt/venv /opt/venv
COPY --from=jamui /src/dist /usr/share/jmwalletd/jam
COPY --chmod=0644 xbt-docker/torrc /etc/jm-xbt/torrc
COPY --chmod=0755 xbt-docker/entrypoint.sh /usr/local/bin/jm-xbt-entrypoint
COPY --chmod=0644 xbt-docker/preflight.py /usr/local/lib/jm-xbt/preflight.py

RUN chmod 0755 /etc/jm-xbt /usr/local/lib/jm-xbt \
    && groupadd --gid 1000 jm && useradd --uid 1000 --gid 1000 --home-dir /data --no-create-home jm \
    && mkdir -p /data && chown 1000:1000 /data && chmod 700 /data

# XBT defaults (override with -e; environment wins over /data/config.toml).
# Network: only jmwalletd (28183: API + Jam) listens on the container's own
# interface, so that `-p 127.0.0.1:28183:28183` can reach it. Tor (SOCKS
# 28185, control 28186), the orderbook watcher (28184) and the maker's onion
# listener stay on 127.0.0.1 inside the container. Run it on a normal bridge
# network, never with --network host.
# Nick authentication: require_verified, our directory supports JMP-0005.
# Takers use only makers WITH a fidelity bond (allowance 0 = no bondless
# maker is ever chosen, the CoinJoin fails with "Not enough makers with a
# fidelity bond" instead); zero-fee rule kept as a second guard.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    JOINMARKET_DATA_DIR=/data \
    JMWALLETD_HOST=0.0.0.0 \
    BITCOIN__BACKEND_TYPE=descriptor_wallet \
    NETWORK_CONFIG__NETWORK=mainnet \
    NETWORK_CONFIG__DIRECTORY_SERVERS=f3tfzgk5ilwfapx427yx5ckeh7ptuvskfxnfnicfo747jyq5mgzdtvad.onion:5222 \
    NETWORK_CONFIG__NICK_AUTH_MODE=require_verified \
    NETWORK_CONFIG__NICK_AUTH_DIRECTORY_IDS='{"f3tfzgk5ilwfapx427yx5ckeh7ptuvskfxnfnicfo747jyq5mgzdtvad.onion:5222":"f3tfzgk5ilwfapx427yx5ckeh7ptuvskfxnfnicfo747jyq5mgzdtvad.onion:5222"}' \
    TOR__SOCKS_HOST=127.0.0.1 \
    TOR__SOCKS_PORT=28185 \
    TOR__CONTROL_HOST=127.0.0.1 \
    TOR__CONTROL_PORT=28186 \
    TOR__COOKIE_PATH=/data/tor/control_auth_cookie \
    ORDERBOOK_WATCHER__HTTP_HOST=127.0.0.1 \
    ORDERBOOK_WATCHER__HTTP_PORT=28184 \
    OBWATCH_URL=http://127.0.0.1:28184 \
    MAKER__ONION_SERVING_HOST=127.0.0.1 \
    TAKER__BONDLESS_MAKERS_ALLOWANCE=0 \
    TAKER__BONDLESS_REQUIRE_ZERO_FEE=true \
    TAKER__MINIMUM_MAKERS=4

USER 1000:1000
WORKDIR /data
VOLUME /data
EXPOSE 28183
ENTRYPOINT ["tini", "-g", "--", "jm-xbt-entrypoint"]
