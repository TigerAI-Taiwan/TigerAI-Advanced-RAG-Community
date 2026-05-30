# AGENTS.md — Autonomous install guide for AI coding agents

You are an AI coding agent (Claude Code / Antigravity / Codex / etc.). A human handed you **two URLs** and asked you to install and wire them together **without further human steps**:

1. **OpenGenie AI Stack** — https://github.com/TigerAI-Taiwan/OpenGenie-AI-Stack (the infrastructure)
2. **This repo** — TigerAI Advanced RAG, Community Edition (the application on top)

Your job: deploy OpenGenie first, then deploy this app and connect it to OpenGenie. Do it end-to-end; only stop to ask the human for **secrets you cannot obtain** (e.g. a Cloud AI API key).

## Order of operations

1. **Install OpenGenie** by following its repo (it ships its own `llms.txt` + agent deployment skill protocols). Bring up at least: **n8n, Qdrant, Open WebUI, Docling, PostgreSQL, Redis, FileBrowser**. Record:
   - the Docker **network name**
   - each service's **container name / URL**
   - the **shared RAG files host path** (the directory FileBrowser/n8n use)
   - an **n8n API key** (create one if needed)
2. **Install this app** — follow **[INSTALL.md](INSTALL.md)** step by step. It is written for you to execute.

## Hard rules

- This is the **Community (cloud-only)** edition. Do **not** try to wire local/Ollama, reranking, multimodal catalog, or a scheduling queue — those are paid tiers and intentionally absent.
- The only human input you should request is the **Cloud AI endpoint + API key** (and the public base URL if deploying remotely). Everything else you discover from the running OpenGenie stack.
- Verify with the in-app health check before declaring done (expect all services green).

## ⚠️ Known deploy gotchas — do these BEFORE `docker compose up`

These are the failure modes that real installs hit silently (pipeline succeeds with 0 files, login fails, port conflicts). Address them up front:

1. ~~Create the `RAG/` subdirectory under the shared files path~~ — **AUTO-HANDLED since v1.0.4**. The `tigerai-splitter` container creates `/srv/RAG -> .` symlink on startup. You no longer need to do anything. (Conceptually: the `RAG/` directory = the training projects container shown in Tab 02 UI; symlink presents this view to n8n.) Verify: `docker exec tigerai-splitter ls -la /srv/RAG`

2. **Check `HOST_UI_PORT`**: default 8088 conflicts with cAdvisor in many OpenGenie stacks. Probe first (`ss -lnt | grep 8088` or `docker ps --format '{{.Ports}}' | grep 8088`). If taken, set `HOST_UI_PORT=8888` (or another free port) in `.env` AND update `PUBLIC_URL` to match.

3. **Set a strong FileBrowser password** (≥12 chars, not weak/common). Recent FileBrowser silently rejects weak passwords; default `admin`/`changeme` will fail login.

4. **`allowed_formats` default is `pdf`** (since v1.0.3). If user wants to ingest other types (docx, md, txt), set it in Tab 07 → System Settings after deploy. The legacy default `xlsx` would have caused PDF uploads to be silently filtered out.

5. **Verify n8n credentials exist**: the imported workflows reference `OpenAI` and other credentials by id. After `python deploy_n8n.py`, ask the user to open n8n → Settings → Credentials, create or rebind `OpenAI` (or whichever LLM provider you wired) before training will work.

## What this app needs from OpenGenie

| OpenGenie service | This app uses it for |
|---|---|
| n8n | RAG ingestion + query workflows (you import them) |
| Qdrant | vector database |
| Open WebUI | chat apps (you install 2 pipe functions) |
| Docling | PDF → Markdown |
| PostgreSQL | metadata (projects/rules/keywords/chat-apps) |
| Redis | used by n8n |
| FileBrowser | shared RAG files volume |

Continue in **[INSTALL.md](INSTALL.md)**.
