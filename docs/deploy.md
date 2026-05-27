# LearnAgent Deployment

This is the minimal single-server deployment path for the current local MVP.

## Server

Recommended baseline:

```text
4 vCPU
8-16 GB RAM
50-100 GB SSD
Ubuntu 22.04/24.04
Docker + Docker Compose
```

## DNS

Point the domain to the server IP:

```text
A      @      <server-ip>
CNAME  www    mlsshherr.fun
A      api    <server-ip>
```

Use `api.mlsshherr.fun` for the Agent API.

## Environment

Create `.env` on the server:

```bash
cp .env.example .env
```

At minimum, set:

```env
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash
SCENARIO=watermark
COPILOT_CAPABILITIES=rag,http
AGENT_EVENT_STORE_PATH=/app/storage/learnagent-events.sqlite
AGENT_CHECKPOINT_PATH=/app/storage/langgraph-checkpoints.sqlite
```

Do not commit `.env`.

## Start

```bash
docker compose up -d --build
docker compose logs -f copilot-agent
```

The container listens on `127.0.0.1:8090` on the host. Put Nginx or Caddy in front of it.

## Nginx

```nginx
server {
    listen 80;
    server_name api.mlsshherr.fun;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
    }
}
```

Add HTTPS with Certbot:

```bash
sudo certbot --nginx -d api.mlsshherr.fun
```

## Check

```bash
curl http://127.0.0.1:8090/v1/scenario
curl https://api.mlsshherr.fun/v1/scenario
```

## Persistent Data

These paths are mounted from the host:

```text
./storage   SQLite EventStore, checkpoints, memory
./artifacts runtime/eval summaries
```

Back up `storage/` regularly.
