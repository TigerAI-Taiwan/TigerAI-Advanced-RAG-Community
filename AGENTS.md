# AGENTS.md — Autonomous install guide for AI coding agents

You are an AI coding agent (Claude Code / Antigravity / Codex / Gemini / etc.). A human handed you **two URLs** and asked you to install + wire them together **without further human steps**:

1. **OpenGenie AI Stack** — https://github.com/TigerAI-Taiwan/OpenGenie-AI-Stack (the infrastructure)
2. **This repo** — TigerAI Advanced RAG, Community Edition (the application on top)

Your job: deploy OpenGenie first, then deploy this app and connect it to OpenGenie. Do it end-to-end; only stop to ask the human for **secrets you cannot obtain via API** (Cloud AI API key + n8n credential creation).

---

## Quick start (check-first, two-phase deploy)

```bash
# Phase 1: clone + start containers
# v1.0.25 (2026-06-01): 用 latest(或最新 tag),不要 pin 舊版。舊版有 fresh-install 必中的 bug
# (SETTINGS_SECRET 崩潰、OWUI key 不讀 DB、訓練 chain cloud_ai_key/output 缺、完成狀態倒退 race…)
# 全在 v1.0.16-25 修掉。
git clone https://github.com/TigerAI-Taiwan/TigerAI-Advanced-RAG-Community.git
cd TigerAI-Advanced-RAG-Community
cp .env.example .env && vim .env   # 依 STACK-REFERENCE.md §4 probe SOP 填(別套預設值!)
docker compose pull                # 確保拉到 latest image(含全部修復)
docker compose up -d

# Phase 2: set env + RUN CHECKS FIRST (no install, just diagnose)
export N8N_URL=http://<n8n-container>:5678
export N8N_API_KEY=<n8n Settings -> API key>
python3 deploy_n8n.py --check-only

# Phase 2 prints status of:
#   [rag-check]   - is RAG/ dir handled?
#   [svc-detect]  - are all OpenGenie services discovered?
#   [cred-check]  - is OpenAI credential ready in n8n?
#   workflow preview - what would be imported

# Read the output. If anything is RED/NOT FOUND/MISSING, fix it FIRST.
# The single most common missing item: "MISSING required credential type: openAiApi"
#   → Open n8n UI, Settings → Credentials → New → OpenAI → paste key → save

# Phase 3: once --check-only shows all OK, actually deploy
python3 deploy_n8n.py --force
```

**Why check-first matters**: previous installs failed silently because someone (you or a previous AI) skipped a step. The `--check-only` mode forces you to see the environment state before any mutation. If you run `--force` directly and a check would have failed, you'll waste 30+ seconds importing workflows that can't activate.

---

## The ONE manual step you must do (and `--check-only` will tell you so)

**Create an OpenAI credential in n8n UI before running `deploy_n8n.py --force`:**

1. Browser → n8n UI → Settings → Credentials → New
2. Pick **"OpenAI"** credential type
3. Paste user's OpenAI API key
4. Save — any name works (script matches by type)

**Why**: n8n REST API encrypts secrets and doesn't allow programmatic credential creation. `deploy_n8n.py` auto-rewires workflow refs to whatever cred you created (by type match + name match fallback), but the cred itself must exist first.

**If you skip this**: `--check-only` will print:
```
[cred-check] MISSING required credential type(s): ['openAiApi']
             Workflows referencing these will fail at activation.
             FIX: open n8n UI → Settings → Credentials → New → pick 'OpenAI'
                  paste your API key → Save (any name works)
             Then re-run this script.
```
Don't try to install without this — it WILL fail.

---

## What's automated (you don't need to touch these)

`deploy_n8n.py` runs 4 pre-flight phases before workflow import:

| Phase | What it does | Was failure mode in pre-v1.0.7 installs |
|---|---|---|
| **RAG/ dir check** | Auto-creates `/srv/RAG` symlink (splitter) AND `/home/node/.n8n-files/RAG` (n8n container via docker exec) | "PDF→MD silently succeeds 0 files" |
| **OpenGenie services detect** | `docker ps` + regex to find docling/qdrant/openwebui/filebrowser/postgres/ollama containers regardless of name; auto-POSTs URLs to backend settings | "Container named differently than defaults → workflows call wrong URL" |
| **Execute Workflow ID rewire** | n8n assigns fresh random IDs to imported workflows; script re-resolves `Execute Workflow` node references by workflow name | "#05 can't find #05-RAG-Core sub-workflow" |
| **Credential ID rewire** | Workflows ship with hardcoded credential IDs from dev env; script GETs `/api/v1/credentials` and rewires by type+name match | "4 workflows fail with 'credentials not found'" |

All phases soft-fail with clear error messages. If a phase fails, you'll see exactly what + how to fix manually.

---

## Hard rules

- This is the **Community (cloud-only)** edition. Do **not** wire local/Ollama, reranking, multimodal catalog, or scheduling queue — those are paid tiers and intentionally absent. If user asks for these, redirect to Pro/Enterprise.
- Only human inputs you can request:
  1. **Cloud AI endpoint + API key** (for Tab 07 OR pre-set in `.env`)
  2. **OpenAI credential creation in n8n UI** (one-time, before deploy_n8n.py)
- Verify with the in-app health check before declaring done: `curl http://localhost:8088/backend/health` (or whatever HOST_UI_PORT you set) should return all green.

---

## What this app needs from OpenGenie

| OpenGenie service | This app uses it for | Auto-detected? |
|---|---|---|
| n8n | RAG ingestion + query workflows (you import them) | ✓ |
| Qdrant | vector database | ✓ |
| Open WebUI | chat apps (you install 2 pipe functions) | ✓ |
| Docling | PDF → Markdown | ✓ |
| PostgreSQL | metadata (projects/rules/keywords/chat-apps) | partially (backend reads via env) |
| Redis | used by n8n | n/a (n8n manages) |
| FileBrowser | shared RAG files volume | ✓ |

---

## Common deploy patterns by scenario

### Scenario A: First time on a clean machine

1. Deploy OpenGenie following its own AGENTS.md / `llms.txt`
2. Run our 5 commands above
3. Open browser, configure Cloud AI in Tab 07, create OpenAI cred in n8n, re-run `deploy_n8n.py --force`
4. Done

### Scenario B: User has existing OpenGenie + wants to add TigerAI

1. Identify existing OpenGenie services (run `docker ps`)
2. Note `RAG_FILES_PATH` from FileBrowser's mount: `docker inspect <fb-container> | grep -A1 Mounts`
3. Set in `.env`: `RAG_FILES_PATH=<that path>` and `STACK_NETWORK=<their docker network>`
4. Run our 5 commands
5. Done

### Scenario C: Re-install / clean slate

```bash
docker compose down -v
docker rm -f tigerai-nginx tigerai-backend tigerai-splitter 2>/dev/null
rm -rf TigerAI-Advanced-RAG-Community
# Then start from Scenario A
```

(`down -v` removes our 3 containers' volumes but leaves OpenGenie's data intact.)

---

## Failure debugging — what to look for in deploy_n8n.py output

The script prints labeled sections. If any of these is missing or fails, that's the problem:

- `[rag-check] OK: ...` → RAG/ dir handled
- `[svc-detect] <service> → <url>` → service discovered. `NOT FOUND` = container with that name doesn't exist; fix container name or set `<SERVICE>_URL` env manually.
- `[REWIRE] <workflow>` → Execute Workflow refs rewired
- `[REWIRE-CRED] <workflow> node '<X>' openAiApi → '<your cred>'` → credential rewired
- `[ACTIVE] <workflow>` → workflow activated

Don't try to manually fix RAG mounts, container names, workflow IDs, or credential bindings — the script handles all of these. **If you're tempted to make a manual change, stop and re-read the script output for the actual error first.**

---

## Pinning

Always pin to a specific release tag for reproducibility:

```yaml
# In your fork of docker-compose.yml override or .env:
TIGERAI_IMAGE_PREFIX=ghcr.io/tigerai-taiwan
# Then images resolve as:
#   ghcr.io/tigerai-taiwan/tigerai-rag-nginx:v1.0.7
#   ghcr.io/tigerai-taiwan/tigerai-rag-backend:v1.0.7
#   ghcr.io/tigerai-taiwan/tigerai-rag-splitter:v1.0.7
```

Or in `docker-compose.yml`, change `:latest` → `:v1.0.7` on each image.

---

Continue in **[INSTALL.md](INSTALL.md)** for the detailed runbook each command performs.
