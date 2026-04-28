# 囚犯困境 課堂賽局平台

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
