"""
Ultimatum Game & Trust Game backend routes.
Mounted on the same FastAPI app + ROOMS dict + LOCK from main.py.
"""
from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional
import random
import secrets
import string
import time


def _gen_code(rooms: dict) -> str:
    while True:
        c = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if c not in rooms:
            return c


# ─────────────────────────── Schemas ───────────────────────────
class UltSettings(BaseModel):
    title: str = "最後通牒實驗"
    pie: int = 100               # 待分配金額
    step: int = 5                # 提案步距
    show_distribution: bool = True  # 結束後是否公開分配統計
    anonymous_mode: bool = True


class TrustSettings(BaseModel):
    title: str = "信任遊戲"
    endowment: int = 10
    multiplier: float = 3.0
    show_distribution: bool = True
    anonymous_mode: bool = True


class JoinReq(BaseModel):
    sid: str
    name: str


class OfferReq(BaseModel):
    offer: int  # responder 拿多少


class DecisionReq(BaseModel):
    accept: bool


class SendReq(BaseModel):
    amount: int  # investor 送出


class ReturnReq(BaseModel):
    amount: int  # trustee 回送


# ─────────────────────────── 共用工具 ───────────────────────────
def _alias_pool():
    nicks = ["小柯","小芙","小翰","小娜","小皓","小琦","小恩","小宇","小棠","小謙",
            "小芸","小昕","小威","小綺","小語","小翔","小晴","小紘","小妍","小睿",
            "小亘","小蓁","小辰","小瑩","小謨","小函","小蕎","小璋","小頌","小逸"]
    avs = ["🦊","🦉","🐢","🐰","🐻","🦝","🐼","🐨","🦁","🐯","🐸","🐙","🦄","🐧","🐺","🐹","🐭","🦊","🐵","🦓"]
    random.shuffle(nicks)
    random.shuffle(avs)
    return nicks, avs


def _assign_alias(room: dict, pid: str):
    pool = room.setdefault("_alias_pool", {"n": [], "a": []})
    if not pool["n"]:
        pool["n"], pool["a"] = _alias_pool()
    p = room["players"][pid]
    p["nickname"] = p.get("nickname") or pool["n"].pop()
    p["avatar"] = p.get("avatar") or pool["a"].pop()


# ─────────────────────────── 共用：建立 / 加入 / 開始 / 結束 ───────────────────────────
def _make_room(rooms: dict, kind: str, settings: dict, lock) -> tuple[str, str]:
    with lock:
        code = _gen_code(rooms)
        token = secrets.token_urlsafe(16)
        rooms[code] = {
            "type": kind,                  # "ult" | "trust"
            "settings": settings,
            "teacher_token": token,
            "phase": "lobby",              # lobby | playing | ended
            "created_at": time.time(),
            "players": {},                 # pid -> {sid,name,role,pair_id,nickname,avatar}
            "pairs": [],                   # list of pair dicts
            "_alias_pool": {"n": [], "a": []},
        }
    return code, token


def _join(rooms: dict, code: str, sid: str, name: str, lock) -> str:
    room = rooms.get(code)
    if not room or room["type"] not in ("ult", "trust"):
        raise HTTPException(404, "找不到房間")
    if room["phase"] != "lobby":
        # 仍允許進入觀看結果，但不再分組
        pass
    with lock:
        existing = next((pid for pid, p in room["players"].items() if p["sid"] == sid), None)
        if existing:
            room["players"][existing]["name"] = name
            return existing
        pid = secrets.token_urlsafe(8)
        room["players"][pid] = {
            "sid": sid, "name": name,
            "role": None, "pair_id": None,
            "joined_at": time.time(),
            "nickname": None, "avatar": None,
        }
        if room["settings"].get("anonymous_mode", True):
            _assign_alias(room, pid)
        return pid


def _start(rooms: dict, code: str, token: str, lock):
    """配對所有大廳玩家並開始遊戲。"""
    room = rooms.get(code)
    if not room or room["type"] not in ("ult", "trust"):
        raise HTTPException(404, "找不到房間")
    if room["teacher_token"] != token:
        raise HTTPException(403, "權限錯誤")
    with lock:
        pids = list(room["players"].keys())
        random.shuffle(pids)
        room["pairs"] = []
        kind = room["type"]
        # 兩兩配對；落單的會分配為「觀察者」（不參與）
        for i in range(0, len(pids) - (len(pids) % 2), 2):
            pid_a, pid_b = pids[i], pids[i+1]
            pair_id = secrets.token_urlsafe(6)
            if kind == "ult":
                # 隨機誰當提案者
                if random.random() < 0.5:
                    proposer, responder = pid_a, pid_b
                else:
                    proposer, responder = pid_b, pid_a
                room["players"][proposer]["role"] = "proposer"
                room["players"][responder]["role"] = "responder"
                room["players"][proposer]["pair_id"] = pair_id
                room["players"][responder]["pair_id"] = pair_id
                room["pairs"].append({
                    "id": pair_id,
                    "proposer": proposer, "responder": responder,
                    "offer": None,            # responder 應拿到的數
                    "accepted": None,         # True/False
                    "payout_p": None, "payout_r": None,
                    "resolved": False,
                })
            else:  # trust
                if random.random() < 0.5:
                    investor, trustee = pid_a, pid_b
                else:
                    investor, trustee = pid_b, pid_a
                room["players"][investor]["role"] = "investor"
                room["players"][trustee]["role"] = "trustee"
                room["players"][investor]["pair_id"] = pair_id
                room["players"][trustee]["pair_id"] = pair_id
                room["pairs"].append({
                    "id": pair_id,
                    "investor": investor, "trustee": trustee,
                    "sent": None, "returned": None,
                    "payout_i": None, "payout_t": None,
                    "resolved": False,
                })
        # 落單者標記為 observer
        if len(pids) % 2:
            odd = pids[-1]
            room["players"][odd]["role"] = "observer"
        room["phase"] = "playing"
    return {"ok": True, "pairs": len(room["pairs"])}


def _end(rooms: dict, code: str, token: str, lock):
    room = rooms.get(code)
    if not room or room["type"] not in ("ult", "trust"):
        raise HTTPException(404, "找不到房間")
    if room["teacher_token"] != token:
        raise HTTPException(403, "權限錯誤")
    with lock:
        room["phase"] = "ended"
    return {"ok": True}


# ─────────────────────────── Ultimatum 邏輯 ───────────────────────────
def _ult_resolve(pair: dict, pie: int, accepted: bool):
    if accepted:
        offer = pair["offer"]
        pair["payout_p"] = pie - offer
        pair["payout_r"] = offer
    else:
        pair["payout_p"] = 0
        pair["payout_r"] = 0
    pair["accepted"] = accepted
    pair["resolved"] = True


def _ult_offer(rooms: dict, code: str, pid: str, offer: int, lock):
    room = rooms.get(code)
    if not room or room["type"] != "ult":
        raise HTTPException(404, "找不到房間")
    settings = room["settings"]
    pie = int(settings.get("pie", 100))
    if offer < 0 or offer > pie:
        raise HTTPException(400, f"offer 必須介於 0–{pie}")
    with lock:
        p = room["players"].get(pid)
        if not p or p.get("role") != "proposer":
            raise HTTPException(400, "你不是提案者")
        pair = next((x for x in room["pairs"] if x["id"] == p["pair_id"]), None)
        if not pair or pair["offer"] is not None:
            raise HTTPException(400, "已提案或無配對")
        pair["offer"] = offer
    return {"ok": True}


def _ult_decide(rooms: dict, code: str, pid: str, accept: bool, lock):
    room = rooms.get(code)
    if not room or room["type"] != "ult":
        raise HTTPException(404, "找不到房間")
    settings = room["settings"]
    pie = int(settings.get("pie", 100))
    with lock:
        p = room["players"].get(pid)
        if not p or p.get("role") != "responder":
            raise HTTPException(400, "你不是回應者")
        pair = next((x for x in room["pairs"] if x["id"] == p["pair_id"]), None)
        if not pair or pair["offer"] is None:
            raise HTTPException(400, "提案者尚未出價")
        if pair["resolved"]:
            raise HTTPException(400, "已決定")
        _ult_resolve(pair, pie, accept)
    return {"ok": True}


# ─────────────────────────── Trust 邏輯 ───────────────────────────
def _trust_send(rooms: dict, code: str, pid: str, amount: int, lock):
    room = rooms.get(code)
    if not room or room["type"] != "trust":
        raise HTTPException(404, "找不到房間")
    settings = room["settings"]
    endow = int(settings.get("endowment", 10))
    if amount < 0 or amount > endow:
        raise HTTPException(400, f"金額必須介於 0–{endow}")
    with lock:
        p = room["players"].get(pid)
        if not p or p.get("role") != "investor":
            raise HTTPException(400, "你不是投資人")
        pair = next((x for x in room["pairs"] if x["id"] == p["pair_id"]), None)
        if not pair or pair["sent"] is not None:
            raise HTTPException(400, "已投資或無配對")
        pair["sent"] = amount
    return {"ok": True}


def _trust_return(rooms: dict, code: str, pid: str, amount: int, lock):
    room = rooms.get(code)
    if not room or room["type"] != "trust":
        raise HTTPException(404, "找不到房間")
    settings = room["settings"]
    endow = int(settings.get("endowment", 10))
    mul = float(settings.get("multiplier", 3.0))
    with lock:
        p = room["players"].get(pid)
        if not p or p.get("role") != "trustee":
            raise HTTPException(400, "你不是受托人")
        pair = next((x for x in room["pairs"] if x["id"] == p["pair_id"]), None)
        if not pair or pair["sent"] is None:
            raise HTTPException(400, "投資人尚未送出")
        if pair["resolved"]:
            raise HTTPException(400, "已回送")
        pool = int(round(pair["sent"] * mul))
        if amount < 0 or amount > pool:
            raise HTTPException(400, f"回送必須介於 0–{pool}")
        pair["returned"] = amount
        pair["payout_i"] = endow - pair["sent"] + amount
        pair["payout_t"] = pool - amount
        pair["resolved"] = True
    return {"ok": True}


# ─────────────────────────── 玩家狀態 ───────────────────────────
def _player_state(rooms: dict, code: str, pid: str):
    room = rooms.get(code)
    if not room or room["type"] not in ("ult", "trust"):
        raise HTTPException(404, "找不到房間")
    p = room["players"].get(pid)
    if not p:
        raise HTTPException(404, "玩家未註冊")

    base = {
        "phase": room["phase"],
        "role": p.get("role"),
        "settings": room["settings"],
        "type": room["type"],
        "my_nickname": p.get("nickname"),
        "my_avatar": p.get("avatar"),
        "player_count": len(room["players"]),
    }

    if room["phase"] == "lobby":
        base["status"] = "waiting"
        return base

    pair = next((x for x in room["pairs"] if x["id"] == p.get("pair_id")), None)

    if room["phase"] == "ended":
        base["status"] = "ended"
        base["my_pair"] = pair
        # 全班統計
        if room["type"] == "ult":
            base["stats"] = _ult_stats(room)
        else:
            base["stats"] = _trust_stats(room)
        return base

    # playing
    if not pair:
        base["status"] = "observer"
        return base

    base["status"] = "playing"
    base["my_pair"] = {
        "id": pair["id"],
        "offer": pair.get("offer"),
        "accepted": pair.get("accepted"),
        "sent": pair.get("sent"),
        "returned": pair.get("returned"),
        "resolved": pair.get("resolved", False),
        "payout_i": pair.get("payout_i"),
        "payout_t": pair.get("payout_t"),
        "payout_p": pair.get("payout_p"),
        "payout_r": pair.get("payout_r"),
    }
    return base


# ─────────────────────────── 統計 ───────────────────────────
def _ult_stats(room: dict) -> dict:
    pie = int(room["settings"].get("pie", 100))
    pairs = [p for p in room["pairs"] if p.get("offer") is not None]
    n = len(pairs)
    if n == 0:
        return {"n": 0, "pie": pie}
    offers = [p["offer"] for p in pairs]
    decided = [p for p in pairs if p["resolved"]]
    accepted = [p for p in decided if p["accepted"]]
    avg = sum(offers) / n
    # 直方圖（每 step 一格）
    step = max(1, int(room["settings"].get("step", 5)))
    bins = {}
    for o in offers:
        k = (o // step) * step
        bins[k] = bins.get(k, 0) + 1
    histogram = sorted(bins.items())
    # 接受率分群
    acc_by_offer = {}
    for p in decided:
        k = (p["offer"] // step) * step
        d = acc_by_offer.setdefault(k, {"total": 0, "acc": 0})
        d["total"] += 1
        if p["accepted"]:
            d["acc"] += 1
    acc_curve = sorted([(k, v["acc"], v["total"]) for k, v in acc_by_offer.items()])
    return {
        "n": n,
        "pie": pie,
        "step": step,
        "avg_offer": round(avg, 2),
        "median_offer": sorted(offers)[n // 2],
        "min_offer": min(offers),
        "max_offer": max(offers),
        "fair_rate": round(sum(1 for o in offers if o >= pie * 0.4) / n, 3),  # ≥40% 視為公平
        "decided": len(decided),
        "accept_rate": round(len(accepted) / len(decided), 3) if decided else None,
        "histogram": histogram,
        "accept_curve": acc_curve,
    }


def _trust_stats(room: dict) -> dict:
    endow = int(room["settings"].get("endowment", 10))
    mul = float(room["settings"].get("multiplier", 3.0))
    pairs = [p for p in room["pairs"] if p.get("sent") is not None]
    n = len(pairs)
    if n == 0:
        return {"n": 0, "endowment": endow, "multiplier": mul}
    sents = [p["sent"] for p in pairs]
    resolved = [p for p in pairs if p["resolved"]]
    avg_sent = sum(sents) / n
    sent_pct = avg_sent / endow if endow else 0
    # 信任分布
    bins = {}
    for s in sents:
        bins[s] = bins.get(s, 0) + 1
    sent_hist = sorted(bins.items())
    # 回送比例
    return_ratios = []
    for p in resolved:
        pool = p["sent"] * mul
        if pool > 0:
            return_ratios.append(p["returned"] / pool)
    avg_ret_ratio = sum(return_ratios) / len(return_ratios) if return_ratios else None
    # 信任是否有回報？（投資人淨利平均）
    net_invs = [p["payout_i"] - endow for p in resolved]
    avg_net_inv = sum(net_invs) / len(net_invs) if net_invs else None
    return {
        "n": n,
        "endowment": endow,
        "multiplier": mul,
        "avg_sent": round(avg_sent, 2),
        "sent_pct": round(sent_pct, 3),
        "median_sent": sorted(sents)[n // 2],
        "decided": len(resolved),
        "avg_return_ratio": round(avg_ret_ratio, 3) if avg_ret_ratio is not None else None,
        "avg_net_investor": round(avg_net_inv, 2) if avg_net_inv is not None else None,
        "trust_paid_off": (avg_net_inv > 0) if avg_net_inv is not None else None,
        "sent_hist": sent_hist,
        "trustees_returned_more_than_sent": sum(1 for p in resolved if p["returned"] > p["sent"]),
    }


# ─────────────────────────── Teacher 儀表板 ───────────────────────────
def _dashboard(rooms: dict, code: str, token: str):
    room = rooms.get(code)
    if not room or room["type"] not in ("ult", "trust"):
        raise HTTPException(404, "找不到房間")
    if room["teacher_token"] != token:
        raise HTTPException(403, "權限錯誤")

    players = []
    for pid, p in room["players"].items():
        players.append({
            "pid": pid, "sid": p["sid"], "name": p["name"],
            "nickname": p.get("nickname"), "avatar": p.get("avatar"),
            "role": p.get("role"), "pair_id": p.get("pair_id"),
        })
    pairs_view = []
    for x in room["pairs"]:
        d = dict(x)
        if room["type"] == "ult":
            d["proposer_name"] = room["players"][x["proposer"]].get("nickname") or room["players"][x["proposer"]]["name"]
            d["responder_name"] = room["players"][x["responder"]].get("nickname") or room["players"][x["responder"]]["name"]
        else:
            d["investor_name"] = room["players"][x["investor"]].get("nickname") or room["players"][x["investor"]]["name"]
            d["trustee_name"] = room["players"][x["trustee"]].get("nickname") or room["players"][x["trustee"]]["name"]
        pairs_view.append(d)
    stats = _ult_stats(room) if room["type"] == "ult" else _trust_stats(room)
    return {
        "code": code,
        "type": room["type"],
        "phase": room["phase"],
        "settings": room["settings"],
        "created_at": room["created_at"],
        "player_count": len(room["players"]),
        "players": players,
        "pairs": pairs_view,
        "stats": stats,
    }


# ─────────────────────────── 路由註冊 ───────────────────────────
def register(app, rooms: dict, lock):
    """Register routes onto a FastAPI app, sharing rooms dict + threading lock."""

    # Pages
    @app.get("/teacher-ultimatum")
    def _p1():
        from fastapi.responses import FileResponse
        return FileResponse("static/teacher-ultimatum.html")

    @app.get("/ultimatum")
    def _p2():
        from fastapi.responses import FileResponse
        return FileResponse("static/ultimatum.html")

    @app.get("/teacher-trust")
    def _p3():
        from fastapi.responses import FileResponse
        return FileResponse("static/teacher-trust.html")

    @app.get("/trust")
    def _p4():
        from fastapi.responses import FileResponse
        return FileResponse("static/trust.html")

    # Teacher: create
    @app.post("/api/teacher/ult-rooms")
    def create_ult(s: UltSettings):
        code, token = _make_room(rooms, "ult", s.dict(), lock)
        return {"code": code, "teacher_token": token}

    @app.post("/api/teacher/trust-rooms")
    def create_trust(s: TrustSettings):
        code, token = _make_room(rooms, "trust", s.dict(), lock)
        return {"code": code, "teacher_token": token}

    # Teacher: start / end
    @app.post("/api/teacher/exp-rooms/{code}/start")
    def start(code: str, token: str):
        return _start(rooms, code, token, lock)

    @app.post("/api/teacher/exp-rooms/{code}/end")
    def end_room(code: str, token: str):
        return _end(rooms, code, token, lock)

    @app.get("/api/teacher/exp-rooms/{code}")
    def dashboard(code: str, token: str):
        return _dashboard(rooms, code, token)

    # Student: join (兼容 /api/rooms/{code}/join)，這裡專屬路徑避免和 PD 衝突
    @app.post("/api/exp-rooms/{code}/join")
    def join(code: str, req: JoinReq):
        pid = _join(rooms, code, req.sid, req.name, lock)
        return {"player_id": pid}

    @app.get("/api/exp-rooms/{code}")
    def get_room(code: str):
        room = rooms.get(code)
        if not room or room["type"] not in ("ult", "trust"):
            raise HTTPException(404, "找不到房間")
        return {
            "code": code, "type": room["type"],
            "settings": room["settings"], "phase": room["phase"],
            "player_count": len(room["players"]),
        }

    @app.get("/api/exp-rooms/{code}/players/{pid}")
    def state(code: str, pid: str):
        return _player_state(rooms, code, pid)

    # Ultimatum actions
    @app.post("/api/exp-rooms/{code}/players/{pid}/offer")
    def ult_offer(code: str, pid: str, req: OfferReq):
        return _ult_offer(rooms, code, pid, req.offer, lock)

    @app.post("/api/exp-rooms/{code}/players/{pid}/decide")
    def ult_decide(code: str, pid: str, req: DecisionReq):
        return _ult_decide(rooms, code, pid, req.accept, lock)

    # Trust actions
    @app.post("/api/exp-rooms/{code}/players/{pid}/send")
    def trust_send(code: str, pid: str, req: SendReq):
        return _trust_send(rooms, code, pid, req.amount, lock)

    @app.post("/api/exp-rooms/{code}/players/{pid}/return")
    def trust_return(code: str, pid: str, req: ReturnReq):
        return _trust_return(rooms, code, pid, req.amount, lock)
