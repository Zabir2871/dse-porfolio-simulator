"""DSE data layer built on the bdshare library (it tracks DSE's site changes for us)."""
import json, re, time, threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
DHAKA = ZoneInfo("Asia/Dhaka")
STATE_FILE = BASE / "state.json"

_ALIASES = {  # normalised column name -> our field
    "symbol": "symbol", "tradingcode": "symbol", "code": "symbol",
    "ltp": "ltp", "high": "high", "low": "low",
    "close": "close", "closep": "close", "closeprice": "close",
    "ycp": "ycp", "change": "change",
    "trade": "trades", "trades": "trades",
    "value": "valueMn", "valuemn": "valueMn", "volume": "volume",
}


def num(v):
    """'1,234.5' -> 1234.5 ; '--', '', None, NaN -> None"""
    if v is None:
        return None
    s = str(v).replace(",", "").replace("\u00a0", "").strip().lower()
    if s in ("", "--", "-", "nil", "n/a", "na", "nan", "none", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_categories():
    try:
        return json.loads((BASE / "categories.json").read_text())
    except Exception:
        return {}


def market_open(now=None):
    n = (now or datetime.now(DHAKA)).astimezone(DHAKA)
    return n.weekday() not in (4, 5) and 600 <= n.hour * 60 + n.minute <= 875  # Sun-Thu 10:00-14:35


def last_trading_date(now=None):
    """Best guess of the trading date the current prices belong to (holidays handled in trading_date())."""
    d = (now or datetime.now(DHAKA)).astimezone(DHAKA)
    if d.hour * 60 + d.minute < 600:
        d -= timedelta(days=1)
    while d.weekday() in (4, 5):  # Fri, Sat
        d -= timedelta(days=1)
    return d.date()


def trading_date(signature):
    """If the date moved forward but no stock's volume changed, it was a holiday: keep the old date."""
    guess = last_trading_date()
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    prev = datetime.strptime(st["date"], "%Y-%m-%d").date() if st.get("date") else None
    if prev and guess > prev and st.get("sig") == signature:
        guess = prev
    STATE_FILE.write_text(json.dumps({"date": guess.isoformat(), "sig": signature}))
    return guess


def fetch_market():
    from bdshare import get_current_trade_data  # imported here so the module loads without it
    df = get_current_trade_data()
    if df is None or len(df) == 0:
        raise RuntimeError("bdshare returned no data (DSE unreachable or blocked)")
    if "symbol" not in {re.sub(r"[^a-z]", "", str(c).lower()) for c in df.columns}:
        df = df.reset_index()
    df = df.rename(columns={c: _ALIASES.get(re.sub(r"[^a-z]", "", str(c).lower()), c) for c in df.columns})
    if "symbol" not in df.columns or "ltp" not in df.columns:
        raise RuntimeError(f"Unexpected columns from bdshare: {list(df.columns)}")

    cats, stocks, sig = load_categories(), [], 0
    for r in df.to_dict("records"):
        sym = str(r.get("symbol", "")).strip().upper()
        if not sym:
            continue
        ycp, ltp = num(r.get("ycp")), num(r.get("ltp"))
        traded = bool(ltp and ltp > 0)
        if not traded:
            ltp = ycp
        if ltp is None:
            continue
        ch = num(r.get("change")) if traded else 0.0
        if ch is None:
            ch = (ltp - ycp) if (traded and ycp is not None) else 0.0
        pos = lambda v: v if v and v > 0 else None
        vol = num(r.get("volume")) or 0
        sig += int(vol)
        stocks.append({
            "symbol": sym, "ltp": ltp, "ycp": ycp if ycp is not None else ltp,
            "high": pos(num(r.get("high"))) or ltp, "low": pos(num(r.get("low"))) or ltp,
            "change": round(ch, 2), "changePercent": round(ch / ycp * 100, 2) if ycp else 0.0,
            "category": cats.get(sym, "A"),
            "close": pos(num(r.get("close"))), "trades": num(r.get("trades")) or 0,
            "valueMn": num(r.get("valueMn")) or 0, "volume": vol,
            "traded": traded, "categorySource": "file" if sym in cats else "default",
        })
    if len(stocks) < 100:
        raise RuntimeError(f"Only {len(stocks)} stocks parsed; DSE data looks incomplete")
    d = trading_date(sig)
    return {"source": "bdshare", "fetchedAt": datetime.now(DHAKA).isoformat(),
            "asOf": d.strftime("%b %d, %Y"), "count": len(stocks), "stocks": stocks}


def fetch_history(symbol, days=370):
    """Daily closes for the last ~year (for the 52-week range and the price chart)."""
    from bdshare import get_basic_historical_data
    end = datetime.now(DHAKA).date()
    df = get_basic_historical_data((end - timedelta(days=days)).isoformat(), end.isoformat(), symbol)
    if df is None or len(df) == 0:
        raise RuntimeError("no history returned")
    df = df.reset_index()
    cols = {re.sub(r"[^a-z]", "", str(c).lower()): c for c in df.columns}
    ccol = cols.get("close") or cols.get("closep") or cols.get("ltp")
    dcol = cols.get("date")
    if ccol is None:
        raise RuntimeError(f"no close column in {list(df.columns)}")
    if dcol is not None:
        df = df.sort_values(dcol)
    closes = [c for c in (num(v) for v in df[ccol]) if c]
    highs = [h for h in (num(v) for v in df[cols["high"]]) if h] if "high" in cols else closes
    lows = [x for x in (num(v) for v in df[cols["low"]]) if x] if "low" in cols else closes
    return {"symbol": symbol, "closes": closes[-60:], "hi52": max(highs), "lo52": min(lows)}


class TTLCache:
    """One upstream call at a time; serve the last good copy if DSE is down."""
    def __init__(self, fn):
        self.fn, self.lock, self.data, self.at = fn, threading.Lock(), None, 0.0

    def get(self, ttl):
        with self.lock:
            if self.data is not None and time.time() - self.at < ttl:
                return self.data
            try:
                self.data, self.at = self.fn(), time.time()
                return self.data
            except Exception as e:
                if self.data is not None:
                    return {**self.data, "stale": True, "error": str(e)}
                raise
