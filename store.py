"""SQLite storage (simulator.db): users, portfolio states, transactions, price ticks, daily prices, company info."""
import hashlib, json, os, re, secrets, sqlite3, threading, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DHAKA = ZoneInfo("Asia/Dhaka")
_lock = threading.RLock()


def _open():
    """Use simulator.db next to the code; on read-only hosts (Vercel) fall back to /tmp (temporary!)."""
    for p in (os.environ.get("DSE_DB"), str(Path(__file__).parent / "simulator.db"), "/tmp/simulator.db"):
        if not p:
            continue
        try:
            c = sqlite3.connect(p, check_same_thread=False)
            c.execute("create table if not exists _probe(x)")
            c.commit()
            return c, p
        except Exception:
            continue
    return sqlite3.connect(":memory:", check_same_thread=False), ":memory:"


_db, DB_PATH = _open()
EPHEMERAL = DB_PATH in (":memory:", "/tmp/simulator.db")  # data is lost when the host recycles
_db.row_factory = sqlite3.Row
_db.executescript("""
create table if not exists users(name text primary key, created_at text);
create table if not exists accounts(key text primary key, name text, salt text, pw_hash text, created_at text);
create table if not exists sessions(token text primary key, user text, created_at text);
create table if not exists states(user text primary key, json text, updated_at text);
create table if not exists transactions(user text, id text, ts text, type text, symbol text, qty real, price real, fee real, total real, pnl real, note text, primary key(user,id));
create table if not exists ticks(ts integer, date text, symbol text, ltp real, ycp real, high real, low real, volume real);
create index if not exists ix_ticks_ts on ticks(ts);
create index if not exists ix_ticks_date on ticks(date);
create table if not exists daily(date text, symbol text, ltp real, ycp real, high real, low real, volume real, value_mn real, trades real, primary key(date,symbol));
create table if not exists companies(symbol text primary key, name text, sector text, category text, pe real, info text, fetched_at text);
""")


def q(sql, args=(), one=False):
    with _lock:
        rows = [dict(r) for r in _db.execute(sql, args).fetchall()]
        _db.commit()
    return (rows[0] if rows else None) if one else rows


# ---- users and portfolio state ----
def users():
    return [r["name"] for r in q("select name from users order by created_at")]

def add_user(name):
    q("insert or ignore into users values(?,?)", (name, datetime.now(DHAKA).isoformat()))

def get_state(user):
    r = q("select json from states where user=?", (user,), one=True)
    return r["json"] if r else None

def put_state(user, js):
    st = json.loads(js)
    add_user(user)
    with _lock:
        _db.execute("insert into states values(?,?,?) on conflict(user) do update set json=excluded.json, updated_at=excluded.updated_at",
                    (user, js, datetime.now(DHAKA).isoformat()))
        _db.execute("delete from transactions where user=?", (user,))
        _db.executemany("insert or replace into transactions values(?,?,?,?,?,?,?,?,?,?,?)",
                        [(user, t.get("id"), t.get("ts"), t.get("type"), t.get("symbol"), t.get("qty"), t.get("price"),
                          t.get("fee"), t.get("total"), t.get("pnl"), t.get("note")) for t in st.get("txns", [])])
        _db.commit()


# ---- accounts: unique names, salted password hashes, per-user session tokens ----
def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 150000).hex()

def _session(name):
    tok = secrets.token_urlsafe(24)
    q("insert into sessions values(?,?,?)", (tok, name, datetime.now(DHAKA).isoformat()))
    return tok

def register(name, pw):
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", name) or name.lower() == "default":
        raise ValueError("Username must be 3-30 letters, numbers, . _ - (and not 'default').")
    if len(pw) < 4:
        raise ValueError("Password must be at least 4 characters.")
    if q("select 1 from accounts where key=?", (name.lower(),)):
        raise KeyError("That username is already taken.")
    salt = secrets.token_hex(16)
    q("insert into accounts values(?,?,?,?,?)", (name.lower(), name, salt, _hash(pw, salt), datetime.now(DHAKA).isoformat()))
    add_user(name)
    return name, _session(name)

def login(name, pw):
    r = q("select * from accounts where key=?", (name.strip().lower(),), one=True)
    if not r or not secrets.compare_digest(r["pw_hash"], _hash(pw, r["salt"])):
        raise PermissionError("Wrong username or password.")
    return r["name"], _session(r["name"])

def allowed(user, token):
    """'default' is the shared test account (no password). Everyone else needs their own token."""
    if user == "default":
        return True
    r = q("select user from sessions where token=?", (token or "",), one=True)
    return bool(r) and r["user"] == user


# ---- market history ----
def save_market(d, tick):
    ts = int(time.time())
    today = datetime.fromtimestamp(ts, DHAKA).date().isoformat()
    day = datetime.strptime(d["asOf"], "%b %d, %Y").date().isoformat()
    with _lock:
        _db.executemany("insert or replace into daily values(?,?,?,?,?,?,?,?,?)",
                        [(day, s["symbol"], s["ltp"], s["ycp"], s["high"], s["low"], s["volume"], s["valueMn"], s["trades"]) for s in d["stocks"]])
        if tick:
            _db.executemany("insert into ticks values(?,?,?,?,?,?,?,?)",
                            [(ts, today, s["symbol"], s["ltp"], s["ycp"], s["high"], s["low"], s["volume"]) for s in d["stocks"]])
        _db.commit()

def history_dates():
    out = {r["date"]: r["n"] for r in q("select date, count(distinct ts) n from ticks group by date")}
    for r in q("select distinct date from daily"):
        out.setdefault(r["date"], 0)
    return [{"date": k, "ticks": v} for k, v in sorted(out.items(), reverse=True)]

def history_times(date):
    rows = q("select distinct ts from ticks where date=? order by ts", (date,))
    if rows:
        return [{"ts": r["ts"], "label": datetime.fromtimestamp(r["ts"], DHAKA).strftime("%H:%M")} for r in rows]
    return [{"ts": None, "label": "Close"}] if q("select 1 from daily where date=? limit 1", (date,)) else []

def history_snapshot(ts=None, date=None):
    if ts:
        return q("select symbol, ltp, ycp, high, low, volume from ticks where ts=?", (ts,))
    return q("select symbol, ltp, ycp, high, low, volume from daily where date=?", (date,))


# ---- company information (scraped once from DSE company pages, politely) ----
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
STATUS = {"done": 0, "total": 0, "running": False}
_meta = {"at": 0, "data": {}}

def _get(url, params):
    import requests
    try:
        return requests.get(url, params=params, headers=UA, timeout=20)
    except requests.exceptions.SSLError:
        return requests.get(url, params=params, headers=UA, timeout=20, verify=False)

def parse_company(html):
    from bs4 import BeautifulSoup
    info = {}
    for tr in BeautifulSoup(html, "html.parser").find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"], recursive=False)]
        if len(cells) in (2, 4):
            for k, v in zip(cells[0::2], cells[1::2]):
                k = k.rstrip(": ").strip()
                if k and v and len(k) <= 70 and len(v) <= 400 and k not in info and len(info) < 120:
                    info[k] = v
    return info

def _pick(info, *needles):
    for k, v in info.items():
        if any(n in k.lower() for n in needles):
            return v

def sync_companies(symbols):
    have = {r["symbol"] for r in q("select symbol from companies")}
    todo = [s for s in symbols if s not in have]
    STATUS.update(done=len(have), total=len(have) + len(todo), running=bool(todo))
    for sym in todo:
        for base in ("https://old.dsebd.org", "https://www.dsebd.org"):
            try:
                r = _get(base + "/displayCompany.php", {"name": sym})
                if r.status_code != 200:
                    continue
                info = parse_company(r.text)
                if not info:
                    continue
                cat = re.search(r"\b([ABNZ])\b", _pick(info, "market category") or "")
                pe = re.search(r"-?\d+(\.\d+)?", (_pick(info, "p/e") or "").replace(",", ""))
                q("insert or replace into companies values(?,?,?,?,?,?,?)",
                  (sym, _pick(info, "company name"), _pick(info, "sector"), cat.group(1) if cat else None,
                   float(pe.group()) if pe else None, json.dumps(info), datetime.now(DHAKA).isoformat()))
                break
            except Exception:
                continue
        STATUS["done"] += 1
        time.sleep(0.7)
    STATUS["running"] = False

def company_meta():
    if time.time() - _meta["at"] > 120:
        _meta["data"] = {r["symbol"]: r for r in q("select symbol, name, sector, category, pe from companies")}
        _meta["at"] = time.time()
    return _meta["data"]

def company(symbol):
    r = q("select * from companies where symbol=?", (symbol,), one=True)
    if r:
        r["info"] = json.loads(r["info"] or "{}")
    return r


# ---- background collector: prices every 5 min while the market is open ----
def background(get_market, is_open):
    last_tick, synced = 0.0, False
    while True:
        try:
            d = get_market()
            if not synced:
                synced = True
                threading.Thread(target=sync_companies, args=([s["symbol"] for s in d["stocks"]],), daemon=True).start()
            open_, first = is_open(), last_tick == 0
            if open_ or first:
                tick = open_ and time.time() - last_tick >= 300
                save_market(d, tick)
                if tick or first:
                    last_tick = time.time()
        except Exception as e:
            print("collector:", e)
        time.sleep(60)
