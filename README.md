# TigerAI Advanced RAG — Community Edition

[繁體中文](#繁體中文) ｜ English

An open-source, web-based platform for building **RAG knowledge bases** — and a **reference application for the [OpenGenie AI Stack](https://github.com/TigerAI-Taiwan/OpenGenie-AI-Stack)**: it shows how to turn that self-hosted AI infrastructure into a real, working product.

Upload PDFs → split → convert (Docling) → chunk + metadata/keywords → ingest to Qdrant → chat through Open WebUI, with **downloadable source citations** in every answer. Fully **bilingual UI (EN / 中文)**.

> **License:** Apache-2.0 · **Status:** Community Edition (free)

---

## How it fits with the OpenGenie AI Stack

```
┌───────────────────────────────────────────────┐
│  TigerAI Advanced RAG — Community  (this app)   │  ← reference application
│  nginx UI · backend · PDF splitter              │
├───────────────────────────────────────────────┤
│  OpenGenie AI Stack  (the infrastructure)       │  ← deploy this first
│  n8n · Qdrant · Open WebUI · Docling ·          │
│  PostgreSQL · Redis · FileBrowser               │
└───────────────────────────────────────────────┘
```

The **OpenGenie AI Stack** provides the engines (workflow automation, vector DB, document conversion, chat UI, database). **TigerAI RAG Community** is the application layer on top — proving how the stack is used end-to-end.

## What you can do

- **Build knowledge bases** from PDFs: server-side split → Docling Markdown → JSON chunking → metadata + L1/L2/L3 keyword dictionary → Qdrant ingest
- **Rule Builder**: generate a domain system-prompt from sample documents (A/B/C → pick or synthesize)
- **Chat verification & QC**: coverage check (source ↔ vector DB) and dual-AI answer audit
- **Cloud chat apps**: wire an Open WebUI app to an n8n query webhook in two clicks
- **Citations with download links**: answers list the source files used, each downloadable

## Editions

| | **Community** (this repo) | **Pro / Enterprise** |
|---|---|---|
| AI | **Cloud** (any OpenAI-compatible endpoint) | **+ On-premise / local** (Ollama, llama.cpp, vLLM) |
| Retrieval | Vector + metadata filtering | **+ Reranking, multimodal catalog RAG, trustworthy QC** |
| Training | Direct training | **+ Scheduling queue** |
| Price | Free, Apache-2.0 | Commercial |

> Need **local / on-prem AI, reranking, or multimodal catalog RAG**? Those are the paid tiers — get in touch.

## Quick start — hand it to your AI agent (no manual setup)

Open an AI coding agent (**Claude Code / Antigravity / Codex**) and give it **two URLs**:

1. https://github.com/TigerAI-Taiwan/OpenGenie-AI-Stack — the infrastructure
2. this repo — the application

The agent reads both and **installs + wires them end-to-end** — no human steps. It only asks you for a **Cloud AI API key**. See **[AGENTS.md](AGENTS.md)** (agent entry) and **[INSTALL.md](INSTALL.md)** (the runbook it executes).

---

<a name="繁體中文"></a>
## 繁體中文

開源、網頁式的 **RAG 知識庫建置平台**,同時是 **[OpenGenie AI Stack](https://github.com/TigerAI-Taiwan/OpenGenie-AI-Stack) 的官方範例應用** —— 示範如何把這套自架 AI 基礎設施變成一個真正可用的產品。

上傳 PDF → 切檔 → Docling 轉檔 → 切塊 + metadata/關鍵字 → 灌入 Qdrant → 透過 Open WebUI 問答,**每個答案都附可下載的原始檔出處**。介面 **中英雙語(EN / 中文)**。

**與 OpenGenie 的關係**:OpenGenie AI Stack 提供引擎(n8n / Qdrant / Open WebUI / Docling / PostgreSQL / Redis / FileBrowser);本專案是跑在上面的**應用層**,完整示範這套 stack 怎麼被運用。

**版本差異**:Community(本 repo,免費、Apache-2.0、**雲端 AI**)＝陽春雲端版;**地端/on-prem、Rerank 重排、多模態型錄 RAG、可信 QC、排程佇列** 屬付費版 —— 需要請洽詢。

**安裝(免動手)**:打開 AI coding agent(**Claude Code / Antigravity / Codex**),把**兩個 URL** 丟給它 —— ① [OpenGenie AI Stack](https://github.com/TigerAI-Taiwan/OpenGenie-AI-Stack)(基礎設施)② 本 repo(應用)。Agent 會自己讀完兩個 repo、**安裝並串接好**,人不用介入;只會跟你要一把 **Cloud AI API key**。詳見 **[AGENTS.md](AGENTS.md)** / **[INSTALL.md](INSTALL.md)**。
