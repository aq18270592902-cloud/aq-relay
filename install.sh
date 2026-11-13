#!/bin/bash
# AQ Relay one-shot installer (Ubuntu 22.04+)
# Usage: sudo bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[!] .env created from .env.example — fill in the passwords and upstream endpoints, then rerun."
  exit 1
fi
set -a; . ./.env; set +a

echo "[1/6] checking docker"
command -v docker >/dev/null || { apt-get update && apt-get install -y docker.io docker-compose-v2; }

echo "[2/6] preparing directories and config"
mkdir -p config logs storage gateway
if [ ! -f config/config.yaml ]; then
  cp config/config.example.yaml config/config.yaml
  echo "[!] config/config.yaml created from the example."
  echo "    Edit it: replace __RELAY_SECRET__ / __ZHIPU_SECRET__ / __JWT_SECRET__ with your own values."
  echo "    Then rerun this script."
  exit 1
fi
sed -i "s|__MYSQL_PASSWORD__|$MYSQL_PASSWORD|g; s|__REDIS_PASSWORD__|$REDIS_PASSWORD|g" config/config.yaml

echo "[3/6] installing bridge services"
cp systemd/aq-polli.service systemd/aq-mask.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aq-polli aq-mask
sleep 2
systemctl is-active aq-polli aq-mask

echo "[4/6] starting chatnio (host network)"
docker compose up -d
sleep 20
docker ps --filter name=chatnio --format '{{.Names}} {{.Status}}'

echo "[5/6] local check"
curl -s -o /dev/null -w "http://127.0.0.1:8094/ -> %{http_code}\n" --max-time 10 http://127.0.0.1:8094/ || true

echo "[6/6] public tunnel (cloudflared quick tunnel)"
docker rm -f aq-tunnel >/dev/null 2>&1 || true
docker run -d --name aq-tunnel --restart always --network host \
  cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://127.0.0.1:8094 >/dev/null
sleep 15
URL=$(docker logs aq-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
echo "$URL" > PUBLIC_URL.txt
echo
echo "PUBLIC URL: $URL"
echo "first-boot root password:  docker logs chatnio 2>&1 | grep 'creating root user'"
