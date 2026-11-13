# AQ Relay — self-hosted OpenAI-compatible gateway

One-box deployment: **ChatNio (CoAI)** as the OpenAI-compatible front end + two Python bridges that
(a) mask upstream vendor identity and (b) serve keyless free models. Ships with a cloudflared quick
tunnel so it is reachable on a public HTTPS URL without touching the firewall.

## What is inside

| Path | Role |
|---|---|
| `docker-compose.yaml` | ChatNio container, host network (uses the host MySQL/Redis) |
| `config/config.example.yaml` | channels + 35 charge rules + 27 public model aliases |
| `gateway/mask_gateway.py` | :8903 — strips `real|alias` syntax, injects an identity system prompt, scrubs vendor tokens from streaming output |
| `gateway/pollinations_bridge.py` | :8899 — keyless fallback upstream |
| `systemd/*.service` | keeps both bridges alive |
| `install.sh` | end-to-end installer |

## Requirements

- Ubuntu 22.04+ (tested), 2 vCPU / 1 GB RAM, ~5 GB disk
- MySQL 8 + Redis on localhost (the installer expects both already running)
- Outbound access to the upstream endpoints you configure

## Install

```bash
git clone <this-repo> /opt/aq-relay && cd /opt/aq-relay
cp .env.example .env && vi .env          # MYSQL_PASSWORD / REDIS_PASSWORD
sudo bash install.sh
```

The script prints the public `https://*.trycloudflare.com` URL at the end and stores it in `PUBLIC_URL.txt`.

## First login

ChatNio creates `root` with a random password on first boot:

```bash
docker logs chatnio 2>&1 | grep "creating root user"
```

Reset it any time (ChatNio stores `sha256(password)`, no salt):

```bash
NEW='YourNewPassword'
HASH=$(printf '%s' "$NEW" | sha256sum | cut -d' ' -f1)
mysql -h127.0.0.1 -uchatnio -p"$MYSQL_PASSWORD" chatnio \
  -e "UPDATE auth SET password='$HASH' WHERE username='root';"
```

## API

```bash
curl https://<your-url>/v1/models -H "Authorization: Bearer sk-..."
curl https://<your-url>/v1/chat/completions \
  -H "Authorization: Bearer sk-..." -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.2","messages":[{"role":"user","content":"hi"}]}'
```

Get a key: log in → `GET /api/apikey` with the JWT, or `/apikey` in the web UI.

## Notes

- `usage.quota.percent` returned by some upstreams is a **per-account 5-hour window**, not the pool total.
- Model names in `config/config.example.yaml` are aliases; the real upstream model sits after the `|`.
- The quick tunnel domain changes when the `aq-tunnel` container is recreated. For a stable hostname use a
  named tunnel with your own Cloudflare account.
