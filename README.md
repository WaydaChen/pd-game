# 囚犯困境 課堂賽局平台

## 課堂實驗中心（入口）

啟動後開 `http://localhost:8000/hub`，選擇模組。各遊戲教師端入口：

| 遊戲 | 教師端 | 學生端 |
|------|--------|--------|
| 囚犯困境 | `/teacher` | `/` |
| 銀行擠兌 | `/teacher-bank` | `/bank` |
| 最後通牒 | `/teacher-ultimatum` | `/ultimatum` |
| 信任遊戲 | `/teacher-trust` | `/trust` |
| 全域博弈 | `/teacher-globalgame` | `/globalgame` |
| ZIP-Code 期貨交易 | `/teacher-zip` | `/zip` |

## ZIP-Code 期貨交易遊戲

多人即時期貨市場（WebSocket）。教師於 `/teacher-zip` 建立房間，取得 6 碼房間代碼與 4 碼主持碼；
學生於 `/zip` 輸入房間代碼與姓名/學號進場。五輪逐位揭曉交割價（＝五個祕密數字之和），第五輪後結算。

- 兩種撮合模式：**電子撮合**（匿名、嚴格價格優先）與**喊價模式**（部分可見、顯示對手代號），僅大廳階段可切換。
- 倒數歸零由伺服器自動收盤；教師重新整理後可用主持碼接手。
- 事件同步寫入 `data/{房間代碼}.jsonl`：建房、進場、每一筆報價與成交、揭露、結算全部有紀錄。
- **房間可從日誌重建**：房間不在記憶體裡時（伺服器重啟、容器被換掉、當掉重來），只要日誌還在，
  學生連線或教師匯出都會自動把房間接回來 —— 主持碼、部位、成交、已揭露的數字都一致。
  中途斷掉的那一輪會以「開著、沒有倒數」的狀態回來，由教師手動收盤。
  只有還沒揭露的祕密數字會重抽，那些數字從未離開伺服器，所以不影響任何人已經做過的判斷。
- 教師端可匯出 `trades / orders / summary / efficiency` 四種 CSV（UTF-8 with BOM，Excel 相容），
  以及**原始事件日誌 `.jsonl`**。
- 後台端點 `/admin/rooms`、`/admin/export` 以環境變數 `ADMIN_TOKEN` 保護（未設定則停用）。

### 上課前務必確認

1. **掛載持久化儲存。** 雲端容器的檔案系統是暫時的，**沒有掛 volume 的話，日誌會隨著每次重新部署
   或重啟一起消失**，上面的「可從日誌重建」就形同虛設。Railway 的作法是加一個 volume
   掛到 `/data`，再設環境變數 `ZIP_DATA_DIR=/data`（程式讀這個變數，不必改程式碼）；
   本機 docker-compose 已經設好 `zip-data` volume。
2. **上課中不要推版。** 推版會換掉容器，進行中的房間會整個消失（有 volume 的話撿得回來，
   但學生要重新連線）。
3. **每輪下課按一次「raw log (.jsonl)」。** 最便宜的保險：就算 volume 沒掛好、伺服器出事，
   手上那份檔案就能重建整場課的檢討資料。
4. **主持碼記下來。** 有它才進得回房間（伺服器重啟過也一樣，房間會從日誌接回來）。
   教師頁會把房號與主持碼存在瀏覽器裡、重新整理後自動接回原本的房間，
   但換一台電腦或清掉瀏覽器資料就只能靠手抄的那組碼。
   另外，按「Create room」是開一個**全新的空房間**，學生不會跟過來 —— 頁面會先跳出提醒。

### 百人班的效能注意事項

廣播成本是這個遊戲唯一會爆掉的地方，設計上已經做了三件事，改動前請先理解：

- `broadcast()` 每場只算一次 `_positions` 與 `_ledger`，再把同角色的 payload 共用。
  **不要把 `_ledger()` 放回 `build_state()` 裡面** —— 那是最貴的一項，100 人時一次廣播
  會從 12ms 變成 220ms，事件迴圈被塞滿之後，每秒一次的背景計時器就排不進去，
  結果是整場凍住、倒數停住（第四輪之後成交累積夠多就會發生）。
- 交易事件走 `mark_dirty()` 合併，最多每 `FLUSH_SECONDS`（0.1 秒）推一次，而不是每筆事件都全量廣播。
- 送出用 `asyncio.gather`，單一連線逾時（`SEND_TIMEOUT`，5 秒）就移除，
  免得有人手機睡著、TCP 半開時拖住整場。

## 專案結構


```
pd-game/
├── main.py              # FastAPI 後端
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── static/
    └── index.html       # 前端（自動由後端 serve）
```

## 本地測試

```bash
# 安裝依賴
pip install -r requirements.txt

# 設定 API Key
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxx

# 啟動伺服器
uvicorn main:app --reload

# 瀏覽器開啟
open http://localhost:8000
```

## Docker 本地測試

```bash
# 複製並填入 API Key
echo "ANTHROPIC_API_KEY=sk-ant-xxxxxxxx" > .env

# 啟動
docker compose up --build

# 瀏覽器開啟
open http://localhost:8000
```

---

## 部署到雲端

### 方案 A — Railway（最簡單，免費額度）

1. 前往 https://railway.app 並登入
2. New Project → Deploy from GitHub（先把專案推上 GitHub）
3. 在 Variables 頁面加入：
   ```
   ANTHROPIC_API_KEY = sk-ant-xxxxxxxx
   ZIP_DATA_DIR      = /data
   ```
4. **加一個 Volume**（服務頁 → Settings → Volumes → New Volume），Mount path 填 `/data`。
   沒有這一步的話，ZIP 遊戲的事件日誌會隨著每次重新部署一起消失，當掉之後就撿不回來了。
5. Railway 會自動偵測 Dockerfile 並部署，幾分鐘後給你一個公開網址

### 方案 B — Render（免費，稍慢）

1. 前往 https://render.com
2. New → Web Service → Connect GitHub repo
3. Runtime 選 Docker
4. Environment Variables 加入 `ANTHROPIC_API_KEY`
5. Deploy

### 方案 C — Google Cloud Run（按用量計費）

```bash
# 安裝 gcloud CLI 並登入後執行：

PROJECT_ID=your-project-id
IMAGE=gcr.io/$PROJECT_ID/pd-game

docker build -t $IMAGE .
docker push $IMAGE

gcloud run deploy pd-game \
  --image $IMAGE \
  --platform managed \
  --region asia-east1 \
  --allow-unauthenticated \
  --set-env-vars ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
```

### 方案 D — Fly.io

```bash
# 安裝 flyctl 後：
fly launch          # 依提示設定
fly secrets set ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
fly deploy
```

---

## API 端點

| Method | Path | 說明 |
|--------|------|------|
| GET | `/` | 前端頁面 |
| POST | `/api/ai-choice` | AI 決定本回合選擇 |
| POST | `/api/ai-analysis` | AI 分析本回合策略 |
| GET | `/health` | 健康檢查 |
