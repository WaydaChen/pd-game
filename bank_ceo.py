"""
商銀行長的第一年 Part I — 登記與成績儲存

學生在 /bank-ceo 開場登記系級、學號、姓名後開始；結算時寫下問題、按「送出成績」。
  - data/bankceo.json：以學號為鍵，保留最高分那一次（同分取較新）與最近一次。
  - data/bankceo-attempts.jsonl：每一次送出都留下完整紀錄（含學生寫的問題）。
  - data/bankceo-logins.jsonl：每一次登入（按「開始上班」）都記一筆，當作點名紀錄。
成績只認學號；姓名不符時開場會提示（顯示遮蔽後的舊姓名），學生確認後仍可繼續，
紀錄改用最新姓名，舊姓名留在 names。
教師頁 /teacher-bank-ceo；成績與 CSV 不需密碼即可讀取（依老師要求）。
"""

import csv
import io
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from options_puzzle import DATA_DIR, _check_identity, _mask, _name_key

router = APIRouter()

RECORDS_PATH = os.path.join(DATA_DIR, "bankceo.json")
ATTEMPTS_PATH = os.path.join(DATA_DIR, "bankceo-attempts.jsonl")
LOGINS_PATH = os.path.join(DATA_DIR, "bankceo-logins.jsonl")
TW = timezone(timedelta(hours=8))  # Railway 主機是 UTC；點名日期以台灣時間為準


def _now_iso() -> str:
    return datetime.now(TW).isoformat(timespec="seconds")

ENDINGS = {
    "T": "被派員接管", "U": "被派員監管", "R": "被限期改善", "A": "穩健經營獎",
    "G": "賭贏了，但金管會盯上你了", "P": "獲利王，但被盯上了",
    "C": "靠央行救了一命", "S": "驚險過關",
}
CAMELS_KEYS = "CAMELS"

_lock = threading.Lock()


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


def _load_jsonl(path: str) -> List[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _clip(s, n: int) -> str:
    return str(s or "").strip()[:n]


# ── 頁面 ─────────────────────────────────────────────────────────────

@router.get("/bank-ceo")
def game_page():
    return FileResponse("static/bank-ceo.html")


@router.get("/teacher-bank-ceo")
def teacher_page():
    return FileResponse("static/teacher-bank-ceo.html")


# ── 學生 API ─────────────────────────────────────────────────────────

class LoginReq(BaseModel):
    dept: str
    sid: str
    name: str
    confirm: bool = False   # 姓名與舊紀錄不同時，學生按「是我，繼續」後才算登入


def _add_name(rec: dict, name: str) -> None:
    names = list(rec.get("names") or ([rec["name"]] if rec.get("name") else []))
    if all(_name_key(n) != _name_key(name) for n in names):
        names.append(name)
    rec["names"] = names


def _public(rec):
    if not rec:
        return None
    return {k: rec.get(k) for k in ("dept", "sid", "name", "names", "plays", "best", "last",
                                    "logins", "first_login", "last_login",
                                    "first_submit", "created_at", "updated_at")}


@router.post("/api/bankceo/login")
def login(req: LoginReq):
    dept, sid, name = _check_identity(req.dept, req.sid, req.name)
    now = _now_iso()
    with _lock:
        records = _load()
        rec = records.get(sid)
        mismatch = bool(rec) and _name_key(rec.get("name")) != _name_key(name)
        if mismatch and not req.confirm:
            return {"sid": sid, "record": _public(rec), "name_hint": _mask(rec.get("name"))}
        new = dict(rec or {"created_at": now, "plays": 0})
        _add_name(new, name)
        new.update({"dept": dept, "sid": sid, "name": name,
                    "logins": (new.get("logins") or 0) + 1,
                    "first_login": new.get("first_login") or now, "last_login": now})
        records[sid] = new
        _save(records)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOGINS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now, "sid": sid, "dept": dept, "name": name},
                               ensure_ascii=False) + "\n")
    return {"sid": sid, "record": _public(new), "name_hint": None}


class SubmitReq(BaseModel):
    dept: str
    sid: str
    name: str
    score: int
    end_key: str
    camels: Dict[str, int] = {}     # {"C":1, "A":2, ...}
    comp: int = 0
    wrong: int = 0
    wrong_log: Dict[str, int] = {}
    car: float = 0
    roe: float = 0
    notes: List[str] = []
    question: str


@router.post("/api/bankceo/submit")
def submit(req: SubmitReq):
    dept, sid, name = _check_identity(req.dept, req.sid, req.name)
    end_key = (req.end_key or "").strip().upper()
    if end_key not in ENDINGS:
        raise HTTPException(400, "結局代號錯誤")
    question = _clip(req.question, 2000)
    if len(question) < 15:
        raise HTTPException(400, "問題至少要寫 15 個字")
    now = _now_iso()
    run = {
        "ts": now,
        "score": max(0, min(100, int(req.score))),
        "end_key": end_key,
        "end": ENDINGS[end_key],
        "camels": {k: max(1, min(5, int(req.camels.get(k, 5)))) for k in CAMELS_KEYS},
        "comp": max(1, min(5, int(req.comp or 5))),
        "wrong": max(0, min(200, int(req.wrong))),
        "wrong_log": {_clip(k, 30): max(0, min(200, int(v)))
                      for k, v in list(req.wrong_log.items())[:20]},
        "car": round(max(-10.0, min(10.0, float(req.car))), 4),
        "roe": round(max(-10.0, min(10.0, float(req.roe))), 4),
        "notes": [_clip(n, 200) for n in req.notes[:30]],
        "question": question,
    }
    with _lock:
        records = _load()
        rec = records.get(sid)
        new = dict(rec or {"created_at": now, "plays": 0})
        _add_name(new, name)
        new.update({"dept": dept, "sid": sid, "name": name, "updated_at": now,
                    "first_submit": new.get("first_submit") or now,
                    "plays": (new.get("plays") or 0) + 1, "last": run})
        if not new.get("best") or run["score"] >= new["best"]["score"]:
            new["best"] = run
        records[sid] = new
        _save(records)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ATTEMPTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"sid": sid, "dept": dept, "name": name, **run},
                               ensure_ascii=False) + "\n")
    return {"ok": True, "record": _public(new)}


# ── 教師 API ─────────────────────────────────────────────────────────

@router.get("/api/bankceo/records")
def records():
    with _lock:
        recs = sorted(_load().values(), key=lambda r: (r.get("dept", ""), r.get("sid", "")))
    return {"records": [_public(r) for r in recs]}


def _csv_response(rows: List[list], filename: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _run_cols(run: dict) -> list:
    cm = run.get("camels") or {}
    wl = "、".join(f"{k}×{v}" for k, v in (run.get("wrong_log") or {}).items())
    return ([run.get("score"), f'{run.get("end_key")} {run.get("end")}' if run.get("end_key") else "", run.get("comp")]
            + [cm.get(k, "") for k in CAMELS_KEYS]
            + [run.get("wrong"), wl, run.get("question"), " / ".join(run.get("notes") or [])])


RUN_HEAD = (["分數", "結局", "CAMELS綜合"] + [f"CAMELS-{k}" for k in CAMELS_KEYS]
            + ["觀念題答錯", "答錯明細", "學生的問題", "決策紀錄"])


@router.get("/api/bankceo/export.csv")
def export_csv():
    """每位學生一列：最高分那一次的完整內容。"""
    with _lock:
        recs = sorted(_load().values(), key=lambda r: (r.get("dept", ""), r.get("sid", "")))
    rows = [["系級", "學號", "姓名", "曾用姓名", "登入次數", "第一次登入", "最後登入",
             "送出次數", "第一次送出", "最後送出"] + RUN_HEAD]
    for r in recs:
        others = [n for n in (r.get("names") or []) if n != r.get("name")]
        rows.append([r.get("dept"), r.get("sid"), r.get("name"), "、".join(others),
                     r.get("logins") or 0, r.get("first_login"), r.get("last_login"),
                     r.get("plays") or 0, r.get("first_submit") or (r.get("created_at") if r.get("plays") else ""),
                     r.get("updated_at") if r.get("plays") else ""] + _run_cols(r.get("best") or {}))
    return _csv_response(rows, "bank-ceo-scores.csv")


@router.get("/api/bankceo/attempts.csv")
def export_attempts():
    """每次送出一列（含每一次寫的問題）。"""
    with _lock:
        atts = _load_jsonl(ATTEMPTS_PATH)
    rows = [["送出時間", "系級", "學號", "姓名"] + RUN_HEAD]
    for a in atts:
        rows.append([a.get("ts"), a.get("dept"), a.get("sid"), a.get("name")] + _run_cols(a))
    return _csv_response(rows, "bank-ceo-attempts.csv")


@router.get("/api/bankceo/attendance.csv")
def export_attendance():
    """點名紀錄：每位學生每天一列（台灣時間），含當天第一次與最後一次登入時間、登入次數。"""
    with _lock:
        logs = _load_jsonl(LOGINS_PATH)
        recs = _load()
    days: Dict[tuple, dict] = {}
    for g in logs:
        ts = g.get("ts") or ""
        key = (ts[:10], g.get("sid"))
        d = days.setdefault(key, {"first": ts, "last": ts, "n": 0,
                                  "dept": g.get("dept"), "name": g.get("name")})
        d["first"], d["last"], d["n"] = min(d["first"], ts), max(d["last"], ts), d["n"] + 1
        d["dept"], d["name"] = g.get("dept"), g.get("name")
    rows = [["日期", "系級", "學號", "姓名", "第一次登入", "最後登入", "當天登入次數", "目前最高分"]]
    for (day, sid), d in sorted(days.items(), key=lambda kv: (kv[0][0], kv[1]["dept"] or "", kv[0][1] or "")):
        best = (recs.get(sid) or {}).get("best") or {}
        rows.append([day, d["dept"], sid, d["name"], d["first"][11:19], d["last"][11:19], d["n"],
                     best.get("score", "")])
    return _csv_response(rows, "bank-ceo-attendance.csv")

