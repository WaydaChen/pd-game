#!/bin/bash
# 囚犯困境 — 一鍵推上 GitHub
# 使用方法：bash push-to-github.sh

echo ""
echo "=== 囚犯困境 GitHub 上傳工具 ==="
echo ""

# 詢問 GitHub 網址
read -p "請貼上你的 GitHub repo 網址（例：https://github.com/你的帳號/pd-game.git）：" REPO_URL

if [ -z "$REPO_URL" ]; then
  echo "❌ 沒有輸入網址，結束。"
  exit 1
fi

echo ""
echo "➡️  初始化 Git..."
git init

echo "➡️  加入所有檔案..."
git add .

echo "➡️  建立第一個 commit..."
git commit -m "初始版本：囚犯困境課堂賽局平台"

echo "➡️  設定主分支為 main..."
git branch -M main

echo "➡️  連接到 GitHub..."
git remote add origin "$REPO_URL" 2>/dev/null || git remote set-url origin "$REPO_URL"

echo ""
echo "➡️  推上 GitHub（系統會要求輸入帳號和 Token）..."
echo "   ⚠️  密碼請輸入你的 Personal Access Token，不是登入密碼"
echo ""
git push -u origin main

echo ""
if [ $? -eq 0 ]; then
  echo "✅ 成功！你的程式碼已經在 GitHub 上了。"
  echo ""
  echo "接下來去 railway.app 部署："
  echo "  1. 用 GitHub 帳號登入 Railway"
  echo "  2. New Project → Deploy from GitHub → 選 pd-game"
  echo "  3. Variables 加入 ANTHROPIC_API_KEY"
  echo "  4. 等幾分鐘就有網址了 🎉"
else
  echo "❌ 推送失敗，請確認："
  echo "  - GitHub repo 網址是否正確"
  echo "  - Token 是否有 repo 權限"
fi
