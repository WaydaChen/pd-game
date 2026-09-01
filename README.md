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
- 事件同步寫入 `data/{房間代碼}.jsonl`（容器重啟不遺失）。
- 教師端可匯出 `trades / orders / summary` 三種 CSV（UTF-8 with BOM，Excel 相容）。
- 後台端點 `/admin/rooms`、`/admin/export` 以環境變數 `ADMIN_TOKEN` 保護（未設定則停用）。

> 部署到 Railway 時請掛載 volume 到 `/app/data`（docker-compose 已設定 `zip-data` volume），並可選擇設定 `ADMIN_TOKEN`。

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
   ```
4. Railway 會自動偵測 Dockerfile 並部署，幾分鐘後給你一個公開網址

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
