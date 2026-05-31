# TigerAI Community Edition — 整合學習與避坑指南

> 來源:2026-05-31 Gemini 在遠端機 fresh install Community 連續踩 8 個坑(v1.0.10–v1.0.17 修)。
> 本文件記錄踩過的錯、根因、修法、與 deploy SOP,給日後 AI agent / 工程師避雷用。

---

## 1. 核心踩坑紀錄(8 個 release)

### v1.0.10 — nginx envsubst bypass
- **錯**:docker-compose nginx `command: exec nginx -g 'daemon off;'` 繞過 nginx:alpine 的 entrypoint init scripts(特別是 `20-envsubst-on-templates.sh`)
- **果**:nginx 啟動時不處理 `/etc/nginx/templates/*.template`,conf.d/ 空 → 所有 proxy 404 → UI JSON.parse 失敗「unexpected character」
- **修**:`exec /docker-entrypoint.sh nginx -g 'daemon off;'`(走完 init scripts)

### v1.0.11 — FileBrowser 不在 OpenGenie 預設
- **錯**:`STACK-REFERENCE.md` 寫 FB 是「選用」、要 user 自己裝
- **果**:Tab 02 上傳功能整個壞掉(沒 FB 後端找不到)
- **修**:docker-compose 自帶 `filebrowser` service,`--noauth` 內部 docker network only

### v1.0.12 — FileBrowser `--root` 範圍錯
- **錯**:FB `--root=/srv`(整個 RAG_FILES_PATH 為根)
- **果**:Tab 02 訓練專案列表把 n8n 雜檔跟 RAG/ 子目錄一起當「眾多專案」列出
- **修**:`--root=/srv/RAG`(只看 RAG/ 訓練專案命名空間)

### v1.0.13 — RAG/ root convention 全 chain 對齊
- **錯**:FB --root 改 /srv/RAG 後,backend `SPLITTER_FILES_ROOT` / splitter `FILES_ROOT` / nginx `/files/` mount 沒跟著
- **果**:啟動鈕「此專案尚未上傳任何檔案」(has-source 找錯路徑)+ 答案下載連結 404
- **修**:三處同步改:
  - backend `SPLITTER_FILES_ROOT=/srv/RAG`
  - splitter `FILES_ROOT=/srv/RAG`
  - nginx mount `RAG_FILES_PATH/RAG:/usr/share/nginx/files`

### v1.0.14 — n8n workflow executeWorkflow id rewire
- **錯**:source JSON 寫死 source 機器的 workflow id(`WePsMURN28E7ECrI` / `0UQpFS2dW2As1JGL`)且 `cachedResultName` 用短名(`Sub-Chat-Cloud` 對不上實際 `Sub-Chat-Cloud-v1.0`)
- **果**:fresh install 5 個 workflow 全 publish 失敗「Cannot publish workflow: references workflow X which is not published」
- **修**:所有寫死 id → `__REWIRE_BY_NAME__` placeholder + cachedResultName 全名;deploy_n8n.py by-name rewire 邏輯本來就對

### v1.0.15 — 全 audit 55 confirmed bugs 一次修
- 17 個 fixer agent 跑 source / docs / polish
- 重點:#02 dedup 6 重複 node id、移除 workflow hardcoded fallback URL、Sub-Chat-Cloud 加 local/cloud 分流、預設密碼洩漏 log 移除、dispatchTrainingItem 錯誤 fail-loud、splitter jobs TTL/上限 + PDF handle close、app.js 10+ 處 XSS escapeHtml、deploy_n8n.py --force 不再跳過 cred check
- 追加修 Gemini 4 個 install 坑:
  - FB scratch image 沒 /bin/sh → 移 entrypoint,改 `depends_on tigerai-splitter condition: service_healthy`
  - LLM temperature 0.2 寫死(o1 reject)→ 4 處全清,讓 OpenAI 用 default

### v1.0.16 — OWUI key/url 寫死不讀 appConfig
- **錯**:`owuiRequest` / `createOwuiPipeline` / `deleteOwuiPipeline` 用 startup const `OWUI_API_KEY` / `OWUI_BASE_URL`,不讀 `appConfig`
- **果**:Tab 07 改 OWUI key/url 完全沒效(必須 docker exec 寫 .env + 重啟)→ chat-app 建立失敗「OWUI Pipeline 建立失敗」
- **修**:3 處改 `appConfig.owui_key || OWUI_API_KEY`(對齊 cloud_ai_key/n8n_key/qdrant_key 早就有的正確模式)
- 順手:N8N_HOST 預設 `ai-customer-service-n8n` → `n8n-main`(對齊 OpenGenie)

### v1.0.17 — #05-RAG-Core expression 內 literal newline
- **錯**:line 423 Build RAG Context expression `={{ ... '...\n...' ... }}`,JSON 內 `\n` decode 後是 raw newline char → n8n JS engine 解析 `'...實體換行...'` → SyntaxError「invalid syntax」
- **果**:RAG 查詢全死
- **修**:JSON 內 `\n` → `\\n`(decode 後變 JS escape sequence,engine 正常解析)

---

## 2. 三層系統架構認知

```
┌──────────────────────────────────────────────────────────┐
│  TigerAI WebApp(中樞神經 / Single Source of Truth)        │
│  - appConfig(DB system_settings)= 唯一設定來源            │
│  - 向 OWUI 註冊新的 chat-app pipeline                       │
│  - 觸發 n8n workflow                                      │
└──────────────────────┬───────────────────────────────────┘
                       │
       ┌───────────────┼───────────────┐
       ▼                               ▼
┌────────────────────┐         ┌────────────────────────┐
│ Open WebUI(展示層) │         │ n8n(大腦與執行引擎)    │
│ - 純 UI shell      │         │ - RAG 邏輯 / 資料清洗   │
│ - Pipeline 範本    │         │ - Qdrant 查詢 / embed   │
│   永遠 Inactive    │         │ - 子工作流 by-name      │
│ - WebApp 複製出的  │         │   rewire 後參照         │
│   chat-app 才      │         │ - HTTP 必須 retry on    │
│   Active           │         │   fail 抵抗網路瞬斷     │
└────────────────────┘         └────────────────────────┘
```

**Single Source of Truth 原則**:所有 API key、URL、Webhook、collection_name 都存在 WebApp 的 `appConfig`(Tab 07 改即時生效)。OWUI Pipeline 內存的 `valves` 只是 WebApp 推進去的「快取」,不負責保管邏輯。

**Template vs Instance 模型**:
- **Template Pipeline**(`n8n_pipeline_v212` / `n8n_pipeline_v052`)= 藍圖,放 OWUI 上**永遠 Inactive**
- **Instance Pipeline**(`labor01` / `labor02` 等 chat-app)= WebApp 從 template 複製 → 改名 → 寫入專屬 n8n webhook URL + app_name → activate
- **絕對不要**在 OWUI 直接 active template,會吃掉 chat-app 選單

---

## 3. 正確部署 SOP

### Step 1:基礎環境
```bash
# 1.1 .env 必填(尤其 OWUI_API_KEY,Tab 07 後可改但首次至少要有值)
cp .env.example .env
vi .env  # 至少填:OWUI_API_KEY / PG_PASSWORD / RAG_FILES_PATH

# 1.2 啟動 4 容器
docker compose up -d
docker compose ps  # 4 個 healthy:nginx / tigerai-backend / tigerai-splitter / filebrowser
```

### Step 2:n8n workflow 部署
```bash
# 2.1 deploy 自動 import(需要 N8N_API_KEY 含 workflows:* + credentials:read)
python3 deploy_n8n.py --apply settings.json --import-workflows

# 2.2 確認 9 個 workflow 全部 active 且 cred-broken=0
#     若 cred-broken 有值 → 到 n8n UI 手動綁 OpenAI credential 後重 publish
```

### Step 3:OWUI Pipeline 範本部署
```bash
# 3.1 上傳 n8n_pipeline_v212.py / n8n_pipeline_v052.py 到 OWUI
#     ⚠️ 上傳 ID 必須跟 WebApp 預設範本 ID 一致:
#        - n8n_pipeline_v212 (串流)
#        - n8n_pipeline_v052 (非串流)
#     若取了別名(如 n8n_v2),WebApp 找不到 → 建 chat-app 永遠失敗

# 3.2 確認這些範本 Inactive(別自作聰明 activate)
```

### Step 4:建立 Chat App
- 進入 TigerAI WebApp Tab 03(或 Tab 06)
- 填 App 名稱 (英文+數字,如 `labor01`)
- 選 collection、Rule、Pipeline 範本
- 點 儲存 → 應該看到「✅ OWUI Pipeline 已建立: labor01」
- WebApp 自動在 OWUI 複製 template → 改名 labor01 → 寫 valves → activate

### Step 5:驗證 chain
```bash
# 5.1 Tab 02 點啟動專案 → Step 0/1/2/3/4 跑完
# 5.2 OWUI 選 labor01 model → 發訊息 → 應該 RAG 答覆 + reference 連結
# 5.3 點 reference 連結 → 應該下載到原檔(/backend/dl 反查 /files/)
```

---

## 4. 已知還沒修(下次 release 候選)

- **deploy_n8n.py 加 OWUI pipeline auto-import**:目前 `deployments/owui/*.py` 要 user 手動匯入,容易踩 ID 命名坑(坑 2)
- **`createOwuiPipeline` 找不到 template 時 fallback error message 改具體**:「OWUI 上找不到 template `n8n_pipeline_v052`,請先匯入 `deployments/owui/n8n_pipeline_v052.py`」而非籠統「Pipeline 建立失敗」
- **HTTP request 加 retry on transient error**:#04 Cloud AI Embed 等 OpenAI 呼叫加 `retryOnFail: true, retryAttempts: 3` 抵抗 DNS/網路 hiccup
- **fresh-install smoke sandbox**:任何 v1.0.x release 前在乾淨 docker-compose 跑 deploy_n8n.py + 第一個 chat-app 建立 + 第一筆訓練 + 第一次 RAG 查詢,通才能發

---

## 5. 跨坑 takeaway

- 「我自己機器跑得起來 → release」是錯的驗證 — 我機器既有的 cache/cred/workflow id 都會掩蓋 source bug
- **沒有 fresh-install smoke test 的 release 等於沒驗證**
- service key 一律從 `appConfig` 動態讀(對齊 cloud_ai_key/ollama_api_key/n8n_key/qdrant_key/docling_url 的正確模式),不要在新地方寫死 startup const
- n8n workflow export 前必跑 grep 確認:無 hardcoded 16-char base62 workflow/cred id、無 expression 內 string literal raw newline
- upstream image(nginx / filebrowser)用原名 service,不冠 `tigerai-`;自製(`tigerai-backend` / `tigerai-splitter`)才冠
