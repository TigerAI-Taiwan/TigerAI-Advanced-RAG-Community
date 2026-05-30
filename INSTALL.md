# INSTALL — agent runbook (TigerAI Advanced RAG, Community)

> Written for an **AI coding agent** to execute end-to-end (see [AGENTS.md](AGENTS.md)). A human can read it too, but the intended flow is: hand the agent this repo + the OpenGenie URL, and it runs everything.

**Precondition:** the **OpenGenie AI Stack** is already deployed and healthy (n8n, Qdrant, Open WebUI, Docling, PostgreSQL, Redis, FileBrowser).

---

## 1. Discover the running OpenGenie stack

```bash
docker network ls                 # find the stack network
docker ps --format '{{.Names}}'   # find container names (n8n, qdrant, open-webui, postgres, redis, docling, filebrowser)
docker inspect <n8n-container> --format '{{range .Mounts}}{{.Source}} {{.Destination}}{{"\n"}}{{end}}'  # find the shared RAG files host path
```

Record: `STACK_NETWORK`, each service name/URL, `RAG_FILES_PATH`, and create an **n8n API key** (n8n → Settings → API).

## 2. Configure

```bash
cp .env.example .env
```
Edit `.env` so each host/URL matches the **actual OpenGenie container names** you found (e.g. `N8N_HOST`, `DOCLING_BASE_URL`, `QDRANT_URL`, `PG_HOST`, `REDIS_HOST`, `OWUI_BASE_URL`). Set `PUBLIC_URL` to a URL users can reach (e.g. `http://<host>:8088`).

Export the compose vars (or put them in a `.env` next to the compose):
```bash
export STACK_NETWORK=<opengenie network name>
export RAG_FILES_PATH=<shared RAG files host path>
export TIGERAI_IMAGE_PREFIX=ghcr.io/tigerai-taiwan   # adjust to the published registry
```

> **Port 8088 already taken?** The `docker-compose.yml` now honors a `HOST_UI_PORT` env var (defaults to `8088`). If port 8088 is occupied on the host (common: cAdvisor in OpenGenie also defaults to 8088), override it at startup, e.g. `HOST_UI_PORT=8888 docker compose up -d`. Remember to update `PUBLIC_URL` in `.env` to match.

> **FileBrowser password rule (⚠️ gotcha):** recent FileBrowser versions enforce a **minimum 12-char** password and reject weak ones. The classic `admin` / `changeme` defaults will fail silently at login. Set a real ≥12-char `FB_PASSWORD` in `.env` AND match it in the FileBrowser admin UI (or pre-seed via FileBrowser's own setup) before this app's upload feature will work.

## 3. Start the three app services (on the OpenGenie network)

```bash
docker compose pull
docker compose up -d
# tigerai-nginx (UI :8088), tigerai-backend, tigerai-splitter
curl -s http://localhost:8088/backend/health   # expect ok
```

## 4. Import the n8n workflows (via API)

**Recommended: use the automated importer.**

```bash
export N8N_URL=http://<n8n-host>:5678
export N8N_API_KEY=<your key>
python deploy_n8n.py
# Imports every workflow under n8n/V3-WebApp-v1.0/, rewires Execute Workflow
# subflow references by name (their ids change on each install), and activates
# them. Idempotent: re-runs skip existing workflows unless --force is passed.
```

- Why the script is needed: when n8n imports a workflow it assigns a **fresh random id** to every subflow. Any `Execute Workflow` node that referenced the old id is now broken. `deploy_n8n.py` re-resolves those references **by workflow name** after import so the call graph still works without manual editing.

These are the cloud query entries (`#05-NonStream-Cloud`, `#05b-Cloud-Streaming`, `#05-RAG-Core`, `Sub-Chat-Cloud`) plus the ingestion pipeline (`#01`–`#04`, `#03b`). The script activates them; note each Production webhook path from the n8n UI.

### Manual fallback (no Python)

If Python isn't available, you can POST each file with curl:
```bash
for f in n8n/V3-WebApp-v1.0/*.json; do
  curl -s -X POST "http://<n8n-host>:5678/api/v1/workflows" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" -H "Content-Type: application/json" \
    --data-binary @"$f"
done
```
> **Warning:** this path leaves every `Execute Workflow` reference pointing at the *old* (pre-import) subflow ids, so the workflows will fail at runtime. You must open each caller in the n8n UI and re-pick the correct subflow from the dropdown before activating.

## 5. Install the Open WebUI pipes

Add both functions to Open WebUI (Admin → Functions → import):
`deployments/owui/n8n_pipeline_v052.py` (non-streaming) and `deployments/owui/n8n_pipeline_v212.py` (streaming).

## 6. Configure Cloud AI (the one human-provided secret)

Open **http://localhost:8088 → 07.System Settings**, set the **Cloud AI** endpoint + **API key** (ask the human for this) and the **Qdrant** URL. Run the health check — expect all services green.

## 7. Hand off

Tell the human it's ready at `http://<host>:8088` (UI toggles **EN / 中文**). First real use: 01.Rule Builder → 02.Training Projects (upload PDFs, Start) → 05 chat app → 06 QC.

---

- **Cloud-only edition** — local AI, reranking, multimodal catalog, scheduling queue are paid tiers (do not attempt to wire them).
- License: Apache-2.0.
