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
