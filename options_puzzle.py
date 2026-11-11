"""
選擇權拼圖 Options Puzzle — 登記與成績儲存

學生從 /options-puzzle 選語言、登記系級/學號/姓名，再進入中文或英文版。
成績以學號為鍵存在 data/optpuzzle.json；每一次「結束並儲存」也會寫入
data/optpuzzle-attempts.jsonl 留下完整紀錄。

成績規則：
  - 第一次玩或「繼續上次進度」：每過一關自動存，逐關取最高分。
  - 「重新挑戰」：從零開始，按「結束並儲存」時由學生選擇覆蓋或保留原成績。
"""

import csv
import io
import json
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

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


def _load() -> Dict[str, dict]:
    try:
        with open(RECORDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(records: Dict[str, dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RECORDS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RECORDS_PATH)


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


def _public(rec: Optional[dict]) -> Optional[dict]:
    if not rec:
        return None
    return {k: rec.get(k) for k in
            ("dept", "sid", "name", "lang", "best", "score", "max_score",
             "solved", "plays", "created_at", "updated_at")}


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


@router.post("/api/optpuzzle/login")
def login(req: LoginReq):
    dept, sid, name = _check_identity(req.dept, req.sid, req.name)
    with _lock:
        rec = _load().get(sid)
    return {"sid": sid, "record": _public(rec)}


class SaveReq(BaseModel):
    dept: str
    sid: str
    name: str
    lang: str = "zh"
    best: Dict[str, int] = {}
    max_score: int = 0
    # 'auto'     : 第一次 / 繼續進度，逐關取最高分合併進正式成績
    # 'overwrite': 重新挑戰後選擇覆蓋原成績
    # 'keep'     : 重新挑戰後選擇保留原成績（只記錄這次嘗試）
    mode: str = "auto"


@router.post("/api/optpuzzle/save")
def save(req: SaveReq):
    dept, sid, name = _check_identity(req.dept, req.sid, req.name)
    if req.mode not in ("auto", "overwrite", "keep"):
        raise HTTPException(400, "mode 錯誤")
    lang = "en" if req.lang == "en" else "zh"
    best = _clean_best(req.best)
    score = sum(best.values())
    max_score = max(0, min(int(req.max_score or 0), (MAX_LEVEL_INDEX + 1) * MAX_PTS_PER_LEVEL))
    now = _now_iso()

    with _lock:
        records = _load()
        rec = records.get(sid)
        if req.mode == "auto":
            if rec:
                merged = dict(rec.get("best") or {})
                for k, v in best.items():
                    merged[k] = max(merged.get(k, 0), v)
                best = merged
            score = sum(best.values())
        if req.mode in ("auto", "overwrite") or not rec:
            new = dict(rec or {"created_at": now, "plays": 1})
            new.update({
                "dept": dept, "sid": sid, "name": name, "lang": lang,
                "best": best, "score": score,
                "max_score": max(max_score, (rec or {}).get("max_score", 0)),
                "solved": len(best), "updated_at": now,
            })
            records[sid] = new
        else:
            new = rec
        if req.mode != "auto":
            new["plays"] = (new.get("plays") or 1) + (1 if rec else 0)
            records[sid] = new
        _save(records)
        if req.mode != "auto":
            _log_attempt({"ts": now, "sid": sid, "dept": dept, "name": name,
                          "lang": lang, "mode": req.mode,
                          "attempt_score": sum(_clean_best(req.best).values()),
                          "attempt_best": _clean_best(req.best),
                          "official_score": new["score"]})
    return {"ok": True, "record": _public(records[sid])}


# ── 教師 API ─────────────────────────────────────────────────────────

def _require_admin(token: str) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(403, "伺服器未設定 ADMIN_TOKEN")
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "token 錯誤")


@router.get("/api/optpuzzle/records")
def records(token: str = ""):
    _require_admin(token)
    with _lock:
        recs = list(_load().values())
    recs.sort(key=lambda r: (r.get("dept", ""), r.get("sid", "")))
    return {"records": [_public(r) for r in recs]}


@router.get("/api/optpuzzle/export.csv")
def export_csv(token: str = ""):
    _require_admin(token)
    with _lock:
        recs = sorted(_load().values(), key=lambda r: (r.get("dept", ""), r.get("sid", "")))
    levels = sorted({int(k) for r in recs for k in (r.get("best") or {})})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["系級", "學號", "姓名", "語言", "總分", "滿分", "過關數", "遊玩次數",
                "首次登記", "最後更新"] + [f"第{i + 1}關" for i in levels])
    for r in recs:
        b = r.get("best") or {}
        w.writerow([r.get("dept"), r.get("sid"), r.get("name"), r.get("lang"),
                    r.get("score"), r.get("max_score"), r.get("solved"), r.get("plays"),
                    r.get("created_at"), r.get("updated_at")]
                   + [b.get(str(i), "") for i in levels])
    data = "﻿" + buf.getvalue()  # BOM 讓 Excel 正確顯示中文
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="options-puzzle-scores.csv"'})
