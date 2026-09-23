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
import re
import secrets
import string
import time
import unicodedata
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
NEUTRAL_PRICE = DIGIT_EXPECTED * TOTAL_ROUNDS   # 無資訊時的中性預期交割價 = 22.5
MAX_DELIVERY = 9 * TOTAL_ROUNDS                  # 交割價上限 = 45
# 報價上限。交割價最高只可能到 45，上面刻意留一小段空間，讓「被支配的報價」
# 還掛得出來 —— 有人掛買價 48 時，賣給他就是無風險套利。那是講無套利界限最好
# 的現場教材，硬卡在 45 就沒有這個機會；原本的 99 則跟任何量都沒有關係。
MAX_QUOTE = MAX_DELIVERY + 5                     # = 50
CONFUSING = set("O0I1l")      # 房間代碼避開的易混淆字元
BADGE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COLORS = ["#7c6af7", "#4ecb8a", "#f0b429", "#4a9eff", "#ff7eb6", "#42d4f4",
          "#a594ff", "#ff8a5c", "#5cd6c0", "#d68cff", "#ffd15c", "#8ce85c"]


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()


# ── 重連認人 ──────────────────────────────────────────────────────────

# 中文字之間的空白（「王 小明」＝「王小明」）。不無差別刪除所有空白：
# 那會把 "Li Na" 和 "Lin A" 併成同一個人，撞在一起比認不出來更糟。
_CJK = "\u3400-\u9fff\uf900-\ufaff"          # 中日韓統一表意文字
CJK_GAP = re.compile("(?<=[" + _CJK + "])" + r"\s+" + "(?=[" + _CJK + "])")


def _norm_name(name: str) -> str:
    """重連認人用的正規化鍵。

    學號打成 s1234 / S1234、中間多打一個空格、全形英數字，都應該算同一個人
    —— 否則學生只是換個打法就變成全新交易者。
    NFKC 把全形轉半形，casefold 吃掉大小寫，空白壓成單一空格，中文字之間的空白刪掉。
    """
    s = unicodedata.normalize("NFKC", name or "")
    s = " ".join(s.split())
    s = CJK_GAP.sub("", s)
    return s.casefold()


def _neutral_price(revealed: list) -> float:
    """中性預期交割價：已揭露數字之和 + 未揭露位數 × 4.5（論文習題 4 的定義）。"""
    return sum(revealed) + (TOTAL_ROUNDS - len(revealed)) * DIGIT_EXPECTED


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
    key: str            # 正規化後的比對鍵（重連認人用，不外送）
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
    # 保證金只是檢討面板（習題 3）的參數：遊戲進行中不結算、不限制下單，
    # 跟論文的設計一致。結算後教師還可以改，逐日結算表會跟著重算。
    orig_margin: float = 10.0
    maint_margin: float = 8.0
    # 學生端是否顯示「最新成交價相對中性預期偏離多少」。
    # 預設關閉：那個數字等於把答案的一半告訴學生，要不要給由教師決定。
    show_deviation: bool = False
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


def _matchable(market: Market, trader_id: str, side: str, price: int) -> Optional[Order]:
    """新委託進來時，找出可以立刻成交的最佳對手單（可立即成交的限價單）。

    真實交易所的做法：掛出的買價若高於（或等於）場上最佳賣價，立刻成交，而且
    成交價用**簿子上那張**的價格，不是新單開的價格。先掛者享有價格優先，後到
    的人得到比自己開價更好的成交。少了這一步，委託簿會停在買價高於賣價的
    「交叉」狀態，那張掛錯的單就等著被別人撿走。

    只套用在電子撮合。喊價模式維持原本的行為：學生只看得到部分報價，自動去
    配對一張他可能根本沒看到的委託並不合理，所以那邊還是要自己按下成交。
    另外不跟自己的委託成交，會跳過自己的單去找下一個最佳。
    """
    if market.mode != "electronic":
        return None
    if side == "bid":
        cands = sorted((o for o in _live_offers(market) if o.price <= price),
                       key=lambda o: (o.price, o.ts))
    else:
        cands = sorted((o for o in _live_bids(market) if o.price >= price),
                       key=lambda o: (-o.price, o.ts))
    for o in cands:
        if o.trader_id == trader_id:
            continue
        if not _is_visible(market, trader_id, o):
            continue
        return o
    return None


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
    """由成交紀錄計算每位交易者的淨部位與成交統計。"""
    pos = {tid: {"net": 0, "buys": 0, "sells": 0,
                 "n_trades": 0, "rounds": set()} for tid in market.traders}
    for tr in market.trades:
        for tid, sign in ((tr.buyer_id, +1), (tr.seller_id, -1)):
            if tid not in pos:
                continue
            p = pos[tid]
            p["net"] += sign
            p["n_trades"] += 1
            p["rounds"].add(tr.round)
            if sign > 0:
                p["buys"] += 1
            else:
                p["sells"] += 1
    return pos


def _settle_series(market: Market) -> list:
    """每一輪的結算價 = 該輪最後一筆成交價。

    沒有成交就沿用前一輪，第一輪也沒有就用中性價 22.5 —— 真實交易所在無成交時
    也會給理論結算價，否則逐日結算表會在冷清的那一輪斷掉。
    """
    out, prev = [], float(NEUTRAL_PRICE)
    for r in range(1, TOTAL_ROUNDS + 1):
        rt = [tr for tr in market.trades if tr.round == r]
        s = float(rt[-1].price) if rt else prev
        out.append(s)
        prev = s
    return out


def _ledger(market: Market) -> dict:
    """每位交易者的逐日結算表（習題 3）。

    跟論文一樣是「事後」的分析：遊戲進行中不收保證金、不追繳、不擋單，
    這裡只是用班上交易出來的結算價，示範保證金帳戶會怎麼走。

    每一輪收盤：
      1. 昨日留下來的部位，按今昨結算價差重評價
      2. 今日新成交，按成交價到今日結算價重評價；部位變動時存入／退回保證金
      3. 餘額低於 口數×維持保證金 → 追繳回 口數×原始保證金
    交割日再按交割價結算一次，然後退回全部保證金。

    累計損益最後一定等於 Σ 方向×(交割價 − 成交價)，也就是論文的結算損益。
    """
    orig, maint = market.orig_margin, market.maint_margin
    n_closed = len(market.revealed)          # 已收盤的輪數
    D = market.delivery_price                # 結算前為 None
    series = _settle_series(market)

    out = {}
    for tid in market.traders:
        trades = [tr for tr in market.trades
                  if tr.buyer_id == tid or tr.seller_id == tid]
        pos, prev_s, balance, cum = 0, None, 0.0, 0.0
        rows, calls, call_total = [], 0, 0.0

        def _mark(label, s, rt, final=False):
            nonlocal pos, prev_s, balance, cum, calls, call_total
            daily = 0.0
            if prev_s is not None:
                daily += pos * (s - prev_s)          # 1) 舊部位重評價
            before = abs(pos)
            for tr in rt:                            # 2) 今日新成交
                d = 1 if tr.buyer_id == tid else -1
                daily += d * (s - tr.price)
                pos += d
            after = abs(pos)
            deposit = (after - before) * orig
            balance += deposit + daily
            cum += daily
            pre_call = balance
            call = 0.0
            # 3) 追繳。交割日只做最後一次結算、整個帳戶退回，不會再追繳
            if not final and after > 0 and balance < after * maint:
                call = after * orig - balance
                balance += call
                calls += 1
                call_total += call
            rows.append({
                "label": label, "settle": round(s, 2), "trades": len(rt),
                "pos": pos, "deposit": round(deposit, 2),
                "daily": round(daily, 2), "cum": round(cum, 2),
                # 論文 Table 5 記的是追繳前的餘額，追繳金額另列一欄
                "pre_call": round(pre_call, 2),
                "call": round(call, 2),
                "balance": round(balance, 2),
                "maintenance": 0.0 if final else round(after * maint, 2),
            })
            prev_s = s

        for r in range(1, n_closed + 1):
            _mark(f"R{r}", series[r - 1], [tr for tr in trades if tr.round == r])
        if D is not None:
            # 交割日：以交割價做最後一次結算，帳戶餘額（含原始保證金）全數退回。
            # 餘額照論文 Table 5 顯示退回前的金額，例如 10 + 7 = 17。
            _mark("Delivery", float(D), [], final=True)
            rows[-1]["returned"] = round(balance, 2)

        out[tid] = {"rows": rows, "pnl": round(cum, 2),
                    "calls": calls, "call_total": round(call_total, 2)}
    return out


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


def build_board_state(market: Market) -> dict:
    """投影用的公開看板。

    刻意不含任何個人資訊：沒有姓名、代號、淨部位、損益排行榜。
    淨部位會洩漏誰做多誰做空、破壞匿名；即時損益排行榜會誘發末輪梭哈。
    """
    bb, bo = _best_bid(market), _best_offer(market)
    net = {}
    for tr in market.trades:
        net[tr.buyer_id] = net.get(tr.buyer_id, 0) + 1
        net[tr.seller_id] = net.get(tr.seller_id, 0) - 1
    return {
        "type": "state",
        "role": "board",
        "code": market.code,
        "mode": market.mode,
        "phase": market.phase,
        "round": market.round,
        "total_rounds": TOTAL_ROUNDS,
        "round_ends_at": market.round_ends_at,
        "server_time": _now(),
        "revealed": _revealed_slots(market),
        "neutral_price": NEUTRAL_PRICE,
        "trader_count": len(market.traders),
        "best_bid": bb.price if bb else None,
        "best_offer": bo.price if bo else None,
        "spread": (bo.price - bb.price) if (bb and bo) else None,
        "n_bids": len(_live_bids(market)),
        "n_offers": len(_live_offers(market)),
        "volume": len(market.trades),
        "open_interest": sum(v for v in net.values() if v > 0),
        "last": market.trades[-1].price if market.trades else None,
        # 成交序列只有輪次、價格、時間，沒有任何身分
        "ticks": [{"round": tr.round, "price": tr.price, "ts": tr.ts}
                  for tr in market.trades],
        "delivery_price": market.delivery_price,   # 結算前恆為 None
    }


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
        "neutral_price": NEUTRAL_PRICE,
        "max_delivery": MAX_DELIVERY,
        "max_quote": MAX_QUOTE,
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
            "orig_margin": market.orig_margin,
            "maint_margin": market.maint_margin,
            "show_deviation": market.show_deviation,
        },
    }

    pos = _positions(market)
    led = _ledger(market)

    if is_host:
        # 教師端：完整委託簿 + 全體部位 + 全部成交（含對手身分）
        base["role"] = "host"
        base["host_key"] = market.host_key
        base["book"] = [_order_view(o, market, True)
                        for o in market.book if o.status == "live"]
        base["trades"] = [_trade_view(tr, market, None, True) for tr in market.trades]
        traders_view = []
        for tid, t in market.traders.items():
            p = pos.get(tid, {"net": 0, "n_trades": 0})
            traders_view.append({
                "id": tid, "name": t.name, "badge": t.badge, "color": t.color,
                "net": p["net"], "n_trades": p["n_trades"],
                # 已結算損益：算到最後一次收盤為止，結算後即為最終損益
                "pnl": led.get(tid, {}).get("pnl", 0.0),
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
        p = pos.get(trader_id, {"net": 0, "n_trades": 0})
        base["me"] = {
            "id": t.id, "name": t.name, "badge": t.badge, "color": t.color,
            "net": p["net"], "n_trades": p["n_trades"],
            "pnl": led.get(trader_id, {}).get("pnl", 0.0),
        }
        # 自己的 live 報價（可撤單）
        base["my_orders"] = [
            _order_view(o, market, True) for o in market.book
            if o.status == "live" and o.trader_id == trader_id
        ]
    return base


def _round_prices(market: Market) -> list:
    """每一輪的成交均價（VWAP）與筆數。沒有成交的輪次為 None。"""
    out = []
    for r in range(1, TOTAL_ROUNDS + 1):
        rt = [tr for tr in market.trades if tr.round == r]
        out.append({"round": r, "n": len(rt),
                    "vwap": (sum(t.price for t in rt) / len(rt)) if rt else None})
    return out


def _efficiency(market: Market) -> dict:
    """習題 6：市場效率檢驗。

    論文的判準：揭露的數字大於 4.5 是好消息、小於 4.5 是壞消息，再看成交價
    有沒有隨之上下。這裡做成三層，從最好講的到最精確的：

    1. 方向一致率 —— 第 r 輪揭露之後，第 r+1 輪的均價有沒有往對的方向走。
    2. 反應係數 β —— 價格變動對「應有變動 d−4.5」的過原點迴歸斜率。
       β≈1 完全反應、β<1 反應不足、β>1 過度反應。
    3. 內線可得利潤 —— 事後知道交割價的人在每筆成交上能賺 |交割價 − 成交價|，
       對應習題 6「如果有內線，他會怎麼交易」。

    第 5 位揭露後就結算、沒有後續交易，所以事件最多只有 4 個。β 是課堂用的
    描述統計，不是有檢定力的迴歸。
    """
    px = _round_prices(market)
    dig = market.revealed
    D = market.delivery_price
    neutral = [_neutral_price(dig[:k]) for k in range(len(dig) + 1)]

    events = []
    for r in range(1, TOTAL_ROUNDS):          # 第 r 輪揭露 → 第 r+1 輪反應
        if r > len(dig):
            break
        d = dig[r - 1]
        before, after = px[r - 1]["vwap"], px[r]["vwap"]
        resp = (after - before) if (before is not None and after is not None) else None
        delta = d - DIGIT_EXPECTED                    # 這則消息「應該」讓價格動多少
        # 同向 = 價格確實往消息的方向動了。紋風不動（resp == 0）算沒有反映資訊。
        agree = None if resp is None else (
            (resp > 0 and delta > 0) or (resp < 0 and delta < 0))
        events.append({
            "round": r, "digit": d,
            "news": "good" if delta > 0 else "bad",
            "delta": delta,
            "vwap_before": round(before, 2) if before is not None else None,
            "vwap_after": round(after, 2) if after is not None else None,
            "response": round(resp, 2) if resp is not None else None,
            "n_before": px[r - 1]["n"], "n_after": px[r]["n"],
            "agree": agree,
        })

    usable = [e for e in events if e["agree"] is not None]
    tally = None
    if usable:
        k = sum(1 for e in usable if e["agree"])
        num = sum(e["delta"] * e["response"] for e in usable)
        den = sum(e["delta"] ** 2 for e in usable)
        tally = {"n": len(usable), "k": k, "rate": round(k / len(usable), 3),
                 "beta": round(num / den, 3) if den else None}

    # 定價誤差：各輪均價離該輪輪初中性預期多遠。
    # 同時記錄「誰是主動方」：成交是買方去接賣價，還是賣方去接買價。
    # 如果價格偏低的輪次剛好都是賣方主動，就支持「賣方比買方積極」這個解釋。
    errs, signed, rounds_detail = [], [], []
    for r in range(1, TOTAL_ROUNDS + 1):
        v = px[r - 1]["vwap"]
        rt = [tr for tr in market.trades if tr.round == r]
        taker_buy = sum(1 for tr in rt if tr.taker_id == tr.buyer_id)
        dev = None
        if v is not None and r - 1 < len(neutral):
            dev = v - neutral[r - 1]
            errs.append(abs(dev))
            signed.append(dev)
        rounds_detail.append({
            "round": r, "n": len(rt),
            "vwap": round(v, 2) if v is not None else None,
            "neutral": round(neutral[r - 1], 2) if r - 1 < len(neutral) else None,
            "deviation": round(dev, 2) if dev is not None else None,
            "taker_buy": taker_buy,
            "taker_sell": len(rt) - taker_buy,
            "buy_ratio": (round(taker_buy / len(rt), 3) if rt else None),
        })

    # 偏低的輪次裡，主動賣出佔多少；偏高的輪次裡，主動買進佔多少
    def _share(rows, key):
        tot = sum(x["n"] for x in rows)
        return (round(sum(x[key] for x in rows) / tot, 3), tot) if tot else (None, 0)

    low = [x for x in rounds_detail if x["deviation"] is not None and x["deviation"] < 0]
    high = [x for x in rounds_detail if x["deviation"] is not None and x["deviation"] > 0]
    sell_share, low_n = _share(low, "taker_sell")
    buy_share, high_n = _share(high, "taker_buy")
    aggression = {
        "low_rounds": [x["round"] for x in low], "low_trades": low_n,
        "sell_share_when_low": sell_share,
        "high_rounds": [x["round"] for x in high], "high_trades": high_n,
        "buy_share_when_high": buy_share,
    }

    by_round, total = [], 0.0
    for r in range(1, TOTAL_ROUNDS + 1):
        rt = [tr for tr in market.trades if tr.round == r]
        amt = sum(abs(D - tr.price) for tr in rt) if D is not None else 0.0
        total += amt
        by_round.append({"round": r, "n": len(rt), "profit": round(amt, 2)})

    return {
        "events": events,
        "tally": tally,
        "rounds": rounds_detail,
        "aggression": aggression,
        "mae": round(sum(errs) / len(errs), 2) if errs else None,
        "bias": round(sum(signed) / len(signed), 2) if signed else None,
        "converging": (errs[0] > errs[-1]) if len(errs) >= 2 else None,
        "insider": {"total": round(total, 2), "by_round": by_round,
                    "n_trades": len(market.trades),
                    "per_trade": (round(total / len(market.trades), 2)
                                  if market.trades else None)},
    }


def _round_flow(market: Market) -> list:
    """習題 1 的中間步驟：每輪新開多單、平掉多單、以及未平倉量。

    照論文 Table 4 的算法：
      · 新開多單 = 該輪成交筆數（每一筆成交都會產生一口多單）
      · 平掉多單 = 該輪被平掉的多單口數，有兩種來源：
          1. 以前開的多單，這一輪被賣掉（賣方原本是多單）
          2. 這一輪新開的多單，當場就抵銷掉（買方原本是空單，買進其實是平倉）
      · 未平倉量 = 前一輪未平倉量 + 新開 − 平掉
        （等同於各交易者淨部位取正值後加總）
    """
    net, out = {}, []
    for r in range(1, TOTAL_ROUNDS + 1):
        opened = closed = 0
        for tr in market.trades:
            if tr.round != r:
                continue
            opened += 1
            if net.get(tr.buyer_id, 0) < 0:      # 買方原本是空單 → 當場抵銷
                closed += 1
            if net.get(tr.seller_id, 0) > 0:     # 賣方原本是多單 → 平掉舊多單
                closed += 1
            net[tr.buyer_id] = net.get(tr.buyer_id, 0) + 1
            net[tr.seller_id] = net.get(tr.seller_id, 0) - 1
        out.append({"opened": opened, "closed": closed,
                    "open_interest": sum(v for v in net.values() if v > 0)})
    return out


def _build_review(market: Market) -> dict:
    """結算後的檢討統計：每輪成交量／未平倉／結算價／揭露數字／輪初預期交割價。"""
    rounds = []
    prev_settle = None
    flow = _round_flow(market)
    for r in range(1, TOTAL_ROUNDS + 1):
        rt = [tr for tr in market.trades if tr.round == r]
        volume = len(rt)
        settle = rt[-1].price if rt else prev_settle
        opened = flow[r - 1]["opened"]
        closed = flow[r - 1]["closed"]
        open_interest = flow[r - 1]["open_interest"]
        # 第 k 輪輪初預期交割價 =（前 k-1 位已揭露之和）+ (5-(k-1)) * 4.5
        prior = sum(market.revealed[:r - 1]) if len(market.revealed) >= r - 1 else 0
        expected = prior + (TOTAL_ROUNDS - (r - 1)) * DIGIT_EXPECTED
        revealed_digit = market.revealed[r - 1] if len(market.revealed) >= r else None
        rounds.append({
            "round": r, "volume": volume,
            "opened": opened, "closed": closed,
            "settle_price": settle, "open_interest": open_interest,
            "revealed_digit": revealed_digit, "expected_delivery": expected,
        })
        prev_settle = settle
    led = _ledger(market)
    ledgers = sorted([{"badge": t.badge, "name": t.name, "color": t.color,
                       **led.get(tid, {})}
                      for tid, t in market.traders.items()],
                     key=lambda x: x["badge"])
    return {
        "delivery_price": market.delivery_price,
        "secret_digits": market.secret_digits,   # 結算後才給
        "rounds": rounds,
        "efficiency": _efficiency(market),       # 習題 6
        "ledgers": ledgers,                      # 習題 3：逐日結算表
        "settle_series": _settle_series(market),
        "margin": {"orig": market.orig_margin, "maint": market.maint_margin},
    }


# ── 廣播 ──────────────────────────────────────────────────────────────

async def broadcast(code: str):
    market = MARKETS.get(code)
    if not market:
        return
    dead = []
    for conn in CONNS.get(code, []):
        try:
            await conn.ws.send_json(
                build_board_state(market) if conn.role == "board"
                else build_state(market, conn.role, conn.trader_id))
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
        await _send(conn, {"type": "error",
                           "message": "Please enter your student ID (請輸入學號)"})
        return
    # 重連：以正規化後的姓名／學號認人，恢復原部位（同房間內唯一）
    key = _norm_name(name)
    if not key:
        await _send(conn, {"type": "error",
                           "message": "Please enter your student ID (請輸入學號)"})
        return
    existing = next((t for t in market.traders.values() if t.key == key), None)
    rejoined = existing is not None
    if existing:
        tid = existing.id
        _log_event(market.code, "rejoin",
                   {"trader_id": tid, "name": name, "round": market.round})
    else:
        tid = secrets.token_urlsafe(8)
        market.traders[tid] = Trader(
            id=tid, name=name, key=key, badge=_gen_badge(market),
            color=COLORS[len(market.traders) % len(COLORS)],
            joined_at=_now(), joined_round=market.round,
        )
        _log_event(market.code, "join",
                   {"trader_id": tid, "name": name, "key": key,
                    "badge": market.traders[tid].badge, "round": market.round})
    conn.role = "trader"
    conn.trader_id = tid
    p = _positions(market).get(tid, {"net": 0, "n_trades": 0})
    await _send(conn, {"type": "joined", "trader_id": tid,
                       "badge": market.traders[tid].badge,
                       "name": market.traders[tid].name,
                       "rejoined": rejoined,
                       "net": p["net"], "n_trades": p["n_trades"],
                       "pnl": _ledger(market).get(tid, {}).get("pnl", 0.0)})


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
    if not (0 <= price <= MAX_QUOTE):
        await _send(conn, {"type": "error",
                           "message": f"價格需在 0–{MAX_QUOTE} 之間"})
        return
    # 可立即成交就立刻成交，不要讓委託簿停在交叉狀態
    hit = _matchable(market, conn.trader_id, side, price)
    if hit is not None:
        _record_trade(market, hit, conn.trader_id)
        if hit in market.book:
            market.book.remove(hit)
        _log_event(market.code, "quote_matched",
                   {"trader_id": conn.trader_id, "side": side,
                    "quoted_price": price, "traded_price": hit.price,
                    "round": market.round})
        better = abs(price - hit.price)
        msg = (f"Your {'bid' if side == 'bid' else 'offer'} of {price} traded immediately "
               f"at {hit.price} — the best price already on the book.")
        if better:
            msg += (f" That is {better} better for you than the price you typed."
                    f"（你掛的價格立即以更好的 {hit.price} 成交）")
        await _send(conn, {"type": "notice", "message": msg})
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
    # 保證金只用在檢討面板的逐日結算表，不影響交易，所以結算後也能調整重算
    changed_margin = False
    for k in ("orig_margin", "maint_margin"):
        if k in msg:
            try:
                setattr(market, k, max(0.0, float(msg[k])))
                changed_margin = True
            except (TypeError, ValueError):
                pass
    if changed_margin:
        _log_event(market.code, "set_margin",
                   {"orig_margin": market.orig_margin,
                    "maint_margin": market.maint_margin})
    if "show_deviation" in msg:
        market.show_deviation = bool(msg["show_deviation"])
        _log_event(market.code, "set_display",
                   {"show_deviation": market.show_deviation})
    game_keys = {"round_seconds", "mode", "visible_fraction", "resample_seconds"}
    if not game_keys & set(msg):
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


async def _handle_board(market: Market, conn: Conn, msg: dict):
    """投影看板握手。不需要主持碼 —— 送出的全都是本來就公開的資訊。"""
    conn.role = "board"
    conn.trader_id = None
    await _send(conn, {"type": "board_ok", "code": market.code})


HANDLERS = {
    "board": _handle_board,
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
    orig_margin: float = 10.0          # 僅供檢討面板（習題 3）
    maint_margin: float = 8.0
    show_deviation: bool = False       # 學生端是否顯示偏離中性預期的提示


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
        orig_margin=max(0.0, settings.orig_margin),
        maint_margin=max(0.0, settings.maint_margin),
        show_deviation=bool(settings.show_deviation),
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


@router.get("/zip-board")
def zip_board_page():
    """投影用的公開看板（另開分頁丟到投影機）。"""
    return FileResponse("static/zip-board.html")


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
        header = ["房間 room", "模式 mode", "輪次 round", "時間 ts_iso", "成交價 price",
                  "買方姓名 buyer_name", "賣方姓名 seller_name",
                  "掛單方代號 maker_badge", "成交方代號 taker_badge",
                  "成交時最佳買價 best_bid_at_trade",
                  "成交時最佳賣價 best_offer_at_trade",
                  "執行滑價 execution_slippage"]
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
        header = ["房間 room", "模式 mode", "輪次 round", "時間 ts_iso", "委託編號 order_id",
                  "學號 student_id", "方向 side", "價格 price", "狀態 status",
                  "存續秒數 lifetime_seconds"]
        rows = [header]
        for o in market.all_orders:
            life = round((o.ended_at - o.ts), 2) if o.ended_at else ""
            rows.append([
                market.code, market.mode, o.round, _iso(o.ts), o.id,
                name_of.get(o.trader_id, "?"), o.side, o.price, o.status, life,
            ])
        return rows

    if kind == "summary":
        header = ["房間 room", "模式 mode", "學號 student_id", "代號 badge",
                  "成交數 n_trades", "買進次數 n_buys", "賣出次數 n_sells",
                  "淨部位 net_position", "交割價 delivery_price",
                  "損益 pnl",
                  "逐日結算追繳次數 margin_calls", "逐日結算追繳總額 margin_call_total",
                  "參與輪數 rounds_active"]
        rows = [header]
        pos = _positions(market)
        led = _ledger(market)
        dp = market.delivery_price
        for tid, t in market.traders.items():
            p = pos.get(tid, {"net": 0, "buys": 0, "sells": 0,
                              "n_trades": 0, "rounds": set()})
            L = led.get(tid, {})
            rows.append([
                market.code, market.mode, t.name, t.badge, p["n_trades"],
                p["buys"], p["sells"], p["net"],
                dp if dp is not None else "", L.get("pnl", 0.0),
                L.get("calls", 0), L.get("call_total", 0.0),
                len(p["rounds"]),
            ])
        return rows

    if kind == "efficiency":
        # 習題 6 的原始資料：每一次揭露當成一個事件，學生可以自己在 Excel 重算
        if market.phase != "settled":
            raise HTTPException(status_code=400, detail="尚未結算，無法匯出效率檢驗")
        header = ["房間 room", "模式 mode",
                  "揭露輪次 reveal_round", "反應輪次 response_round",
                  "揭露數字 digit", "消息 news", "應有變動 d_minus_4.5",
                  "揭露前均價 vwap_before", "揭露後均價 vwap_after",
                  "揭露前成交筆數 n_before", "揭露後成交筆數 n_after",
                  "價格反應 response", "方向同向 agrees",
                  "交割價 delivery_price"]
        rows = [header]
        for ev in _efficiency(market)["events"]:
            rows.append([
                market.code, market.mode, ev["round"], ev["round"] + 1, ev["digit"],
                "好消息 good" if ev["news"] == "good" else "壞消息 bad",
                ev["delta"], ev["vwap_before"], ev["vwap_after"],
                ev["n_before"], ev["n_after"], ev["response"],
                "" if ev["agree"] is None else ("是 Y" if ev["agree"] else "否 N"),
                market.delivery_price,
            ])
        return rows

    raise HTTPException(status_code=400,
                        detail="kind 必須是 trades|orders|summary|efficiency")


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
