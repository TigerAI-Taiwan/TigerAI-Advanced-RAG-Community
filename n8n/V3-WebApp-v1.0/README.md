# V3-WebApp-v1.0 — TigerAI-WebApp 配套 n8n Workflows

> **目錄定位**：搭配 **TigerAI-WebApp v1.0（含 QC Audit 付費版）** 的 n8n workflow 完整集合。匯入這 5 個就夠。

## 版本對應

| 元件 | 版本 | 日期 |
|------|------|------|
| TigerAI-WebApp | v1.0（Tab 01 Rule Builder + Tab 08 QC Audit） | 2026-04-20 |
| n8n Workflows | V3-WebApp-v1.0（本目錄，2026-04-21 重新編號） | 2026-04-21 |
| 前身 | 前代 交付版 8 個 workflow → 本版合併/通用化為 5 個 | — |

## 檔案清單（編號 01–05 連續）

| 檔案 | Webhook path | 對應流程 |
|------|--------------|----------|
| `#01-DataTransferring-Docling-Generic-v1.2.json` | `/webhook/tigerai-step1` | PDF → Markdown（Docling，`folder_id` 防呆） |
| `#02-DataProcessing-MD2JSON-Generic-v1.2.json`   | `/webhook/tigerai-step2` | MD → JSON（**ollama↔cloud_ai 自動切換**，`folder_id` 防呆） |
| `#03-DataValidation-MD2QA-Generic-v1.3.json`     | `/webhook/tigerai-step3` | 讀全部 MD → 產 10 題 QA（**ollama↔cloud_ai**）→ 寫檔 + POST DB + **callback `/webhook/n8n`** |
| `#04-DataIngestion-Generic-v1.3.json`            | `/webhook/tigerai-step4` | JSON → Qdrant（**ollama↔cloud_ai embedding 自動切換**） |
| `#05-Query-Generic-v1.2.json`                    | `/webhook/tigerai-query` | 線上查詢兩階段 AI（Agent1 / Embed / Agent2 **各自獨立判斷 ollama↔cloud_ai**） |

每個 JSON 內部的 sticky note 都有 **版本歷史** 區塊。

## Ollama ↔ Cloud AI 自動切換（v1.2/v1.3）

所有會打 LLM / embedding 的 workflow 節點前都插一個 IF 判斷，規則一致：

```text
if ollama_main_model      notEmpty → Ollama /api/chat         else → Cloud AI /chat/completions
if ollama_embedding_model notEmpty → Ollama /api/embed        else → Cloud AI /embeddings
```

Cloud AI 端使用 OpenAI-compatible 介面，需 `cloud_ai_url`、`cloud_ai_key`、`cloud_ai_model` / `cloud_ai_embedding_model`。所有 parser 同時支援 Ollama (`message.content` / `embeddings[0]`) 與 OpenAI (`choices[0].message.content` / `data[0].embedding`) 回傳格式。

## QC 功能分工

| QC 類型 | n8n | Webapp |
|---------|-----|--------|
| 產 10 題人工測試 | **#03 workflow**（手動觸發，給客戶看的入口）→ POST `/qc/save-test-questions` | `generateTestQuestions()` 自動版（專案跑完自動產） |
| 覆蓋率檢查（原料→VectorDB） | — | Tab 08 `POST /qc/coverage` |
| RAG 答題品質稽核（雙 AI） | — | Tab 08 `POST /qc/rag-audit` |
| 線上查詢（兩階段 AI） | **#07 workflow**：Agent1 意圖判斷 + Agent2 RAG 答題 | Tab 04 AI Chat Verification 可串接 |

#03 與 backend 寫同一個 DB 表（`qc_test_questions`），結案報告會自動列出 10 題。

## 關鍵字清單（結案報告）

Webapp `generateFinalReport()` 會掃所有 `Step3-MD2JSON/*.json` 抽出以下欄位到完成報告：
- `level1` / `l1` / `L1` / `category`
- `level2` / `l2` / `L2` / `subcategory`
- `level3` / `l3` / `L3`
- `tags` / `keywords` / `metadata_tags`

產出格式：L1/L2/L3 階層樹 + 各層獨立清單 + 全部標籤，**可複製進 Tab 03 Keyword Dictionary** 或 **#07 query 的 `keywords[]` 欄位**做 OR filter。

> 若 Rule 的 `system_prompt` 沒要求輸出這些欄位，報告會提示你去改 Rule。

## 設計原則（v1.0）

- **Webhook 觸發**（backend 直呼，不用 manual trigger）
- **完全 payload-driven**：Ollama URL/model、Qdrant URL/key、collection、system_prompt 都由 payload 帶入，workflow 內部不寫死任何主機名 / 模型名
- **繞過 LangChain Default Data Loader** → 直打 Qdrant HTTP API，metadata schema 完全自由
- **system_prompt 由 Rule 動態帶入**（Meta-Prompt 產出 → `projects_metadata.system_prompt_snapshot` 落錨 → 觸發時塞進 payload）
- **UUID v5 Point ID**（`original_file#chunk_index`）→ 冪等 upsert，同檔重跑會 overwrite 而不重複

## 部署步驟

### 1. 匯入 workflow

在 n8n UI → Workflows → Import from File，依序匯入這 3 個 `-v1.0.json`。**先不要 Activate**，先檢查 credentials 和路徑。

### 2. 對齊 webhook path（Tab 06 系統設定）

Tab 06 UI 已同步成 5 個欄位，直接填：

| TigerAI 欄位 | 填入 |
|-------------|------|
| #01 Step 1 (Docling PDF→MD) | `/webhook/tigerai-step1` |
| #02 Step 2 (MD→JSON) | `/webhook/tigerai-step2` |
| #03 Step 3 (MD→10 QA 測試題) | `/webhook/tigerai-step3` |
| #04 Step 4 (JSON→Qdrant 灌庫) | `/webhook/tigerai-step4` |
| #05 線上查詢 (Query) | `/webhook/tigerai-query` |

backend 狀態機已壓縮（v1.1）：`Step 01 → Step 02 → Step 03 → Step 43 已完成`（原 Step 41/42/43 合併成 #04 一個 workflow，不再分 3 步）。

### 3. Ollama 設定（Tab 06）

1. Base URL 填 `http://ollama:11434`
2. 按 **Refresh Models**
3. 選 **Main Model**（例 `qwen3:32b`，#02 + QC 稽核 AI 用）
4. 選 **Embedding Model**（例 `qwen3-embedding-8b`，#05 + QC 覆蓋率用）
5. 按 **📏** 測維度
6. Save

### 4. Qdrant Collection 維度

`vectors.size` 必須等於 Embedding Model 回的維度：

```bash
curl -X PUT http://localhost:6333/collections/your_collection \
  -H "Content-Type: application/json" \
  -d '{"vectors": {"size": 4096, "distance": "Cosine"}}'
```

（Qwen3-Embedding-8B = 4096 維）

## Payload 規格（backend → n8n）

```json
{
  "folder_path": "/proj_A",
  "project_name": "proj_A",
  "collection_name": "labor_law_2026",
  "system_prompt": "...(from Rule)...",
  "allowed_formats": "pdf",
  "ollama_url": "http://ollama:11434",
  "ollama_main_model": "qwen3:32b",
  "ollama_embedding_model": "qwen3-embedding-8b",
  "ollama_embedding_dim": "4096",
  "cloud_ai_url": "https://api.openai.com/v1",
  "cloud_ai_key": "sk-...",
  "cloud_ai_model": "gpt-5-mini",
  "cloud_ai_embedding_model": "text-embedding-3-large"
}
```

每個 workflow 只取自己需要的欄位。**Ollama 欄位為空時自動 fallback 到 cloud_ai**。

## Qdrant Payload 結構（#05 寫入）

Point ID：`uuidV5(original_file + '#' + chunk_index, NAMESPACE)`

```json
{
  "text": "第十一條 ...",
  "original_file": "工作規則.pdf",
  "source_file": "工作規則.md",
  "chunk_index": 3,
  "project_name": "proj_A",
  "document_title": "...",
  "article_id": "第十一條",
  "section_id": "二",
  "tags": ["..."],
  "suggested_questions": ["..."]
}
```

所有 metadata 欄位平鋪到 top-level，具體欄位由 Rule 的 System Prompt 決定。

### 依檔案刪除

```bash
curl -X POST http://localhost:6333/collections/your_collection/points/delete \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"original_file","match":{"value":"工作規則.pdf"}}]}}'
```

### 依專案刪除

```bash
curl -X POST http://localhost:6333/collections/your_collection/points/delete \
  -d '{"filter":{"must":[{"key":"project_name","match":{"value":"proj_A"}}]}}'
```

## 壓縮狀態機（選配）

讓 backend 狀態機對齊 3-step 流程：

```js
// backend/index.js - getStepsSeq()
return {
  'Step 01 進行中': { nextStatus: 'Step 03 進行中', webhook: `${base}${appConfig.wh_step3}`, subfolder: 'Step1-File2MD' },
  'Step 03 進行中': { nextStatus: 'Step 43 進行中', webhook: `${base}${appConfig.wh_step41}`, subfolder: 'Step3-MD2JSON' },
  'Step 43 進行中': { nextStatus: '已完成' }
};
```

## 測試

```bash
# 手動觸發 #01
curl -X POST http://localhost:5678/webhook/tigerai-step1 \
  -H "Content-Type: application/json" \
  -d '{"folder_path":"/test_proj","project_name":"test_proj","collection_name":"test_col","allowed_formats":"pdf"}'
```

正常流程：n8n #01/#02/#05 跑完 → 專案狀態變「已完成」→ Webapp `generateFinalReport()` 自動執行 QC-1（產 10 題附報告）→ 使用者可到 Tab 08 手動跑 QC-2 / QC-3。

## 除錯

### Q: Ollama 404 Model Not Found
```bash
docker exec ollama ollama list   # 確認 Tab 06 選的 model 名稱真的存在
```

### Q: Qdrant dimension mismatch
Collection 建立時的 `vectors.size` 必須等於 Ollama 回的維度。用 Tab 06 的 📏 重測。

### Q: n8n workflow 沒觸發
1. Workflow 已 **Active**？
2. Tab 06 webhook path 對？
3. `docker logs tigerai-backend | grep webhook` 有沒有呼叫記錄
4. `docker logs n8n` 有沒有收到

### Q: #02 LLM 輸出不是合法 JSON
`Parse + Tag JSON` 節點會嘗試剝 markdown code fence、再找 `[...]`。若失敗：
1. Rule 的 System Prompt 有無明確要求「只輸出 JSON Array」
2. Ollama model 是否支援 JSON 模式
3. 把 temperature 降到 0（目前 0.2）

## 升級路徑

未來若新增 workflow（例如 #07 線上查詢給 OpenWebUI），請另開 `V3-WebApp-v1.1/` 目錄，不要就地覆寫，保留可回溯。
