"""
ZIP-Code 期貨交易遊戲 — FastAPI 後端（含撮合引擎）

複製並延伸自 Damianova & Damianov (2018) 的課堂期貨交易遊戲。
以 WebSocket 提供多人即時市場，事件同步 append 到 data/{code}.jsonl。

設計優先序（規格 §14）：資料完整性 > 撮合正確性 > 連線韌性 > 電子模式 > 前端 > 喊價模式。
"""

import asyncio
import hashlib
import json
import os
import random
import secrets
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

router = APIRouter()

DATA_DIR = os.environ.get("ZIP_DATA_DIR", "data")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

TOTAL_ROUNDS = 5
DIGIT_EXPECTED = 4.5           # 單一數字期望值
CONFUSING = set("O0I1l")      # 房間代碼避開的易混淆字元
BADGE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COLORS = ["#7c6af7", "#4ecb8a", "#f0b429", "#4a9eff", "#ff7eb6", "#42d4f4",
          "#a594ff", "#ff8a5c", "#5cd6c0", "#d68cff", "#ffd15c", "#8ce85c"]


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()


# ── 資料模型 ──────────────────────────────────────────────────────────

@dataclass
class Order:
    id: str
    trader_id: str
    side: str            # 'bid' | 'offer'
    price: int
    round: int
    ts: float
    status: str = "live"   # 'live' | 'filled' | 'withdrawn' | 'expired'
    ended_at: Optional[float] = None


@dataclass
class Trade:
    id: str
    round: int
    price: int
    buyer_id: str
    seller_id: str
    maker_id: str
    taker_id: str
    ts: float
    best_bid: Optional[int]
    best_offer: Optional[int]
    slippage: int


@dataclass
class Trader:
    id: str
    name: str
    badge: str
    color: str
    joined_at: float
    joined_round: int


@dataclass
class Market:
    code: str
    host_key: str
    mode: str                       # 'electronic' | 'outcry'
    created_at: float
    round_seconds: int = 180
    visible_fraction: float = 0.4
    resample_seconds: int = 3
    orig_margin: float = 10.0
    maint_margin: float = 8.0
    secret_digits: list = field(default_factory=list)   # 永不外送
    revealed: list = field(default_factory=list)
    round: int = 0
    phase: str = "lobby"            # 'lobby' | 'open' | 'closed' | 'settled'
    round_ends_at: Optional[float] = None
    traders: dict = field(default_factory=dict)         # id -> Trader
    book: list = field(default_factory=list)            # live orders (當前輪)
    all_orders: list = field(default_factory=list)      # 全部歷史委託
    trades: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def delivery_price(self) -> Optional[int]:
        if self.phase == "settled":
            return sum(self.secret_digits)
        return None


MARKETS: dict = {}          # code -> Market
CONNS: dict = {}            # code -> list[Conn]


@dataclass
class Conn:
    ws: WebSocket
    role: str = "spectator"   # 'trader' | 'host' | 'spectator'
    trader_id: Optional[str] = None


# ── 事件日誌 ──────────────────────────────────────────────────────────

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _log_event(code: str, event: str, payload: dict):
    """每筆事件同步 append 到 data/{code}.jsonl（記憶體會因重啟清空，日誌不會）。"""
    _ensure_data_dir()
    rec = {"ts": _now(), "ts_iso": _iso(_now()), "event": event, **payload}
    path = os.path.join(DATA_DIR, f"{code}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 房間代碼 / 代號 ───────────────────────────────────────────────────

def _gen_room_code() -> str:
    pool = [c for c in (string.ascii_uppercase + string.digits) if c not in CONFUSING]
    while True:
        code = "".join(random.choices(pool, k=6))
        if code not in MARKETS:
            return code


def _gen_badge(market: Market) -> str:
    used = {t.badge for t in market.traders.values()}
    while True:
        b = "".join(random.choices(BADGE_CHARS, k=3))
        if b not in used:
            return b


# ── 撮合輔助 ──────────────────────────────────────────────────────────

def _live_bids(market: Market):
    return [o for o in market.book if o.status == "live" and o.side == "bid"]


def _live_offers(market: Market):
    return [o for o in market.book if o.status == "live" and o.side == "offer"]


def _best_bid(market: Market) -> Optional[Order]:
    bids = _live_bids(market)
    if not bids:
        return None
    # 價高者優先，同價時間優先（ts 早者）
    return sorted(bids, key=lambda o: (-o.price, o.ts))[0]


def _best_offer(market: Market) -> Optional[Order]:
    offers = _live_offers(market)
    if not offers:
        return None
    return sorted(offers, key=lambda o: (o.price, o.ts))[0]


def _is_visible(market: Market, viewer_id: Optional[str], order: Order) -> bool:
    """喊價模式：只看得到自己的報價 + 隨機子集（每 resample_seconds 重抽）。"""
    if market.mode == "electronic":
        return True
    if order.trader_id == viewer_id:
        return True
    if viewer_id is None:      # 教師端看全部
        return True
    bucket = int(_now() // market.resample_seconds)
    h = hashlib.sha256(f"{viewer_id}:{order.id}:{bucket}".encode()).digest()
    val = int.from_bytes(h[:4], "big") / 2**32
    return val < market.visible_fraction


def _positions(market: Market):
    """由成交紀錄計算每位交易者的部位與現金流。"""
    pos = {tid: {"net": 0, "cash": 0.0, "buys": 0, "sells": 0,
                 "n_trades": 0, "rounds": set()} for tid in market.traders}
    for tr in market.trades:
        for tid, sign in ((tr.buyer_id, +1), (tr.seller_id, -1)):
            if tid not in pos:
                continue
            p = pos[tid]
            p["net"] += sign
            p["cash"] += -sign * tr.price       # 買進付出、賣出收入
            p["n_trades"] += 1
            p["rounds"].add(tr.round)
            if sign > 0:
                p["buys"] += 1
            else:
                p["sells"] += 1
    return pos


def _total_pnl(market: Market, p: dict) -> float:
    dp = market.delivery_price
    if dp is None:
        return p["cash"]
    return p["cash"] + p["net"] * dp


# ── 視角化狀態 ────────────────────────────────────────────────────────

def _order_view(o: Order, market: Market, show_owner: bool):
    v = {"id": o.id, "side": o.side, "price": o.price, "round": o.round, "ts": o.ts}
    if show_owner:
        t = market.traders.get(o.trader_id)
        v["badge"] = t.badge if t else "?"
    return v


def _trade_view(tr: Trade, market: Market, viewer_id: Optional[str], show_counterparty: bool):
    v = {"id": tr.id, "round": tr.round, "price": tr.price, "ts": tr.ts}
    # 標記自己是買方或賣方
    if viewer_id == tr.buyer_id:
        v["role"] = "buy"
    elif viewer_id == tr.seller_id:
        v["role"] = "sell"
    else:
        v["role"] = None
    if show_counterparty:
        bt = market.traders.get(tr.buyer_id)
        st = market.traders.get(tr.seller_id)
        v["buyer_badge"] = bt.badge if bt else "?"
        v["seller_badge"] = st.badge if st else "?"
    return v


def _revealed_slots(market: Market):
    return [market.revealed[i] if i < len(market.revealed) else None
            for i in range(TOTAL_ROUNDS)]


def build_state(market: Market, role: str, trader_id: Optional[str]) -> dict:
    is_host = role == "host"
    bb = _best_bid(market)
    bo = _best_offer(market)

    base = {
        "type": "state",
        "code": market.code,
        "mode": market.mode,
        "phase": market.phase,
        "round": market.round,
        "total_rounds": TOTAL_ROUNDS,
        "round_ends_at": market.round_ends_at,
        "server_time": _now(),
        "revealed": _revealed_slots(market),
        "revealed_count": len(market.revealed),
        "delivery_price": market.delivery_price,
        "best_bid": bb.price if bb else None,
        "best_offer": bo.price if bo else None,
        "best_bid_id": bb.id if bb else None,
        "best_offer_id": bo.id if bo else None,
        "trader_count": len(market.traders),
        "config": {
            "round_seconds": market.round_seconds,
            "visible_fraction": market.visible_fraction,
            "resample_seconds": market.resample_seconds,
        },
    }

    pos = _positions(market)

    if is_host:
        # 教師端：完整委託簿 + 全體部位 + 全部成交（含對手身分）
        base["role"] = "host"
        base["host_key"] = market.host_key
        base["book"] = [_order_view(o, market, True)
                        for o in market.book if o.status == "live"]
        base["trades"] = [_trade_view(tr, market, None, True) for tr in market.trades]
        traders_view = []
        for tid, t in market.traders.items():
            p = pos.get(tid, {"net": 0, "cash": 0.0, "n_trades": 0})
            traders_view.append({
                "id": tid, "name": t.name, "badge": t.badge, "color": t.color,
                "net": p["net"], "cash": round(p["cash"], 2),
                "n_trades": p["n_trades"], "pnl": round(_total_pnl(market, p), 2),
            })
        base["traders"] = sorted(traders_view, key=lambda x: x["badge"])
        base["review"] = _build_review(market) if market.phase == "settled" else None
        return base

    # 學生端
    base["role"] = "trader"
    show_owner = market.mode == "outcry"
    visible = [o for o in market.book
               if o.status == "live" and _is_visible(market, trader_id, o)]
    base["book"] = [_order_view(o, market, show_owner) for o in visible]
    base["trades"] = [_trade_view(tr, market, trader_id, market.mode == "outcry")
                      for tr in market.trades]

    t = market.traders.get(trader_id)
    if t:
        p = pos.get(trader_id, {"net": 0, "cash": 0.0, "n_trades": 0})
        base["me"] = {
            "id": t.id, "name": t.name, "badge": t.badge, "color": t.color,
            "net": p["net"], "cash": round(p["cash"], 2),
            "n_trades": p["n_trades"], "pnl": round(_total_pnl(market, p), 2),
        }
        # 自己的 live 報價（可撤單）
        base["my_orders"] = [
            _order_view(o, market, True) for o in market.book
            if o.status == "live" and o.trader_id == trader_id
        ]
    return base


def _build_review(market: Market) -> dict:
    """結算後的檢討統計：每輪成交量／未平倉／結算價／揭露數字／輪初預期交割價。"""
    rounds = []
    prev_settle = None
    for r in range(1, TOTAL_ROUNDS + 1):
        rt = [tr for tr in market.trades if tr.round == r]
        volume = len(rt)
        settle = rt[-1].price if rt else prev_settle
        # 每輪未平倉量：該輪結束時各交易者淨部位取正值後加總
        net = {}
        for tr in market.trades:
            if tr.round <= r:
                net[tr.buyer_id] = net.get(tr.buyer_id, 0) + 1
                net[tr.seller_id] = net.get(tr.seller_id, 0) - 1
        open_interest = sum(v for v in net.values() if v > 0)
        # 第 k 輪輪初預期交割價 =（前 k-1 位已揭露之和）+ (5-(k-1)) * 4.5
        prior = sum(market.revealed[:r - 1]) if len(market.revealed) >= r - 1 else 0
        expected = prior + (TOTAL_ROUNDS - (r - 1)) * DIGIT_EXPECTED
        revealed_digit = market.revealed[r - 1] if len(market.revealed) >= r else None
        rounds.append({
            "round": r, "volume": volume,
            "settle_price": settle, "open_interest": open_interest,
            "revealed_digit": revealed_digit, "expected_delivery": expected,
        })
        prev_settle = settle
    return {
        "delivery_price": market.delivery_price,
        "secret_digits": market.secret_digits,   # 結算後才給
        "rounds": rounds,
    }


# ── 廣播 ──────────────────────────────────────────────────────────────

async def broadcast(code: str):
    market = MARKETS.get(code)
    if not market:
        return
    dead = []
    for conn in CONNS.get(code, []):
        try:
            await conn.ws.send_json(build_state(market, conn.role, conn.trader_id))
        except Exception:
            dead.append(conn)
    for d in dead:
        _remove_conn(code, d)


async def _send(conn: Conn, msg: dict):
    try:
        await conn.ws.send_json(msg)
    except Exception:
        pass


def _remove_conn(code: str, conn: Conn):
    lst = CONNS.get(code, [])
    if conn in lst:
        lst.remove(conn)


# ── 回合流程 ──────────────────────────────────────────────────────────

def _clear_book(market: Market):
    ts = _now()
    for o in market.book:
        if o.status == "live":
            o.status = "expired"
            o.ended_at = ts
    market.book = []


async def _open_round(market: Market):
    if market.phase not in ("lobby", "closed") or market.round >= TOTAL_ROUNDS:
        return
    market.round += 1
    market.phase = "open"
    market.round_ends_at = _now() + market.round_seconds
    market.book = []
    _log_event(market.code, "open_round",
               {"round": market.round, "round_ends_at": market.round_ends_at})


async def _close_round(market: Market):
    if market.phase != "open":
        return
    r = market.round
    market.phase = "closed"
    market.round_ends_at = None
    # 揭露第 r 位數字
    digit = market.secret_digits[r - 1]
    market.revealed.append(digit)
    _clear_book(market)
    _log_event(market.code, "close_round", {"round": r})
    _log_event(market.code, "reveal", {"index": r - 1, "digit": digit})
    # 對前端送出 reveal 動畫訊息
    for conn in CONNS.get(market.code, []):
        await _send(conn, {"type": "reveal", "index": r - 1, "digit": digit})
    if r >= TOTAL_ROUNDS:
        market.phase = "settled"
        _log_event(market.code, "settle",
                   {"delivery_price": sum(market.secret_digits),
                    "secret_digits": market.secret_digits})


# ── 撮合：接受既有報價 ────────────────────────────────────────────────

def _record_trade(market: Market, order: Order, taker_id: str) -> Trade:
    bb = _best_bid(market)
    bo = _best_offer(market)
    best_bid = bb.price if bb else None
    best_offer = bo.price if bo else None
    if order.side == "bid":
        # 被接受的是買價 → 掛單方買、接受方賣
        buyer_id, seller_id = order.trader_id, taker_id
        ref = best_bid                       # 同向最佳 = 最佳買價
        slip = (best_bid - order.price) if best_bid is not None else 0
    else:
        buyer_id, seller_id = taker_id, order.trader_id
        ref = best_offer
        slip = (order.price - best_offer) if best_offer is not None else 0
    slip = max(0, int(slip))

    order.status = "filled"
    order.ended_at = _now()

    tr = Trade(
        id=secrets.token_urlsafe(6), round=market.round, price=order.price,
        buyer_id=buyer_id, seller_id=seller_id,
        maker_id=order.trader_id, taker_id=taker_id, ts=_now(),
        best_bid=best_bid, best_offer=best_offer, slippage=slip,
    )
    market.trades.append(tr)
    _log_event(market.code, "trade", {
        "trade_id": tr.id, "round": tr.round, "price": tr.price,
        "buyer_id": buyer_id, "seller_id": seller_id,
        "maker_id": tr.maker_id, "taker_id": taker_id,
        "best_bid": best_bid, "best_offer": best_offer, "slippage": slip,
    })
    return tr


# ── WebSocket 訊息處理 ────────────────────────────────────────────────

async def _handle_join(market: Market, conn: Conn, msg: dict):
    name = (msg.get("name") or "").strip()
    if not name:
        await _send(conn, {"type": "error", "message": "請輸入姓名或學號"})
        return
    # 同名重連：恢復原部位（同房間內姓名唯一）
    existing = next((t for t in market.traders.values() if t.name == name), None)
    if existing:
        tid = existing.id
    else:
        tid = secrets.token_urlsafe(8)
        market.traders[tid] = Trader(
            id=tid, name=name, badge=_gen_badge(market),
            color=COLORS[len(market.traders) % len(COLORS)],
            joined_at=_now(), joined_round=market.round,
        )
        _log_event(market.code, "join",
                   {"trader_id": tid, "name": name,
                    "badge": market.traders[tid].badge, "round": market.round})
    conn.role = "trader"
    conn.trader_id = tid
    await _send(conn, {"type": "joined", "trader_id": tid,
                       "badge": market.traders[tid].badge})


async def _handle_host(market: Market, conn: Conn, msg: dict):
    if msg.get("host_key") != market.host_key:
        await _send(conn, {"type": "error", "message": "主持碼錯誤"})
        return
    conn.role = "host"
    conn.trader_id = None
    await _send(conn, {"type": "hosted"})


async def _handle_quote(market: Market, conn: Conn, msg: dict):
    if conn.role != "trader" or not conn.trader_id:
        return
    if market.phase != "open":
        await _send(conn, {"type": "error", "message": "目前不在交易時間"})
        return
    side = msg.get("side")
    if side not in ("bid", "offer"):
        return
    try:
        price = int(msg.get("price"))
    except (TypeError, ValueError):
        await _send(conn, {"type": "error", "message": "價格必須是整數"})
        return
    if not (0 <= price <= 99):
        await _send(conn, {"type": "error", "message": "價格需在 0–99 之間"})
        return
    o = Order(id=secrets.token_urlsafe(6), trader_id=conn.trader_id,
              side=side, price=price, round=market.round, ts=_now())
    market.book.append(o)
    market.all_orders.append(o)
    _log_event(market.code, "quote",
               {"order_id": o.id, "trader_id": o.trader_id,
                "side": side, "price": price, "round": market.round})


async def _handle_withdraw(market: Market, conn: Conn, msg: dict):
    if conn.role != "trader":
        return
    oid = msg.get("order_id")
    o = next((x for x in market.book if x.id == oid and x.status == "live"), None)
    if not o:
        return
    if o.trader_id != conn.trader_id:
        await _send(conn, {"type": "error", "message": "只能撤自己的報價"})
        return
    o.status = "withdrawn"
    o.ended_at = _now()
    market.book.remove(o)
    _log_event(market.code, "withdraw", {"order_id": oid, "trader_id": conn.trader_id})


async def _handle_take(market: Market, conn: Conn, msg: dict):
    if conn.role != "trader" or not conn.trader_id:
        return
    if market.phase != "open":
        await _send(conn, {"type": "error", "message": "目前不在交易時間"})
        return
    oid = msg.get("order_id")
    o = next((x for x in market.book if x.id == oid and x.status == "live"), None)
    if not o:
        await _send(conn, {"type": "error", "message": "那張報價已經不在了"})
        return
    if o.trader_id == conn.trader_id:
        await _send(conn, {"type": "error", "message": "不可與自己的報價成交"})
        return

    if market.mode == "electronic":
        # 嚴格價格優先：只有最佳同向報價可被成交
        best = _best_bid(market) if o.side == "bid" else _best_offer(market)
        if not best or best.price != o.price:
            await _send(conn, {"type": "error", "message": "只能成交最佳報價"})
            return
        # 同價時間優先：以伺服器判定的最佳（最早）為準
        o = best
    else:
        # 喊價模式：只能成交自己看得見的報價
        if not _is_visible(market, conn.trader_id, o):
            await _send(conn, {"type": "error", "message": "那張報價已經不在了"})
            return

    _record_trade(market, o, conn.trader_id)
    if o in market.book:
        market.book.remove(o)


async def _handle_config(market: Market, conn: Conn, msg: dict):
    if conn.role != "host":
        return
    if market.phase != "lobby":
        await _send(conn, {"type": "error", "message": "只能在大廳階段修改設定"})
        return
    if "round_seconds" in msg:
        try:
            rs = int(msg["round_seconds"])
            market.round_seconds = max(30, min(600, rs))
        except (TypeError, ValueError):
            pass
    if msg.get("mode") in ("electronic", "outcry"):
        market.mode = msg["mode"]
    if "visible_fraction" in msg:
        try:
            market.visible_fraction = max(0.1, min(1.0, float(msg["visible_fraction"])))
        except (TypeError, ValueError):
            pass
    if "resample_seconds" in msg:
        try:
            market.resample_seconds = max(1, min(30, int(msg["resample_seconds"])))
        except (TypeError, ValueError):
            pass
    _log_event(market.code, "set_config",
               {"round_seconds": market.round_seconds, "mode": market.mode,
                "visible_fraction": market.visible_fraction})


HANDLERS = {
    "join": _handle_join,
    "host": _handle_host,
    "quote": _handle_quote,
    "withdraw": _handle_withdraw,
    "take": _handle_take,
    "set_config": _handle_config,
}


@router.websocket("/ws/{room_code}")
async def ws_endpoint(websocket: WebSocket, room_code: str):
    await websocket.accept()
    market = MARKETS.get(room_code)
    if not market:
        await websocket.send_json({"type": "error", "message": "找不到房間"})
        await websocket.close()
        return
    conn = Conn(ws=websocket)
    CONNS.setdefault(room_code, []).append(conn)
    try:
        # 先送一次目前狀態
        await websocket.send_json(build_state(market, conn.role, conn.trader_id))
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")

            if mtype == "open_round" and conn.role == "host":
                async with market.lock:
                    await _open_round(market)
                await broadcast(room_code)
                continue
            if mtype == "close_round" and conn.role == "host":
                async with market.lock:
                    await _close_round(market)
                await broadcast(room_code)
                continue

            handler = HANDLERS.get(mtype)
            if not handler:
                continue
            async with market.lock:
                await handler(market, conn, msg)
            await broadcast(room_code)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _remove_conn(room_code, conn)


# ── 背景計時器：倒數歸零由伺服器自動收盤 ──────────────────────────────

async def _ticker():
    while True:
        await asyncio.sleep(1)
        now = _now()
        for code, market in list(MARKETS.items()):
            try:
                if market.phase == "open" and market.round_ends_at and now >= market.round_ends_at:
                    async with market.lock:
                        await _close_round(market)
                    await broadcast(code)
                elif market.mode == "outcry" and market.phase == "open":
                    # 喊價模式每 resample_seconds 重抽可見子集 → 週期性重推
                    if int(now) % market.resample_seconds == 0:
                        await broadcast(code)
            except Exception:
                pass


def start_ticker():
    _ensure_data_dir()
    asyncio.create_task(_ticker())


# ── HTTP：建立房間 / 頁面 ─────────────────────────────────────────────

class ZipRoomSettings(BaseModel):
    mode: str = "electronic"           # 'electronic' | 'outcry'
    round_seconds: int = 180
    visible_fraction: float = 0.4
    resample_seconds: int = 3
    orig_margin: float = 10.0
    maint_margin: float = 8.0


@router.post("/api/zip/room")
def create_room(settings: ZipRoomSettings):
    code = _gen_room_code()
    host_key = "".join(random.choices(BADGE_CHARS, k=4))
    market = Market(
        code=code, host_key=host_key,
        mode=settings.mode if settings.mode in ("electronic", "outcry") else "electronic",
        created_at=_now(),
        round_seconds=max(30, min(600, settings.round_seconds)),
        visible_fraction=max(0.1, min(1.0, settings.visible_fraction)),
        resample_seconds=max(1, min(30, settings.resample_seconds)),
        orig_margin=settings.orig_margin, maint_margin=settings.maint_margin,
        secret_digits=[random.randint(0, 9) for _ in range(TOTAL_ROUNDS)],
    )
    MARKETS[code] = market
    _log_event(code, "create_room",
               {"mode": market.mode, "round_seconds": market.round_seconds})
    return {"code": code, "host_key": host_key}


@router.get("/zip")
def zip_page():
    return FileResponse("static/zip.html")


@router.get("/teacher-zip")
def teacher_zip_page():
    return FileResponse("static/teacher-zip.html")


@router.get("/zip/health")
def zip_health():
    return {"status": "ok", "rooms": len(MARKETS)}


# ── CSV 匯出 ──────────────────────────────────────────────────────────

def _csv_response(rows: list, filename: str) -> Response:
    out = "\ufeff"                    # UTF-8 BOM（Excel 相容）
    for row in rows:
        out += ",".join(_csv_cell(c) for c in row) + "\r\n"
    return Response(
        content=out.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_cell(v) -> str:
    if v is None:
        return ""
    s = str(v)
    if any(ch in s for ch in [",", '"', "\n", "\r"]):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _export_rows(market: Market, kind: str):
    name_of = {tid: t.name for tid, t in market.traders.items()}
    badge_of = {tid: t.badge for tid, t in market.traders.items()}

    if kind == "trades":
        header = ["room", "mode", "round", "ts_iso", "price", "buyer_name",
                  "seller_name", "maker_badge", "taker_badge", "best_bid_at_trade",
                  "best_offer_at_trade", "execution_slippage"]
        rows = [header]
        for tr in market.trades:
            rows.append([
                market.code, market.mode, tr.round, _iso(tr.ts), tr.price,
                name_of.get(tr.buyer_id, "?"), name_of.get(tr.seller_id, "?"),
                badge_of.get(tr.maker_id, "?"), badge_of.get(tr.taker_id, "?"),
                tr.best_bid, tr.best_offer, tr.slippage,
            ])
        return rows

    if kind == "orders":
        header = ["room", "mode", "round", "ts_iso", "order_id", "trader_name",
                  "side", "price", "status", "lifetime_seconds"]
        rows = [header]
        for o in market.all_orders:
            life = round((o.ended_at - o.ts), 2) if o.ended_at else ""
            rows.append([
                market.code, market.mode, o.round, _iso(o.ts), o.id,
                name_of.get(o.trader_id, "?"), o.side, o.price, o.status, life,
            ])
        return rows

    if kind == "summary":
        header = ["room", "mode", "trader_name", "badge", "n_trades", "n_buys",
                  "n_sells", "net_position", "cash_flow", "delivery_price",
                  "total_pnl", "rounds_active"]
        rows = [header]
        pos = _positions(market)
        dp = market.delivery_price
        for tid, t in market.traders.items():
            p = pos.get(tid, {"net": 0, "cash": 0.0, "buys": 0, "sells": 0,
                              "n_trades": 0, "rounds": set()})
            rows.append([
                market.code, market.mode, t.name, t.badge, p["n_trades"],
                p["buys"], p["sells"], p["net"], round(p["cash"], 2),
                dp if dp is not None else "", round(_total_pnl(market, p), 2),
                len(p["rounds"]),
            ])
        return rows

    raise HTTPException(status_code=400, detail="kind 必須是 trades|orders|summary")


@router.get("/api/zip/room/{code}/export.csv")
def export_csv(code: str, kind: str = "trades", host_key: str = ""):
    market = MARKETS.get(code)
    if not market:
        raise HTTPException(status_code=404, detail="找不到房間")
    if host_key != market.host_key:
        raise HTTPException(status_code=403, detail="主持碼錯誤")
    rows = _export_rows(market, kind)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _csv_response(rows, f"zip-{code}-{kind}-{stamp}.csv")


# ── Admin ─────────────────────────────────────────────────────────────

def _check_admin(token: str):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="伺服器未設定 ADMIN_TOKEN")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="授權失敗")


@router.get("/admin/rooms")
def admin_rooms(token: str = ""):
    _check_admin(token)
    return [{
        "code": m.code, "mode": m.mode, "phase": m.phase, "round": m.round,
        "traders": len(m.traders), "trades": len(m.trades),
        "created_at": _iso(m.created_at),
    } for m in MARKETS.values()]


@router.get("/admin/export")
def admin_export(room: str, kind: str = "trades", token: str = ""):
    _check_admin(token)
    market = MARKETS.get(room)
    if not market:
        # 記憶體沒有 → 嘗試從事件日誌重建摘要（僅回傳原始 jsonl）
        path = os.path.join(DATA_DIR, f"{room}.jsonl")
        if os.path.exists(path):
            return FileResponse(path, media_type="application/x-ndjson",
                                filename=f"{room}.jsonl")
        raise HTTPException(status_code=404, detail="找不到房間")
    rows = _export_rows(market, kind)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _csv_response(rows, f"zip-{room}-{kind}-{stamp}.csv")
