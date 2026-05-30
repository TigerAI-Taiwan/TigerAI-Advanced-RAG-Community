# STACK-REFERENCE — OpenGenie AI Stack 整合參考

> **這份不是規定,是參考**。每台機器的 OpenGenie 部署都可能不同(AMD / ARM64 / NVIDIA 三套 stack、不同版本、客戶自訂 fork)。本文件給 install AI 一個「OpenGenie 通常長什麼樣」的基準,然後 AI 自己判斷現場是哪個變體、要怎麼接 TigerAI。
>
> **TigerAI 本身不依賴特定 OpenGenie 變體**。只要找得到對應的服務、連得上、權限對,就能跑。AI 的工作是 **probe 實際機況 → 對照本文件 → 自己生成 .env**。

---

## 1. TigerAI 需要 OpenGenie 提供什麼

| 服務 | 用途 | 對 TigerAI 的最小要求 |
|---|---|---|
| **PostgreSQL** | metadata(projects / rules / keywords / chat-apps) | TCP 連得到、可建 schema(預設用 `tigerai_webapp` schema)|
| **Redis** | n8n queue + (optional) backend session cache | TCP 連得到、有寫權限的 DB index(n8n 通常用 DB 0;TigerAI backend 預設用 DB 2 避開衝突) |
| **n8n** | RAG ingestion + query workflows | REST API 可達、有 API key、容器內可寫 `/home/node/.n8n-files/` |
| **Qdrant** | vector database | HTTP API 可達(預設 port 6333) |
| **Open WebUI** | chat 入口 | REST API 可達(預設 port 8080) |
| **Docling** | PDF → Markdown | HTTP API 可達(預設 port 5001) |
| **FileBrowser** | 共享 RAG 檔案 UI(讓使用者拖拉上傳) | **OpenGenie 預設不含 FB,所以 TigerAI 自己 bundle 一個進 docker-compose**(container name `filebrowser`,`--noauth` 跑於內部 docker network,對外經 nginx /api/ proxy 走我方 auth)。Install AI 不用管,跟著 `docker compose up -d` 就會起來 |
| **Ollama**(選用) | 地端 LLM(Community edition 不需要) | — |

**關鍵設計原則:TigerAI 透過 `appConfig`(Tab 07 設定 / .env / DB)拿這些 URL,不寫死任何 container 名稱**。所以名字怎麼變都行,只要 .env 正確。

---

## 2. OpenGenie 三個變體的差異(摘自其 stack 設計)

OpenGenie 依硬體平台出三個 compose stack。**container 命名 / volume 掛載 / 環境變數 在三個之間有差異**。AI 必須 `docker ps` 偵測,別假設:

| 服務 | AMD Stack | ARM64 Stack | NVIDIA Stack |
|---|---|---|---|
| Redis | `redis` | `redis` | `redis` |
| PostgreSQL | `postgres` | `postgres` | `postgres` |
| pgAdmin | `pgadmin` | `pgadmin` | `pgadmin` |
| Qdrant | `qdrant` | `qdrant` | **`qdrant-nvidia`** |
| OpenWebUI main | `openwebui-main` | `openwebui-main` | `openwebui-main` |
| OpenWebUI worker | (無 container_name,可 scale) | (同左) | `openwebui-worker-01` + `openwebui-worker-02` |
| n8n main | `n8n-main` | `n8n-main` | `n8n-main` |
| n8n worker | (無 container_name,可 scale) | (同左) | (同左) |
| Ollama volume | `/var/lib/ollama` bind | `ollama_data` named | `/var/lib/ollama` bind |
| Qdrant data | `qdrant_storage` named | 同 AMD | `${BASE_DIR:-/home/wrt/TigerAI}/qdrant` bind |

**Network 名稱(三個 stack 共用)**:`ai_stack_net`(external:true,必須事先建立)

---

## 3. OpenGenie 內部連線預設值(AI 可拿來推導 .env)

### PostgreSQL(三變體共通)
- 容器:`postgres`
- Port:**不對外**(只 internal network,從其他容器看 `postgres:5432`)
- Database:`tigerai`
- User:`adm`
- Password:`tigerai`(**OpenGenie 預設值,正式環境必須改**)
- Schemas:`openwebui`(OWUI 用)、`public`(n8n 用)、TigerAI 會自建 `tigerai_webapp`

### Redis(三變體共通)
- 容器:`redis`
- Port:不對外(從 internal 看 `redis:6379`)
- DB 0:n8n Bull queue
- DB 1:OpenWebUI queue
- DB 2:**TigerAI backend 預設用這個(避開 n8n/OWUI)**

### Qdrant
- 容器:見 §2(AMD/ARM 是 `qdrant`,NVIDIA 是 `qdrant-nvidia`)
- Port:`6333:6333` HTTP REST、`6334:6334` gRPC
- 從 internal 看 `<container_name>:6333`

### OpenWebUI
- main 容器:`openwebui-main`
- Port:`8080:8080`
- 從 internal 看 `openwebui-main:8080`
- 連 OpenGenie 內 Ollama:`http://ollama:11434`
- 連 OpenGenie 內 PG:`postgresql://adm:tigerai@postgres:5432/tigerai?options=-csearch_path=openwebui`

### n8n
- main 容器:`n8n-main`
- Port:`5678:5678`
- 從 internal 看 `n8n-main:5678`
- DB:`postgresql` (host `postgres`, port 5432, user `adm`, pass `tigerai`, db `tigerai`, schema `public`)
- Redis:`redis:6379` DB 0
- API key:**AI 必須引導 user 到 n8n UI 建立**(Settings → API)。權限要含 `workflows:*` + `credentials:read`(用於 deploy_n8n.py rewire)

### Docling
- 容器名:**stack.md 沒明確記錄**,常見 `docling` / `docling-cpu` / `docling-gpu` / `docling-intel`
- Port:`5001`
- AI 用 `docker ps | grep -i docling` 找

### FileBrowser
- **OpenGenie 預設不含**,所以 **TigerAI 自帶**(我們的 docker-compose.yml 內建 `tigerai-filebrowser` 服務,container name `filebrowser`)
- 模式:`--noauth`(內部 docker network only,對外經我方 nginx /api/ proxy)
- 掛載:跟 splitter 同一個 RAG_FILES_PATH(`/srv`)
- Install AI 不用配 / 不用設密碼 / 不用裝額外的;`docker compose up -d` 我方 4 個容器(nginx + backend + splitter + filebrowser)一次起來

---

## 4. AI 該怎麼用本文件(建議流程)

```
1. 讀 AGENTS.md 理解 TigerAI 在做什麼、需要什麼
2. 讀本文件理解 OpenGenie 通常長什麼樣
3. 跑 deploy_n8n.py --probe → 拿到當前機器 docker ps / networks / mounts 的 JSON 報告
4. 對比 probe 結果 × 本文件 → 自己判斷:
   - 是哪個 OpenGenie 變體?(看 Qdrant 名稱:qdrant 還是 qdrant-nvidia)
   - 缺哪些服務?(沒 postgres → 不能裝 TigerAI,先去裝 OpenGenie)
   - container 名跟參考不同?(可能是 fork / 改名,自己對應)
   - PG/Redis 帳密是預設還是改過?(docker inspect 看 env)
5. 自己寫一份 settings.json(根據 probe 結果決定的真實值)
6. deploy_n8n.py --apply settings.json
7. deploy_n8n.py --import-workflows
8. 自己用瀏覽器 / curl 驗收
```

**不用照本文件死背任何值**。本文件給的是「OpenGenie 預設出廠長這樣」,但每台機器都可能不同 — AI 的工作就是把差異弄清楚。

---

## 5. TigerAI 自帶的 3 個容器(這部分 TigerAI 控,跟 OpenGenie 解耦)

- `tigerai-nginx`(對外 UI port,預設 8088,可用 `HOST_UI_PORT` 覆寫)
- `tigerai-backend`(REST API,internal port 3060)
- `tigerai-splitter`(PDF 預處理,internal port 8000)

這 3 個 image 從 ghcr.io/tigerai-taiwan/ 拉,multi-arch(amd64 + arm64,含 Grace/GB10)。AI 不用操心這 3 個容器內部 — 只要把它們 plug 進對的 docker network,設好 .env 指向正確的 OpenGenie 服務即可。

---

## 6. 常見會跨環境差異的設定(AI 要 probe 確認)

| 設定項 | 為什麼會變 | 怎麼確認 |
|---|---|---|
| `*_HOST` container 名 | 三變體差異 / fork / 改名 | `docker ps --format '{{.Names}}'` |
| `STACK_NETWORK` | OpenGenie 用 `ai_stack_net`,但 fork 可能改 | `docker network ls` |
| `PG_PASSWORD` | 預設 `tigerai`,正式環境會改 | `docker inspect postgres --format '{{.Config.Env}}'` 找 `POSTGRES_PASSWORD` |
| `PG_USER` | OpenGenie 預設 `adm`(注意!不是 `postgres`) | 同上找 `POSTGRES_USER` |
| `RAG_FILES_PATH` host 路徑 | 每台不同 | `docker inspect n8n-main --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'` 找 `/home/node/.n8n-files` 的 Source |
| `HOST_UI_PORT` | 8088 常被 cAdvisor 佔 | `ss -lnt | grep 8088` 或 `lsof -i:8088` |
| n8n container 名(`n8n-main` vs `n8n` vs 其他) | 變體差異 | `docker ps | grep n8n` |
| Docling container 名 | 多版本(intel/gpu/cpu) | `docker ps | grep docling` |
| FileBrowser 有沒有裝 | OpenGenie 預設無 | `docker ps | grep -i file` |

---

## 7. 不在本文件範圍的事(別在這找答案)

- TigerAI 內部 workflow 的設計(看 [SDD.md] / TigerAI 自己的文件)
- 客戶資料、密鑰、production tuning(看部署現場的 secret 管理)
- OpenGenie 本身的安裝(看 [OpenGenie repo](https://github.com/TigerAI-Taiwan/OpenGenie-AI-Stack))

---

*本文件刻意不含實際密鑰。AI 要拿到密鑰請自己 `docker inspect` 從 container env 抽,或請使用者提供。*
