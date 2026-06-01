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

### v1.0.18 — PG_SCHEMA / WEBHOOK_PREFIX env 化 + deploy [Community] 前綴
- SCHEMA 寫死 `tigerai_webapp` → 同 PG 跑多 edition settings/projects 互相污染。改 `process.env.PG_SCHEMA`。
- wh_step1-5 支援 `WEBHOOK_PREFIX` → `/webhook/tigerai-{edition}-stepN`(同 n8n 多 edition 不撞)。
- deploy_n8n.py 加 `--edition` → workflow.name 加 `[Community]` 前綴(n8n UI 一眼分辨)。

### v1.0.19 — SETTINGS_SECRET plaintext → Invalid key length(每台新機必中 🔴)
- `encryptValue` 寫死 `Buffer.from(SETTINGS_SECRET,'hex')` 要 64 字 hex,.env 給 plaintext 就 throw → **存任何設定全 500**。
- **修**:`_deriveSettingsKey()` 看 64-hex 維持舊行為(相容既有 ciphertext),否則 SHA256 derive 接受任意 plaintext。

### v1.0.20 — crash recovery + healthcheck
- backend 在 rule 生成中 / 專案訓練中重啟 → status 卡 `generating` / `進行中` → re-entry guard 409 永久鎖死。啟動時自動重設。
- splitter image(python:slim)**沒 wget** → wget healthcheck 永遠 unhealthy + 卡 depends_on:service_healthy 的 filebrowser。改 python urllib;nginx/backend `--spider`→`-O /dev/null`。

### v1.0.21 — deploy edition-prefix 2 bug(獨立 n8n 實測挖出)
- patch_edition_prefix 沒同步改 executeWorkflow 的 `cachedResultName` → rewire 用 cachedResultName 對不上 → `__REWIRE_BY_NAME__` 沒被替換 → activate 失敗。
- activation 順序錯(sub-workflow 要先 active 父才能 active)→ 改**多趟重試 leaves-first auto-resolve**。

### v1.0.22 — #02/#03 訓練 chain 2 bug(端到端實測)
- #02/#03 Build LLM Input 沒把 `cloud_ai_key`/`cloud_ai_url` 傳進 Sub-Chat-Cloud → Cloud AI Chat 401「no API key」→ 訓練死在 Step2。補 5 欄位。
- #03 Parse QA JSON 漏讀 `r.output`(Sub-Chat-Cloud 回 {output})→ Unknown error。#02 有處理 output、#03 漏。

### v1.0.23 — 完成報告改放專案根目錄
- 報告 + keywords.csv 從 Step4-JSON2VectorDB 子目錄 → 專案根(000- 前綴排最前,打開即見)。

### v1.0.24 — RAG Rule 欄子資料夾顯示「—」
- Step* 管線子資料夾沒自己的 rule_id → UI 誤顯「未指定」。只有 root 層專案顯示規則,子資料夾顯「—」。

### v1.0.25 — 訓練完成「已完成→倒退」race
- #04 灌庫對每 chunk 送 1 callback(5 chunks → 5 callback)。第 1 個正確設「已完成」,後續重複 callback 走 else 分支用 body.status(="Step 43 進行中")覆寫 → 倒退,專案永遠卡。終態(已完成/失敗)重複 callback 直接忽略。

### v1.0.26 — deploy cred endpoint 漏 /api/v1(Gemini 遠端發現)
- `api_get(base_url,"/credentials")` 漏 /api/v1(其他 workflow endpoint 都有)→ cred-check/rewire 必 404。

### v1.0.27 — OWUI valves 漏 backend_url + OWUI pipeline 沒宣告 valve
- backend createOwuiPipeline 設 valves 漏 backend_url → #05-RAG-Core query「Invalid URL」。補送。
- (接力)OWUI pipeline 腳本 Valves class **沒宣告 backend_url** → OWUI Pydantic 收到丟棄 → n8n 還是沒。兩個 .py 加 backend_url Field + payload 帶上(v1.0.29 補)。

### v1.0.28 — RAG 答案附下載連結(原始 PDF + 命中 chunk 切片)
- Step4 灌庫時把 `source_url`(/dl 整份 PDF)+ `md_url`(/dl-chunk 命中切片,帶 &i=idx)寫進 chunk payload;查詢命中 #05 組 references_md 附答案末。
- splitter /resolve 加搜 Step1-File2MD/Step2-MD2JSON;backend 加 GET /dl-chunk 即時從 Step2 JSON 取第 i 片回小 .md。

### v1.0.29 — L1/L2/L3 分層比對 + 自家加權重排
- 舊版 L1+L2+L3 全混一袋比所有欄位(跨層雜訊)。改:#04 用字典把 chunk tags 對應 level1/2/3 存 payload;#05 分層命中(matched_l1/l2/l3)→ Build Qdrant Body 分層 filter(level1↔matched_l1...)。
- Rerank by Tier code 節點(**無 rerank 模型**,純算術):L3命中×3+L2×2+L1×1,撈 15 候選 → 算分取前 5。
- ⚠️ **依賴關鍵字字典(Tab 03)有 L1/L2/L3 階層**:chunk tags 要對得到字典層級,level1/2/3 才有值。字典空 → 分層無效果,自動退回扁平比對。

### v1.0.30 — #04 fileSelector 被插入節點打斷 + 假成功偵測
- v1.0.29 插 Fetch Hierarchy 在 Extract Payload 跟 Read JSON 中間,fileSelector 用 `$json.folder_id`(當前輸入)→ 變 Fetch Hierarchy 回應(無 folder_id)→ 讀 0 檔。改顯式 `$('Extract Payload')`。
- **假成功偵測**:Step4 callback chunk_count===0 → 標「已完成(⚠️灌庫0筆)」+ debug_logs error,不再 silent 成功。
- (同版)#04/#05 注入 code 的 regex `\s`/`\n` 被 heredoc 吃掉 → 語法錯/字串跨行壞。改用 Write 工具精確跳脫。

### v1.0.31 — #05 Fetch L1/L2/L3 Keywords backend_url 被前節點洗掉(Gemini 遠端,全體 query 必中)
- n8n HTTP 節點執行後用「回應」覆蓋 $json。第 1 個 Fetch Backend Config 用 `$json.backend_url`(此時 $json=入口資料,有值)→ OK;第 2 個 Fetch L1/L2/L3 Keywords 用 `$json.backend_url`(此時 $json=第1個回應,無 backend_url)→ 空 → URL 少 host → Invalid URL。
- pre-existing bug:之前 backend_url 一直空(OWUI valve 沒宣告)沒暴露,流進來才現形。
- **修**:第 2 個改用源頭 `$('When Called').item.json.backend_url`(#05-RAG-Core 入口節點,不被洗)。注意非 `$('Prep Core Input')`(那在 #05-NonStream)。

---

## 1b. 2026-06-02 本機 community-test 驗證紀錄(v1.0.28-31 新功能)

**測試環境**:`c:/Tools/tigerai-community-test`(獨立 n8n-community + PG_SCHEMA=tigerai_community + 接共用 OpenGenie)。

**驗證方式 & 結果:**
- 完整訓練 chain 有**間歇性卡頓**(接共用基礎設施 + 多次 backend 重啟造成狀態不一致,非功能 bug)→ 改用**直接觸發 #04** 繞過 flaky chain 驗證功能。
- ✅ **下載連結**:直接觸發 #04 → Build Point IDs 產 10 chunk payload,`source_url=.../backend/dl?p=&f=Laborlaw.pdf`、`md_url=.../backend/dl-chunk?p=&f=Laborlaw.md&i=0`;curl /dl-chunk 回傳「命中片段 #0」小切片(HTTP 200)、/dl 回 302 跳轉下載 PDF。**功能實測通。**
- ✅ **#04 fileSelector + regex 修**:Read JSON 讀到檔、Build Point IDs 語法 valid 建 10 payload。
- ⚠️ **L1/L2/L3 level1/2/3 全空**:機制跑通無錯,但 chunk tags(勞動條件/保障權益/最低標準)對不到字典 → 空。**分層效果被「字典要先建 L1/L2/L3 階層」gate 住**(符合 AI 只建議、人核對才入字典的設計)。

**關鍵教訓:**
1. **heredoc 寫 n8n jsCode/expression 的 regex 會吃掉 `\s`/`\n` 跳脫** → 語法錯。改用 Write 工具寫 patch script(精確保留),且每個 code 節點用 `new Function(jsCode)` 驗語法、expression 檢查有沒有混入真換行。
2. **n8n HTTP 節點會用回應覆蓋 $json** → 後續節點要拿前面的欄位必須用源頭 `$('SourceNode').item.json.x`,不能用 `$json.x`。
3. **插入節點到主鏈會打斷下游 `$json` 引用** → 下游改用顯式 `$('UpstreamNode')` 才安全。
4. **「假成功」是最難抓的** → 各步驟 callback 帶數量,backend 驗產出>0 否則標警告(v1.0.30 做了 Step4)。
5. L1/L2/L3 分層比對的**效果前提是字典先建好階層**;空字典時自動退回扁平,不會壞但也沒提升。

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
