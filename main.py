from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import anthropic
import os
import random

app = FastAPI(title="囚犯困境 API")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

# ── Schemas ──────────────────────────────────────────────────────────
class AIChoiceRequest(BaseModel):
    my_choice: str
    history: list[dict]
    round: int
    total_rounds: int | None = None
    know_opponent: bool = True

class AIAnalysisRequest(BaseModel):
    my_choice: str
    opp_choice: str
    my_pts: int
    opp_pts: int
    round: int
    total_rounds: int | None = None
    is_single_round: bool = False
    know_rounds: bool = True
    know_opponent: bool = True

# ── AI opponent decision ──────────────────────────────────────────────
@app.post("/api/ai-choice")
def ai_choice(req: AIChoiceRequest):
    history_text = "; ".join(
        f"回合{i+1}: 你{'合作' if h['myChoice']=='cooperate' else '背叛'}, 對手{'合作' if h['oppChoice']=='cooperate' else '背叛'}"
        for i, h in enumerate(req.history)
    ) or "無"

    if req.total_rounds:
        rounds_info = f"總共{req.total_rounds}回合，現在第{req.round}回合。"
        is_last = req.round >= req.total_rounds
    else:
        rounds_info = "回合數未知。"
        is_last = False

    opponent_info = "你知道對手是誰。" if req.know_opponent else "匿名對局，不知道對手是誰。"

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": (
                    f"你正在玩囚犯困境賽局，你是AI對手。{rounds_info}{opponent_info}"
                    f"歷史: {history_text}。"
                    f"玩家本回合選擇了: {'合作' if req.my_choice=='cooperate' else '背叛'}。"
                    f"{'這是最後一回合，請考慮終局策略。' if is_last else ''}"
                    f"請僅回覆 cooperate 或 betray，不要其他文字。"
                    f"你的策略是Tit-for-Tat加15%隨機變化，終局時傾向背叛。"
                )
            }]
        )
        text = message.content[0].text.strip().lower()
        choice = "cooperate" if "cooperate" in text else "betray"
    except Exception:
        if not req.history:
            choice = "cooperate"
        else:
            last = req.history[-1]["myChoice"]
            betray_prob = 0.5 if is_last else 0.15
            choice = "betray" if random.random() < betray_prob else last
    return {"choice": choice}

# ── AI round analysis ─────────────────────────────────────────────────
@app.post("/api/ai-analysis")
def ai_analysis(req: AIAnalysisRequest):
    context_parts = []
    if req.is_single_round:
        context_parts.append("這是單回合賽局（一次性互動）。")
    else:
        if req.know_rounds and req.total_rounds:
            context_parts.append(f"這是{req.total_rounds}回合賽局，現在第{req.round}回合，玩家知道總回合數。")
        else:
            context_parts.append(f"這是多回合賽局（玩家不知道總回合數），現在第{req.round}回合。")
    context_parts.append("雙方知道對方是誰。" if req.know_opponent else "這是匿名對局，雙方不知道對方身份。")

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    f"{''.join(context_parts)}"
                    f"玩家{'合作' if req.my_choice=='cooperate' else '背叛'}，"
                    f"對手{'合作' if req.opp_choice=='cooperate' else '背叛'}，"
                    f"玩家得{req.my_pts}分對手得{req.opp_pts}分。"
                    f"請用45字以內繁體中文，結合賽局理論與本局設定（匿名/記名、回合數已知/未知、單/多回合）給出分析，直接說重點。"
                )
            }]
        )
        text = message.content[0].text.strip()
    except Exception:
        text = "無法取得分析。"
    return {"analysis": text}

@app.get("/health")
def health():
    return {"status": "ok"}
