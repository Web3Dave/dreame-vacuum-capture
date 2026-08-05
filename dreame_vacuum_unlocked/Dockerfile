# Builder stage matches the toolchain (Ubuntu 20.04 / GCC) Tencent's vendored
# XP2P static libraries were built with - a newer GCC's LTO bytecode won't link.
FROM ubuntu:20.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pc_client/CMakeLists.txt .
COPY pc_client/src/ src/
COPY pc_client/lib/ lib/
RUN cmake -S . -B build && cmake --build build --config Release

FROM ubuntu:20.04

ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_DESCRIPTION
ARG BUILD_NAME
ARG BUILD_REF
ARG BUILD_REPOSITORY
ARG BUILD_VERSION

LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="Web3Dave@users.noreply.github.com" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.created=${BUILD_DATE} \
    org.opencontainers.image.revision=${BUILD_REF} \
    org.opencontainers.image.version=${BUILD_VERSION}

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip libstdc++6 ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# MediaMTX: bundled RTSP server. ffmpeg republishes each device's local FLV
# feed into it (rtsp://127.0.0.1:8554/<did>), and it serves that out to any
# RTSP client (HA's stream component included) on the same path/port.
ARG MEDIAMTX_VERSION=v1.19.3
RUN curl -sL "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
      -o /tmp/mediamtx.tar.gz \
    && tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx \
    && rm /tmp/mediamtx.tar.gz

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY --from=builder /build/build/p2p_sample ./p2p_sample
COPY --from=builder /build/build/p2p_speak ./p2p_speak
COPY dreame_lib/ dreame_lib/
COPY dreame_sign.py .
COPY app.py .
COPY store.py .
COPY config_store.py .
COPY classify_store.py .
COPY classify_train.py .
COPY classify_infer.py .
COPY classify_download.py .
COPY steps.py .
COPY ha_client.py .
COPY ui.py .
COPY templates/ templates/
COPY run.sh .
COPY mediamtx.yml .
RUN chmod +x run.sh

EXPOSE 8099 8100 8554
CMD ["./run.sh"]
