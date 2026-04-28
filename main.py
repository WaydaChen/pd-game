from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import random

app = FastAPI(title="囚犯困境 API")

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

# ── AI opponent decision (Tit-for-Tat + 隨機變化) ─────────────────────
@app.post("/api/ai-choice")
def ai_choice(req: AIChoiceRequest):
    is_last = bool(req.total_rounds and req.round >= req.total_rounds)

    if not req.history:
        # 第一回合：先合作
        choice = "cooperate"
    else:
        last_my = req.history[-1]["myChoice"]
        # 終局傾向背叛；否則 Tit-for-Tat + 15% 隨機翻轉
        if is_last:
            choice = "betray" if random.random() < 0.7 else last_my
        else:
            base = last_my  # 模仿玩家上一回合
            if random.random() < 0.15:
                choice = "betray" if base == "cooperate" else "cooperate"
            else:
                choice = base
    return {"choice": choice}

# ── 規則式回合分析 ────────────────────────────────────────────────────
def _rule_based_analysis(req: AIAnalysisRequest) -> str:
    my, opp = req.my_choice, req.opp_choice

    if my == "cooperate" and opp == "cooperate":
        base = "雙方合作達成柏拉圖最適，互信累積長期收益最高。"
    elif my == "betray" and opp == "betray":
        base = "雙方背叛落入納許均衡，集體得分最差，典型困境。"
    elif my == "cooperate" and opp == "betray":
        base = "你被剝削，對手獲短期最大利得，下一輪可考慮報復以建立威懾。"
    else:  # my betray, opp cooperate
        base = "你成功剝削對手，但長期恐引發對方報復破壞合作。"

    if req.is_single_round:
        tail = "單回合無未來互動，理性策略偏向背叛。"
    elif req.know_rounds and req.total_rounds and req.round >= req.total_rounds:
        tail = "終局逆向歸納下，合作誘因消失。"
    elif not req.know_rounds:
        tail = "回合數未知有助維持合作（無限賽局效應）。"
    else:
        tail = f"剩餘回合仍可建立互信。"

    anon = "匿名降低聲譽成本，背叛誘因升高。" if not req.know_opponent else "記名增加聲譽壓力，較易維持合作。"

    text = base + tail + anon
    # 截 45 字
    return text[:45]

# ── AI round analysis ─────────────────────────────────────────────────
@app.post("/api/ai-analysis")
def ai_analysis(req: AIAnalysisRequest):
    return {"analysis": _rule_based_analysis(req)}

@app.get("/health")
def health():
    return {"status": "ok"}
