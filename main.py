from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import random
import string
import secrets
import time
import threading

app = FastAPI(title="囚犯困境 API")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Pages ────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/teacher")
def teacher_page():
    return FileResponse("static/teacher.html")

@app.get("/health")
def health():
    return {"status": "ok"}

# ── Schemas ──────────────────────────────────────────────────────────
class AIChoiceRequest(BaseModel):
    my_choice: str
    history: list[dict]
    round: int
    total_rounds: Optional[int] = None
    know_opponent: bool = True

class AIAnalysisRequest(BaseModel):
    my_choice: str
    opp_choice: str
    my_pts: int
    opp_pts: int
    round: int
    total_rounds: Optional[int] = None
    is_single_round: bool = False
    know_rounds: bool = True
    know_opponent: bool = True

class RoomSettings(BaseModel):
    rounds_type: str = "multi"      # 'single' | 'multi'
    round_count: int = 5
    know_rounds: bool = True
    know_opponent: bool = True
    title: str = "囚犯困境課堂實驗"

class RoundEntry(BaseModel):
    round: int
    my_choice: str
    opp_choice: str
    my_pts: int
    opp_pts: int

class ResultSubmission(BaseModel):
    sid: str
    name: str
    my_score: int
    opp_score: int
    history: list[RoundEntry]

# ── Room store (in-memory) ───────────────────────────────────────────
ROOMS: dict = {}
LOCK = threading.Lock()

def _gen_code() -> str:
    while True:
        code = "".join(random.choices(string.digits, k=4))
        if code not in ROOMS:
            return code

# ── AI opponent decision (Tit-for-Tat + 隨機變化) ─────────────────────
@app.post("/api/ai-choice")
def ai_choice(req: AIChoiceRequest):
    is_last = bool(req.total_rounds and req.round >= req.total_rounds)
    if not req.history:
        choice = "cooperate"
    else:
        last_my = req.history[-1].get("myChoice", "cooperate")
        if is_last:
            choice = "betray" if random.random() < 0.7 else last_my
        else:
            base = last_my
            if random.random() < 0.15:
                choice = "betray" if base == "cooperate" else "cooperate"
            else:
                choice = base
    return {"choice": choice}

def _rule_based_analysis(req: AIAnalysisRequest) -> str:
    my, opp = req.my_choice, req.opp_choice
    if my == "cooperate" and opp == "cooperate":
        base = "雙方合作達成柏拉圖最適。"
    elif my == "betray" and opp == "betray":
        base = "雙方背叛落入納許均衡，集體得分最差。"
    elif my == "cooperate" and opp == "betray":
        base = "你被剝削，下輪可考慮報復建立威懾。"
    else:
        base = "你成功剝削對手，但長期恐引發報復。"

    if req.is_single_round:
        tail = "單回合理性策略偏向背叛。"
    elif req.know_rounds and req.total_rounds and req.round >= req.total_rounds:
        tail = "終局逆向歸納合作誘因消失。"
    elif not req.know_rounds:
        tail = "回合未知有助維持合作。"
    else:
        tail = "剩餘回合仍可建互信。"

    anon = "匿名背叛誘因升高。" if not req.know_opponent else "記名利於合作。"
    return (base + tail + anon)[:60]

@app.post("/api/ai-analysis")
def ai_analysis(req: AIAnalysisRequest):
    return {"analysis": _rule_based_analysis(req)}

# ── Teacher: create room ─────────────────────────────────────────────
@app.post("/api/teacher/rooms")
def create_room(settings: RoomSettings):
    with LOCK:
        code = _gen_code()
        token = secrets.token_urlsafe(16)
        ROOMS[code] = {
            "code": code,
            "token": token,
            "settings": settings.model_dump(),
            "created_at": time.time(),
            "submissions": [],  # list of dicts
        }
    return {"code": code, "teacher_token": token}

# ── Student: get room settings ───────────────────────────────────────
@app.get("/api/rooms/{code}")
def get_room(code: str):
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")
    return {"code": code, "settings": room["settings"]}

# ── Student: submit results ──────────────────────────────────────────
@app.post("/api/rooms/{code}/results")
def submit_result(code: str, sub: ResultSubmission):
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")
    with LOCK:
        room["submissions"].append({
            "sid": sub.sid,
            "name": sub.name,
            "my_score": sub.my_score,
            "opp_score": sub.opp_score,
            "history": [h.model_dump() for h in sub.history],
            "submitted_at": time.time(),
        })
    return {"ok": True, "count": len(room["submissions"])}

# ── Teacher: dashboard data ──────────────────────────────────────────
@app.get("/api/teacher/rooms/{code}")
def get_room_data(code: str, token: str):
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")
    if token != room["token"]:
        raise HTTPException(status_code=403, detail="授權失敗")

    subs = room["submissions"]
    total = len(subs)
    if total:
        avg_my = sum(s["my_score"] for s in subs) / total
        avg_opp = sum(s["opp_score"] for s in subs) / total
        all_rounds = [r for s in subs for r in s["history"]]
        coop_count = sum(1 for r in all_rounds if r["my_choice"] == "cooperate")
        coop_rate = coop_count / len(all_rounds) if all_rounds else 0
    else:
        avg_my = avg_opp = coop_rate = 0

    return {
        "code": code,
        "settings": room["settings"],
        "created_at": room["created_at"],
        "submissions": subs,
        "stats": {
            "count": total,
            "avg_my_score": round(avg_my, 2),
            "avg_opp_score": round(avg_opp, 2),
            "cooperation_rate": round(coop_rate, 3),
        },
    }
