#!/bin/bash
# Keep PUBLIC_URL.txt in sync with the live cloudflared quick-tunnel domain.
# The domain changes whenever the aq-tunnel container is recreated.
set -u
URL=$(docker logs aq-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)
[ -z "$URL" ] && exit 0
OLD=$(cat /opt/aq-relay/PUBLIC_URL.txt 2>/dev/null | tr -d '\r\n ')
if [ "$URL" != "$OLD" ]; then
  printf '%s\n' "$URL" > /opt/aq-relay/PUBLIC_URL.txt
  echo "$(date -Is) url changed: $OLD -> $URL"
fi
