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

@app.get("/teacher-bank")
def teacher_bank_page():
    return FileResponse("static/teacher-bank.html")

@app.get("/bank")
def bank_page():
    return FileResponse("static/bank.html")

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
            "type": "pd",
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
        "type": room.get("type", "pd"),
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


# ═══════════════════════════════════════════════════════════════════════
# 銀行擠兌（Bank Run / Diamond-Dybvig）
# ═══════════════════════════════════════════════════════════════════════

class TreatmentConfig(BaseModel):
    rounds: int = 3
    pairing: str = "fixed"           # 'fixed' | 'random_each_round'
    threshold: int = 4               # 提款人數 ≥ 此值即破產
    hold_reward_pct: float = 0.5     # 持有報酬：deposit * (1 + pct)
    bankruptcy_compensation: float = 0  # 破產時非提款者拿到的錢
    forced_withdraw_prob: float = 0.0   # 強制提款機率
    show_live_count: bool = True

class BankSettings(BaseModel):
    title: str = "銀行擠兌實驗"
    bank_name: str = "課堂銀行"
    deposit: float = 10
    group_size: int = 6
    t1: TreatmentConfig = TreatmentConfig(bankruptcy_compensation=0)
    t2: TreatmentConfig = TreatmentConfig(bankruptcy_compensation=9)

class BankChoice(BaseModel):
    choice: str   # 'hold' | 'withdraw'

# ── Teacher: create bank room ─────────────────────────────────────────
@app.post("/api/teacher/bank-rooms")
def create_bank_room(settings: BankSettings):
    with LOCK:
        code = _gen_code()
        token = secrets.token_urlsafe(16)
        ROOMS[code] = {
            "code": code,
            "type": "bank",
            "token": token,
            "settings": settings.model_dump(),
            "created_at": time.time(),
            "phase": "lobby",            # 'lobby' | 't1' | 'transition' | 't2' | 'ended'
            "players": {},               # pid -> {sid, name, group_id, joined_at}
            "groups": {},                # gid -> {id, treatment, players: [pid], rounds: [...], current_round_idx}
            "history": [],               # 全部對局結果（用於統計）
        }
    return {"code": code, "teacher_token": token}

def _bank_assign_groups(room, treatment_key):
    """把 waiting 玩家分組（每組 group_size 人）"""
    settings = room["settings"]
    size = max(2, int(settings["group_size"]))
    pids = [pid for pid in room["players"]]
    random.shuffle(pids)
    groups = {}
    gi = 0
    for i in range(0, len(pids), size):
        chunk = pids[i:i+size]
        if len(chunk) < 2:
            # 不足兩人 → 併入上一組
            if groups:
                last_key = list(groups.keys())[-1]
                groups[last_key]["players"].extend(chunk)
                for p in chunk:
                    room["players"][p]["group_id"] = last_key
            continue
        gi += 1
        gid = f"G{gi}"
        groups[gid] = {
            "id": gid,
            "treatment": treatment_key,
            "players": chunk,
            "rounds": [],
            "current_round_idx": 0,
        }
        for p in chunk:
            room["players"][p]["group_id"] = gid
    return groups

def _bank_start_round(room, group):
    """為 group 開始一個新回合：重新隨機（如需要），決定強制提款者"""
    settings = room["settings"]
    tcfg = settings[group["treatment"]]
    if tcfg["pairing"] == "random_each_round" and group["rounds"]:
        # 重新洗牌：跨組重組需在 room 層級處理；此處保持小組內成員不動
        random.shuffle(group["players"])
    forced = {}
    for pid in group["players"]:
        forced[pid] = random.random() < float(tcfg["forced_withdraw_prob"])
    rnd = {
        "round_num": len(group["rounds"]) + 1,
        "decisions": {pid: {"choice": None, "forced": forced[pid], "submitted_at": None} for pid in group["players"]},
        "resolved": False,
        "result": None,
        "started_at": time.time(),
    }
    group["rounds"].append(rnd)

def _bank_resolve_round(room, group, rnd):
    """結算回合"""
    settings = room["settings"]
    tcfg = settings[group["treatment"]]
    deposit = float(settings["deposit"])
    threshold = int(tcfg["threshold"])
    reward_pct = float(tcfg["hold_reward_pct"])
    comp = float(tcfg["bankruptcy_compensation"])

    # 強制提款者一律 withdraw
    for pid, d in rnd["decisions"].items():
        if d["forced"]:
            d["choice"] = "withdraw"
        elif d["choice"] is None:
            d["choice"] = "hold"   # 超時預設持有

    withdrawers = [pid for pid, d in rnd["decisions"].items() if d["choice"] == "withdraw"]
    holders = [pid for pid, d in rnd["decisions"].items() if d["choice"] == "hold"]
    n_w = len(withdrawers)
    n_total = len(rnd["decisions"])
    bankrupt = n_w >= threshold

    payouts = {}
    if bankrupt:
        # 銀行儲備不足，僅能支付 (threshold - 1) 個提款者，隨機抽籤
        capacity = max(0, threshold - 1)
        winners = set(random.sample(withdrawers, min(capacity, n_w)))
        for pid in withdrawers:
            payouts[pid] = deposit if pid in winners else comp
        for pid in holders:
            payouts[pid] = comp
    else:
        for pid in withdrawers:
            payouts[pid] = deposit
        for pid in holders:
            payouts[pid] = round(deposit * (1 + reward_pct), 2)

    rnd["resolved"] = True
    rnd["resolved_at"] = time.time()
    rnd["result"] = {
        "bankrupt": bankrupt,
        "withdraw_count": n_w,
        "total": n_total,
        "threshold": threshold,
        "payouts": payouts,
    }
    # 寫入 history
    for pid, d in rnd["decisions"].items():
        room["history"].append({
            "treatment": group["treatment"],
            "group_id": group["id"],
            "round": rnd["round_num"],
            "pid": pid,
            "sid": room["players"][pid]["sid"],
            "name": room["players"][pid]["name"],
            "choice": d["choice"],
            "forced": d["forced"],
            "bankrupt": bankrupt,
            "withdraw_count": n_w,
            "payout": payouts[pid],
            "ts": time.time(),
        })

# ── Teacher: start treatment ─────────────────────────────────────────
@app.post("/api/teacher/bank-rooms/{code}/start")
def bank_start(code: str, token: str, treatment: str = "t1"):
    room = ROOMS.get(code)
    if not room or room.get("type") != "bank":
        raise HTTPException(status_code=404, detail="找不到房間")
    if token != room["token"]:
        raise HTTPException(status_code=403, detail="授權失敗")
    if treatment not in ("t1", "t2"):
        raise HTTPException(status_code=400, detail="treatment 必須是 t1 或 t2")

    with LOCK:
        # 重組 groups（每次 start 重新分配）
        room["groups"] = _bank_assign_groups(room, treatment)
        for g in room["groups"].values():
            _bank_start_round(room, g)
        room["phase"] = treatment
    return {"ok": True, "groups": len(room["groups"]), "phase": room["phase"]}

# ── Teacher: transition to T2 ────────────────────────────────────────
@app.post("/api/teacher/bank-rooms/{code}/next-treatment")
def bank_next_treatment(code: str, token: str):
    room = ROOMS.get(code)
    if not room or room.get("type") != "bank":
        raise HTTPException(status_code=404, detail="找不到房間")
    if token != room["token"]:
        raise HTTPException(status_code=403, detail="授權失敗")
    with LOCK:
        room["phase"] = "transition"
    return {"ok": True, "phase": "transition"}

# ── Teacher: end ─────────────────────────────────────────────────────
@app.post("/api/teacher/bank-rooms/{code}/end")
def bank_end(code: str, token: str):
    room = ROOMS.get(code)
    if not room or room.get("type") != "bank":
        raise HTTPException(status_code=404, detail="找不到房間")
    if token != room["token"]:
        raise HTTPException(status_code=403, detail="授權失敗")
    with LOCK:
        room["phase"] = "ended"
    return {"ok": True}

# ── Student: bank state ──────────────────────────────────────────────
@app.get("/api/bank/rooms/{code}/players/{pid}")
def bank_player_state(code: str, pid: str):
    room = ROOMS.get(code)
    if not room or room.get("type") != "bank":
        raise HTTPException(status_code=404, detail="找不到房間")
    player = room["players"].get(pid)
    if not player:
        raise HTTPException(status_code=404, detail="玩家未註冊")

    settings = room["settings"]
    base = {
        "phase": room["phase"],
        "settings": settings,
        "type": "bank",
    }
    gid = player.get("group_id")
    group = room["groups"].get(gid) if gid else None

    if room["phase"] == "lobby" or not group:
        # 大廳階段 / 還沒被分組
        base["status"] = "waiting"
        # 累計收益：用 history
        base["total_earnings"] = round(sum(h["payout"] for h in room["history"] if h["pid"] == pid), 2)
        return base

    if room["phase"] == "transition":
        base["status"] = "transition"
        base["t1_summary"] = _player_treatment_summary(room, pid, "t1")
        base["total_earnings"] = round(sum(h["payout"] for h in room["history"] if h["pid"] == pid), 2)
        return base

    if room["phase"] == "ended":
        base["status"] = "ended"
        base["t1_summary"] = _player_treatment_summary(room, pid, "t1")
        base["t2_summary"] = _player_treatment_summary(room, pid, "t2")
        base["total_earnings"] = round(sum(h["payout"] for h in room["history"] if h["pid"] == pid), 2)
        return base

    # 進行中：t1 或 t2
    treatment = room["phase"]
    tcfg = settings[treatment]
    if not group["rounds"]:
        return base
    rnd = group["rounds"][-1]
    decision = rnd["decisions"].get(pid, {})

    # 即時提款計數
    submitted_choices = [d["choice"] for d in rnd["decisions"].values() if d.get("submitted_at")]
    live_withdraw = sum(1 for c in submitted_choices if c == "withdraw")
    submitted_count = len(submitted_choices)

    # 我的累計收益
    total_earnings = round(sum(h["payout"] for h in room["history"] if h["pid"] == pid), 2)

    state = {
        **base,
        "status": "playing",
        "treatment": treatment,
        "treatment_config": tcfg,
        "group_id": gid,
        "group_size": len(group["players"]),
        "round_num": rnd["round_num"],
        "total_rounds": tcfg["rounds"],
        "forced": decision.get("forced", False),
        "my_choice": decision.get("choice"),
        "submitted": bool(decision.get("submitted_at")),
        "round_resolved": rnd["resolved"],
        "submitted_count": submitted_count,
        "live_withdraw_count": live_withdraw if tcfg["show_live_count"] else None,
        "result": rnd["result"] if rnd["resolved"] else None,
        "total_earnings": total_earnings,
        "history": [
            {"round": r["round_num"], "treatment": treatment, "result": r["result"],
             "my_choice": r["decisions"].get(pid, {}).get("choice"),
             "my_forced": r["decisions"].get(pid, {}).get("forced", False),
             "my_payout": (r["result"]["payouts"].get(pid) if r["resolved"] and r["result"] else None)}
            for r in group["rounds"] if r["resolved"]
        ],
    }
    return state

def _player_treatment_summary(room, pid, treatment):
    rows = [h for h in room["history"] if h["pid"] == pid and h["treatment"] == treatment]
    total = round(sum(h["payout"] for h in rows), 2)
    n_with = sum(1 for h in rows if h["choice"] == "withdraw")
    n_forced = sum(1 for h in rows if h["forced"])
    bankrupts = sum(1 for h in rows if h["bankrupt"])
    return {
        "rounds": len(rows),
        "total_earnings": total,
        "withdraw_count": n_with,
        "forced_count": n_forced,
        "bankrupt_rounds": bankrupts,
        "history": rows,
    }

# ── Student: submit bank choice ──────────────────────────────────────
@app.post("/api/bank/rooms/{code}/players/{pid}/choice")
def bank_submit(code: str, pid: str, req: BankChoice):
    if req.choice not in ("hold", "withdraw"):
        raise HTTPException(status_code=400, detail="choice 必須是 hold 或 withdraw")
    room = ROOMS.get(code)
    if not room or room.get("type") != "bank":
        raise HTTPException(status_code=404, detail="找不到房間")
    with LOCK:
        player = room["players"].get(pid)
        if not player:
            raise HTTPException(status_code=404, detail="玩家未註冊")
        gid = player.get("group_id")
        group = room["groups"].get(gid) if gid else None
        if not group or not group["rounds"]:
            raise HTTPException(status_code=400, detail="目前無進行中的回合")
        rnd = group["rounds"][-1]
        if rnd["resolved"]:
            return {"ok": True, "round_resolved": True}
        d = rnd["decisions"].get(pid)
        if not d:
            raise HTTPException(status_code=400, detail="不在本組")
        # 強制提款者：忽略選擇，固定 withdraw
        d["choice"] = "withdraw" if d["forced"] else req.choice
        d["submitted_at"] = time.time()

        # 全員都已提交 → 結算
        if all(x.get("submitted_at") for x in rnd["decisions"].values()):
            _bank_resolve_round(room, group, rnd)
    return {"ok": True}

# ── Student: advance to next round ──────────────────────────────────
@app.post("/api/bank/rooms/{code}/players/{pid}/ack")
def bank_ack(code: str, pid: str):
    """玩家看完結果按下「下一回合」→ 若全組都 ack，則開新回合或結束 treatment"""
    room = ROOMS.get(code)
    if not room or room.get("type") != "bank":
        raise HTTPException(status_code=404, detail="找不到房間")
    with LOCK:
        player = room["players"].get(pid)
        if not player:
            raise HTTPException(status_code=404, detail="玩家未註冊")
        gid = player.get("group_id")
        group = room["groups"].get(gid) if gid else None
        if not group or not group["rounds"]:
            return {"ok": True}
        rnd = group["rounds"][-1]
        if not rnd["resolved"]:
            return {"ok": True}
        rnd.setdefault("acks", set()).add(pid)
        if rnd["acks"] >= set(group["players"]):
            tcfg = room["settings"][group["treatment"]]
            if rnd["round_num"] >= int(tcfg["rounds"]):
                # treatment 結束（這組）
                pass
            else:
                _bank_start_round(room, group)
    return {"ok": True}

# ── Teacher: dashboard ──────────────────────────────────────────────
@app.get("/api/teacher/bank-rooms/{code}")
def bank_dashboard(code: str, token: str):
    room = ROOMS.get(code)
    if not room or room.get("type") != "bank":
        raise HTTPException(status_code=404, detail="找不到房間")
    if token != room["token"]:
        raise HTTPException(status_code=403, detail="授權失敗")

    history = room["history"]
    def _stats(treatment):
        rows = [h for h in history if h["treatment"] == treatment]
        if not rows:
            return {"rounds": 0, "withdraw_rate": 0, "bankrupt_rate": 0, "avg_payout": 0}
        n_with = sum(1 for h in rows if h["choice"] == "withdraw")
        bankrupt_rounds = set()
        all_rounds = set()
        for h in rows:
            key = (h["group_id"], h["round"])
            all_rounds.add(key)
            if h["bankrupt"]:
                bankrupt_rounds.add(key)
        return {
            "rounds": len(all_rounds),
            "withdraw_rate": round(n_with / len(rows), 3),
            "bankrupt_rate": round(len(bankrupt_rounds) / len(all_rounds), 3) if all_rounds else 0,
            "avg_payout": round(sum(h["payout"] for h in rows) / len(rows), 2),
        }

    players_view = []
    for pid, p in room["players"].items():
        rows = [h for h in history if h["pid"] == pid]
        players_view.append({
            "sid": p["sid"], "name": p["name"], "group_id": p.get("group_id"),
            "total_earnings": round(sum(h["payout"] for h in rows), 2),
            "rounds_played": len(rows),
            "t1_earnings": round(sum(h["payout"] for h in rows if h["treatment"] == "t1"), 2),
            "t2_earnings": round(sum(h["payout"] for h in rows if h["treatment"] == "t2"), 2),
            "t1_withdraws": sum(1 for h in rows if h["treatment"] == "t1" and h["choice"] == "withdraw"),
            "t2_withdraws": sum(1 for h in rows if h["treatment"] == "t2" and h["choice"] == "withdraw"),
        })

    groups_view = []
    for gid, g in room["groups"].items():
        groups_view.append({
            "id": gid, "treatment": g["treatment"], "size": len(g["players"]),
            "current_round": (g["rounds"][-1]["round_num"] if g["rounds"] else 0),
            "rounds_resolved": sum(1 for r in g["rounds"] if r["resolved"]),
        })

    return {
        "code": code,
        "type": "bank",
        "settings": room["settings"],
        "phase": room["phase"],
        "created_at": room["created_at"],
        "player_count": len(room["players"]),
        "players": players_view,
        "groups": groups_view,
        "history": history,
        "stats": {"t1": _stats("t1"), "t2": _stats("t2")},
    }

