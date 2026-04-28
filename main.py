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

PAYOFF = {
    ("cooperate", "cooperate"): (3, 3),
    ("cooperate", "betray"):    (0, 5),
    ("betray",    "cooperate"): (5, 0),
    ("betray",    "betray"):    (1, 1),
}

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
    mode: str = "ai"                # 'ai' | 'human'

class JoinRequest(BaseModel):
    sid: str
    name: str

class ChoiceRequest(BaseModel):
    choice: str   # 'cooperate' | 'betray'

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

def _ai_choose(my_history: list, last_my_choice: Optional[str], is_last: bool) -> str:
    """AI 對手策略：第一回合合作；之後 Tit-for-Tat + 15% 隨機翻轉；終局偏背叛"""
    if last_my_choice is None:
        return "cooperate"
    if is_last:
        return "betray" if random.random() < 0.7 else last_my_choice
    if random.random() < 0.15:
        return "betray" if last_my_choice == "cooperate" else "cooperate"
    return last_my_choice

# ── AI opponent decision (legacy, used when mode=ai 在前端直接呼叫) ───
@app.post("/api/ai-choice")
def ai_choice(req: AIChoiceRequest):
    is_last = bool(req.total_rounds and req.round >= req.total_rounds)
    last = req.history[-1].get("myChoice") if req.history else None
    return {"choice": _ai_choose(req.history, last, is_last)}

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
        total_rounds = 1 if settings.rounds_type == "single" else settings.round_count
        ROOMS[code] = {
            "code": code,
            "token": token,
            "settings": settings.model_dump(),
            "total_rounds": total_rounds,
            "created_at": time.time(),
            "phase": "lobby",       # 'lobby' | 'playing' | 'ended'
            "players": {},          # pid -> {sid, name, status, match_id, joined_at}
            "matches": {},          # mid -> match dict
            "submissions": [],      # 結算後的學生成績
        }
    return {"code": code, "teacher_token": token}

# ── Student: get room settings ───────────────────────────────────────
@app.get("/api/rooms/{code}")
def get_room(code: str):
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")
    return {
        "code": code,
        "settings": room["settings"],
        "phase": room["phase"],
        "waiting_count": sum(1 for p in room["players"].values() if p["status"] == "waiting"),
    }

# ── Student: join room (human mode 才需要) ───────────────────────────
@app.post("/api/rooms/{code}/join")
def join_room(code: str, req: JoinRequest):
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")
    with LOCK:
        # 同一 sid 重新進入：覆蓋舊紀錄
        existing = next((pid for pid, p in room["players"].items() if p["sid"] == req.sid), None)
        if existing:
            pid = existing
            room["players"][pid].update({"name": req.name})
        else:
            pid = secrets.token_urlsafe(8)
            room["players"][pid] = {
                "sid": req.sid, "name": req.name,
                "status": "waiting", "match_id": None,
                "joined_at": time.time(),
            }
    return {"player_id": pid, "phase": room["phase"]}

# ── Student: poll state ──────────────────────────────────────────────
@app.get("/api/rooms/{code}/players/{pid}")
def player_state(code: str, pid: str):
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")
    player = room["players"].get(pid)
    if not player:
        raise HTTPException(status_code=404, detail="玩家未註冊")

    base = {
        "status": player["status"],
        "phase": room["phase"],
        "settings": room["settings"],
        "total_rounds": room["total_rounds"],
    }
    mid = player.get("match_id")
    if not mid:
        return base

    match = room["matches"].get(mid)
    if not match:
        return base

    # 找出我方/對方
    if match["p1_pid"] == pid:
        me_key, opp_key = "p1", "p2"
    else:
        me_key, opp_key = "p2", "p1"

    history = []
    my_score = 0
    opp_score = 0
    for r in match["rounds"]:
        if r["resolved"]:
            mc = r[f"{me_key}_choice"]
            oc = r[f"{opp_key}_choice"]
            my_pts, opp_pts = PAYOFF[(mc, oc)]
            my_score += my_pts; opp_score += opp_pts
            history.append({
                "round": r["round"], "my_choice": mc, "opp_choice": oc,
                "my_pts": my_pts, "opp_pts": opp_pts,
            })

    current_round_idx = len(history)  # 已完成 n 輪 → 正在第 n+1 輪
    current_round = match["rounds"][current_round_idx] if current_round_idx < len(match["rounds"]) else None

    my_submitted = bool(current_round and current_round[f"{me_key}_choice"])
    opp_submitted = bool(current_round and current_round[f"{opp_key}_choice"])

    base.update({
        "match_id": mid,
        "opponent_name": match[f"{opp_key}_name"],
        "opponent_is_ai": match[f"{opp_key}_is_ai"],
        "current_round": current_round_idx + 1 if current_round else None,
        "history": history,
        "my_score": my_score,
        "opp_score": opp_score,
        "my_submitted": my_submitted,
        "opp_submitted": opp_submitted,
        "finished": match["finished"],
    })
    return base

# ── Student: submit choice ──────────────────────────────────────────
@app.post("/api/rooms/{code}/players/{pid}/choice")
def submit_choice(code: str, pid: str, req: ChoiceRequest):
    if req.choice not in ("cooperate", "betray"):
        raise HTTPException(status_code=400, detail="choice 必須是 cooperate 或 betray")
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")

    with LOCK:
        player = room["players"].get(pid)
        if not player:
            raise HTTPException(status_code=404, detail="玩家未註冊")
        mid = player.get("match_id")
        match = room["matches"].get(mid) if mid else None
        if not match or match["finished"]:
            raise HTTPException(status_code=400, detail="目前無進行中的對局")

        me_key = "p1" if match["p1_pid"] == pid else "p2"
        opp_key = "p2" if me_key == "p1" else "p1"

        # 找當前未完成回合
        current = next((r for r in match["rounds"] if not r["resolved"]), None)
        if not current:
            raise HTTPException(status_code=400, detail="無進行中的回合")

        if current[f"{me_key}_choice"]:
            return {"ok": True, "already_submitted": True}

        current[f"{me_key}_choice"] = req.choice

        # 若對手是 AI，立刻決定 AI 出牌
        if match[f"{opp_key}_is_ai"]:
            ai_history = []
            for r in match["rounds"]:
                if r["resolved"]:
                    ai_history.append({"myChoice": r[f"{opp_key}_choice"]})
            last_ai = ai_history[-1]["myChoice"] if ai_history else None
            is_last = current["round"] >= room["total_rounds"]
            current[f"{opp_key}_choice"] = _ai_choose(ai_history, last_ai, is_last)

        # 雙方都出 → 結算
        if current[f"{me_key}_choice"] and current[f"{opp_key}_choice"]:
            current["resolved"] = True
            current["resolved_at"] = time.time()

            # 是否最後一回合
            if current["round"] >= room["total_rounds"]:
                _finalize_match(room, match)

    return {"ok": True}

def _finalize_match(room: dict, match: dict):
    """把對局結果寫入 submissions（兩位玩家都記錄）"""
    if match["finished"]:
        return
    match["finished"] = True

    p1_score = 0; p2_score = 0
    p1_history = []; p2_history = []
    for r in match["rounds"]:
        if not r["resolved"]:
            continue
        c1, c2 = r["p1_choice"], r["p2_choice"]
        a, b = PAYOFF[(c1, c2)]
        p1_score += a; p2_score += b
        p1_history.append({"round": r["round"], "my_choice": c1, "opp_choice": c2, "my_pts": a, "opp_pts": b})
        p2_history.append({"round": r["round"], "my_choice": c2, "opp_choice": c1, "my_pts": b, "opp_pts": a})

    if not match["p1_is_ai"]:
        room["submissions"].append({
            "sid": match["p1_sid"], "name": match["p1_name"],
            "my_score": p1_score, "opp_score": p2_score,
            "opponent_name": match["p2_name"],
            "history": p1_history, "submitted_at": time.time(),
        })
    if not match["p2_is_ai"]:
        room["submissions"].append({
            "sid": match["p2_sid"], "name": match["p2_name"],
            "my_score": p2_score, "opp_score": p1_score,
            "opponent_name": match["p1_name"],
            "history": p2_history, "submitted_at": time.time(),
        })

# ── Teacher: start pairing ───────────────────────────────────────────
@app.post("/api/teacher/rooms/{code}/pair")
def start_pairing(code: str, token: str):
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")
    if token != room["token"]:
        raise HTTPException(status_code=403, detail="授權失敗")

    with LOCK:
        waiting = [pid for pid, p in room["players"].items() if p["status"] == "waiting"]
        random.shuffle(waiting)
        total_rounds = room["total_rounds"]
        new_matches = 0

        # 兩兩配對
        i = 0
        while i + 1 < len(waiting):
            a_pid, b_pid = waiting[i], waiting[i+1]
            mid = secrets.token_urlsafe(6)
            a = room["players"][a_pid]; b = room["players"][b_pid]
            room["matches"][mid] = {
                "id": mid,
                "p1_pid": a_pid, "p1_sid": a["sid"], "p1_name": a["name"], "p1_is_ai": False,
                "p2_pid": b_pid, "p2_sid": b["sid"], "p2_name": b["name"], "p2_is_ai": False,
                "rounds": [{"round": r, "p1_choice": None, "p2_choice": None, "resolved": False} for r in range(1, total_rounds+1)],
                "finished": False,
                "started_at": time.time(),
            }
            a["status"] = b["status"] = "playing"
            a["match_id"] = b["match_id"] = mid
            new_matches += 1
            i += 2

        # 奇數人時最後一位配 AI
        if i < len(waiting):
            a_pid = waiting[i]
            a = room["players"][a_pid]
            mid = secrets.token_urlsafe(6)
            room["matches"][mid] = {
                "id": mid,
                "p1_pid": a_pid, "p1_sid": a["sid"], "p1_name": a["name"], "p1_is_ai": False,
                "p2_pid": None,  "p2_sid": "AI",     "p2_name": "AI 對手", "p2_is_ai": True,
                "rounds": [{"round": r, "p1_choice": None, "p2_choice": None, "resolved": False} for r in range(1, total_rounds+1)],
                "finished": False,
                "started_at": time.time(),
            }
            a["status"] = "playing"
            a["match_id"] = mid
            new_matches += 1

        room["phase"] = "playing"

    return {"ok": True, "matches_created": new_matches}

# ── Student: submit results (legacy, mode=ai 用) ─────────────────────
@app.post("/api/rooms/{code}/results")
def submit_result(code: str, sub: ResultSubmission):
    room = ROOMS.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="找不到房間")
    with LOCK:
        room["submissions"].append({
            "sid": sub.sid, "name": sub.name,
            "my_score": sub.my_score, "opp_score": sub.opp_score,
            "opponent_name": "AI 對手",
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

    waiting = [{"name": p["name"], "sid": p["sid"]} for p in room["players"].values() if p["status"] == "waiting"]
    playing = [{"name": p["name"], "sid": p["sid"]} for p in room["players"].values() if p["status"] == "playing"]

    return {
        "code": code,
        "settings": room["settings"],
        "phase": room["phase"],
        "total_rounds": room["total_rounds"],
        "created_at": room["created_at"],
        "submissions": subs,
        "lobby": {"waiting": waiting, "playing": playing},
        "stats": {
            "count": total,
            "avg_my_score": round(avg_my, 2),
            "avg_opp_score": round(avg_opp, 2),
            "cooperation_rate": round(coop_rate, 3),
        },
    }
