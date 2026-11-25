"""
選擇權拼圖 Options Puzzle — 登記與成績儲存

學生從 /options-puzzle 選語言、登記系級/學號/姓名，再進入中文或英文版。
成績分「輪次」存放（例如每個班一輪）：老師在教師頁開新的一輪，學生登入時
進入當時開放的那一輪；同一輪內以學號為鍵。全部存在 data/optpuzzle.json：
  {"rounds": [{"id", "name", "created_at"}], "current": id, "records": {id: {sid: rec}}}
舊格式（直接 {sid: rec}）讀取時自動轉成「第一輪（舊資料）」。
每一次「結束並儲存」也會寫入 data/optpuzzle-attempts.jsonl 留下完整紀錄。

成績規則（每一輪各自計算）：
  - 本輪成績 score：
      第一次玩或「繼續上次進度」：每過一關自動存，逐關取最高星數。
      「重新挑戰」：從零開始，按「結束並儲存」後，本輪成績改成這一次的成績。
  - 本輪最高 high_score：本輪成績出現過的最高分（記下當時各關星數 high_best，
    配分改了也能重算）。超過時回傳 new_high，遊戲頁顯示「刷新最高紀錄」。
"""

import csv
import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

router = APIRouter()

DATA_DIR = os.environ.get("ZIP_DATA_DIR", "data")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
RECORDS_PATH = os.path.join(DATA_DIR, "optpuzzle.json")
ATTEMPTS_PATH = os.path.join(DATA_DIR, "optpuzzle-attempts.jsonl")

MAX_PTS_PER_LEVEL = 3
MAX_LEVEL_INDEX = 100

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_sid(sid: str) -> str:
    return (sid or "").strip().upper()


def _new_round(db: dict, name: str) -> dict:
    rid = "r%d" % int(time.time() * 1000)
    while rid in db["records"]:
        rid += "x"
    r = {"id": rid, "name": name, "created_at": _now_iso()}
    db["rounds"].append(r)
    db["records"][rid] = {}
    db["current"] = rid
    return r


def _load() -> dict:
    try:
        with open(RECORDS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = None
    changed = False
    if isinstance(raw, dict) and isinstance(raw.get("rounds"), list) and isinstance(raw.get("records"), dict):
        db = raw
    else:  # 舊格式或沒有資料
        db, changed = {"rounds": [], "current": None, "records": {}}, True
        if raw:
            r = _new_round(db, "第一輪（舊資料）")
            db["records"][r["id"]] = raw
    if not db["rounds"]:
        _new_round(db, "第一輪")
        changed = True
    if db.get("current") not in db["records"]:
        db["current"] = db["rounds"][-1]["id"]
        changed = True
    if changed:  # 轉換或建立的輪次要馬上寫回，否則每次讀取都會產生新的輪次編號
        _save(db)
    return db


def _save(db: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RECORDS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RECORDS_PATH)


def _round_info(db: dict, rid: str) -> Optional[dict]:
    return next((r for r in db["rounds"] if r["id"] == rid), None)


def _pick_round(db: dict, rid: str) -> str:
    """學生端：沒指定就用目前開放的輪次；指定了不存在的輪次就報錯。"""
    rid = rid or db["current"]
    if rid not in db["records"]:
        raise HTTPException(409, {"code": "round_gone"})
    return rid


def _log_attempt(entry: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ATTEMPTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _clean_best(best: Dict[str, int]) -> Dict[str, int]:
    out = {}
    for k, v in (best or {}).items():
        try:
            i, p = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= i <= MAX_LEVEL_INDEX:
            out[str(i)] = max(0, min(MAX_PTS_PER_LEVEL, p))
    return out


def _score(best: Dict[str, int], pts: List[int]) -> int:
    """每關得分 = 配分 × 星數 / 3（四捨五入）；沒有配分表時退回星數加總。"""
    if not pts:
        return sum(best.values())
    total = 0
    for k, stars in best.items():
        i = int(k)
        if i < len(pts):
            total += int(pts[i] * min(stars, MAX_PTS_PER_LEVEL) / MAX_PTS_PER_LEVEL + 0.5)
    return total


def _clean_pts(pts: List[int]) -> List[int]:
    out = []
    for v in (pts or [])[:MAX_LEVEL_INDEX + 1]:
        try:
            out.append(max(0, min(100, int(v))))
        except (TypeError, ValueError):
            out.append(0)
    return out


def _name_key(name: str) -> str:
    return "".join((name or "").split()).casefold()


def _mask(name: str) -> str:
    n = (name or "").strip()
    if len(n) <= 1:
        return n
    if len(n) == 2:
        return n[0] + "○"
    return n[0] + "○" * (len(n) - 2) + n[-1]


def _check_owner(rec: Optional[dict], name: str) -> None:
    """同一個學號只能由第一次登記的姓名使用，避免打錯學號蓋掉別人的成績。"""
    if rec and _name_key(rec.get("name")) != _name_key(name):
        raise HTTPException(409, {"code": "name_mismatch", "masked": _mask(rec.get("name"))})


def _public(rec: Optional[dict]) -> Optional[dict]:
    if not rec:
        return None
    out = {k: rec.get(k) for k in
           ("dept", "sid", "name", "lang", "best", "score", "max_score",
            "solved", "plays", "created_at", "updated_at")}
    out["high_score"] = rec.get("high_score", rec.get("score"))
    out["high_at"] = rec.get("high_at", rec.get("updated_at"))
    return out


def _check_identity(dept: str, sid: str, name: str) -> tuple:
    dept, name, sid = (dept or "").strip(), (name or "").strip(), _norm_sid(sid)
    if not dept or not sid or not name:
        raise HTTPException(400, "請填寫系級、學號、姓名")
    if len(dept) > 40 or len(sid) > 20 or len(name) > 40:
        raise HTTPException(400, "欄位過長")
    return dept, sid, name


# ── 頁面 ─────────────────────────────────────────────────────────────

@router.get("/options-puzzle")
def entry_page():
    return FileResponse("static/options-puzzle-entry.html")


@router.get("/options-puzzle/zh")
def puzzle_zh():
    return FileResponse("static/options-puzzle_2.html")


@router.get("/options-puzzle/en")
def puzzle_en():
    return FileResponse("static/options-puzzle-en_2.html")


@router.get("/teacher-options-puzzle")
def teacher_page():
    return FileResponse("static/teacher-options-puzzle.html")


# ── 學生 API ─────────────────────────────────────────────────────────

class LoginReq(BaseModel):
    dept: str
    sid: str
    name: str
    round: str = ""   # 從遊戲頁回到入口時帶著原本的輪次；新登入留空 = 目前開放的輪次


@router.get("/api/optpuzzle/round")
def current_round():
    with _lock:
        db = _load()
    return {"round": _round_info(db, db["current"])}


@router.post("/api/optpuzzle/login")
def login(req: LoginReq):
    dept, sid, name = _check_identity(req.dept, req.sid, req.name)
    with _lock:
        db = _load()
        rid = _pick_round(db, req.round)
        rec = db["records"][rid].get(sid)
    _check_owner(rec, name)
    return {"sid": sid, "record": _public(rec), "round": _round_info(db, rid)}


class SaveReq(BaseModel):
    dept: str
    sid: str
    name: str
    lang: str = "zh"
    best: Dict[str, int] = {}
    pts: List[int] = []          # 每關配分（依關卡順序）
    max_score: int = 0
    round: str = ""
    # 'auto'  : 第一次 / 繼續進度，逐關取最高星數合併進本輪成績
    # 'replay': 重新挑戰結束，本輪成績改成這一次的成績
    #           （舊版頁面送來的 'overwrite' / 'keep' 也當作 'replay'）
    mode: str = "auto"


@router.post("/api/optpuzzle/save")
def save(req: SaveReq):
    dept, sid, name = _check_identity(req.dept, req.sid, req.name)
    mode = {"auto": "auto", "replay": "replay", "overwrite": "replay", "keep": "replay"}.get(req.mode)
    if not mode:
        raise HTTPException(400, "mode 錯誤")
    lang = "en" if req.lang == "en" else "zh"
    best = _clean_best(req.best)
    attempt_best = dict(best)
    pts = _clean_pts(req.pts)
    max_score = sum(pts) if pts else max(0, min(int(req.max_score or 0), 1000))
    now = _now_iso()

    with _lock:
        db = _load()
        rid = _pick_round(db, req.round)
        records = db["records"][rid]
        rec = records.get(sid)
        _check_owner(rec, name)
        if mode == "auto" and rec:
            merged = dict(rec.get("best") or {})
            for k, v in best.items():
                merged[k] = max(merged.get(k, 0), v)
            best = merged
        score = _score(best, pts)
        # 本輪最高：用當時的各關星數、以目前配分重算後比較
        high_best = (rec.get("high_best") or rec.get("best") or {}) if rec else {}
        prev_high = _score(high_best, pts) if rec else None
        new_high = prev_high is not None and score > prev_high
        if prev_high is None or new_high:
            high_best, high_score, high_at = dict(best), score, now
        else:
            high_score, high_at = prev_high, rec.get("high_at") or rec.get("updated_at") or now
        new = dict(rec or {"created_at": now, "plays": 1})
        if mode == "replay" and rec:
            new["plays"] = (rec.get("plays") or 1) + 1
        new.update({
            "dept": dept, "sid": sid, "name": name, "lang": lang,
            "best": best, "score": score, "pts": pts, "max_score": max_score,
            "solved": len(best), "updated_at": now,
            "high_best": high_best, "high_score": high_score, "high_at": high_at,
        })
        records[sid] = new
        _save(db)
        if mode == "replay":
            _log_attempt({"ts": now, "round": rid, "sid": sid, "dept": dept, "name": name,
                          "lang": lang, "mode": mode,
                          "attempt_score": _score(attempt_best, pts),
                          "attempt_best": attempt_best,
                          "high_score": high_score, "new_high": new_high})
    return {"ok": True, "record": _public(new), "new_high": new_high, "prev_high": prev_high}


# ── 教師 API ─────────────────────────────────────────────────────────

def _require_admin(token: str) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(403, "伺服器未設定 ADMIN_TOKEN")
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "token 錯誤")


def _rounds_out(db: dict) -> dict:
    return {"current": db["current"],
            "rounds": [dict(r, count=len(db["records"].get(r["id"]) or {})) for r in db["rounds"]]}


@router.get("/api/optpuzzle/rounds")
def list_rounds(token: str = ""):
    _require_admin(token)
    with _lock:
        return _rounds_out(_load())


class RoundReq(BaseModel):
    token: str
    name: str = ""
    id: str = ""


@router.post("/api/optpuzzle/rounds")
def create_round(req: RoundReq):
    """開新的一輪並設為目前開放的輪次。"""
    _require_admin(req.token)
    name = (req.name or "").strip()[:40]
    with _lock:
        db = _load()
        _new_round(db, name or f"第 {len(db['rounds']) + 1} 輪")
        _save(db)
        return _rounds_out(db)


@router.post("/api/optpuzzle/rounds/current")
def set_current_round(req: RoundReq):
    """把某一輪重新設為開放（例如同一班下次上課接著玩）。"""
    _require_admin(req.token)
    with _lock:
        db = _load()
        if req.id not in db["records"]:
            raise HTTPException(404, "找不到這一輪")
        db["current"] = req.id
        _save(db)
        return _rounds_out(db)


def _round_records(db: dict, rid: str) -> List[dict]:
    rid = rid or db["current"]
    if rid not in db["records"]:
        raise HTTPException(404, "找不到這一輪")
    return sorted(db["records"][rid].values(), key=lambda r: (r.get("dept", ""), r.get("sid", "")))


@router.get("/api/optpuzzle/records")
def records(token: str = "", round: str = ""):
    _require_admin(token)
    with _lock:
        recs = _round_records(_load(), round)
    return {"records": [_public(r) for r in recs]}


@router.get("/api/optpuzzle/export.csv")
def export_csv(token: str = "", round: str = ""):
    _require_admin(token)
    with _lock:
        db = _load()
        recs = _round_records(db, round)
        rname = (_round_info(db, round or db["current"]) or {}).get("name", "")
    levels = sorted({int(k) for r in recs for k in (r.get("best") or {})})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["輪次", "系級", "學號", "姓名", "語言", "本輪成績", "本輪最高", "滿分", "過關數", "遊玩次數",
                "首次登記", "最後更新"] + [f"第{i + 1}關" for i in levels])
    for r in recs:
        b = r.get("best") or {}
        w.writerow([rname, r.get("dept"), r.get("sid"), r.get("name"), r.get("lang"),
                    r.get("score"), r.get("high_score", r.get("score")), r.get("max_score"), r.get("solved"), r.get("plays"),
                    r.get("created_at"), r.get("updated_at")]
                   + [b.get(str(i), "") for i in levels])
    data = "﻿" + buf.getvalue()  # BOM 讓 Excel 正確顯示中文
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="options-puzzle-scores.csv"'})
