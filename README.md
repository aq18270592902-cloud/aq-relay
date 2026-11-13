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

## Live deployment

Public endpoint (quick tunnel, regenerated on container restart — see `PUBLIC_URL.txt`):

- Web UI / admin: `https://<PUBLIC_URL>/login`, admin console at `/admin`
- OpenAI-compatible API: `https://<PUBLIC_URL>/v1`
- Model list: 27 aliases (gpt-5.2 family, claude-4.6 family, gemini-3.0, deepseek-v4.1, qwen-4, glm-4.6v, kimi-k3, grok-5, o3/o4-mini …)

Verified end-to-end: `GET /v1/models` → 27, `POST /v1/chat/completions` with `gpt-5.2` returns
`"我是 gpt-5.2，由 OpenAI 研发的大语言模型。"` and `usage.prompt_tokens: 17`.

## Layout on the server

```
/opt/aq-relay/
├── .env                  # MYSQL_PASSWORD / REDIS_PASSWORD / AQ_UPSTREAM_*
├── config/config.yaml    # real config (git-ignored)
├── gateway/              # mask_gateway.py, pollinations_bridge.py, url_keep.sh
├── logs/  storage/
└── PUBLIC_URL.txt        # kept in sync by aq-urlkeep.timer
```

## Services

| Unit | What it does |
|---|---|
| `chatnio` (docker) | the gateway itself, host network, listens on :8094 |
| `aq-tunnel` (docker) | cloudflared quick tunnel → public HTTPS |
| `aq-polli.service` | :8899 keyless bridge |
| `aq-mask.service` | :8903 identity-mask proxy |
| `aq-urlkeep.timer` | refreshes `PUBLIC_URL.txt` every 30 s |

```bash
systemctl status aq-polli aq-mask aq-urlkeep.timer
docker ps --filter name=chatnio --filter name=aq-tunnel
```

## Cost model

ChatNio bills per 1K tokens: `cost = tokens / 1000 * rate`. One `quota` unit ≈ ¥0.1.
Charge rules live under `charge:` in `config/config.yaml`; a model with **no** rule is unpriced —
regular users get `the price of the model is not set` and admins use it for free.
```yaml
charge:
  - id: 1
    type: token-billing
    anonymous: false
    input: 0.03
    output: 0.06
    models:
      - gpt-5.2
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `503 estimated cost exceeds user quota ... your quota: 0.00` | grant quota: `INSERT INTO quota (user_id,quota,used) VALUES (1,999998.59,0) ON DUPLICATE KEY UPDATE quota=999998.59;` |
| `invalid username or password` | ChatNio hashes with plain `sha256(password)`; update the `auth` row directly (see above) |
| `Error 1366 Incorrect string value` | DB/table is latin1 — `ALTER TABLE conversation CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;` |
| model missing from `/v1/models` | channel `state` is false, or config changed without `docker restart chatnio` |
| tunnel URL dead | `docker restart aq-tunnel`, then `cat /opt/aq-relay/PUBLIC_URL.txt` |
