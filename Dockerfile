FROM python:3.11-slim-bookworm

LABEL maintainer="drone-detect"
LABEL description="Wi-Fi based drone detection system"

# ── System dependencies ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        iw \
        wireless-tools \
        aircrack-ng \
        libpcap-dev \
        libpcap0.8 \
        gcc \
        libc-dev \
    && rm -rf /var/lib/apt/lists/*

# ── App ──────────────────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Runtime ───────────────────────────────────────────────────────────────
EXPOSE 8080

# The container must be run with --network=host and --privileged (or
# --cap-add=NET_RAW,NET_ADMIN) so Scapy can access the Wi-Fi adapter.
# Example:
#   docker run --rm --privileged --network=host \
#     -e IFACE=wlan0mon drone-detect web

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "main.py"]
CMD ["web"]
