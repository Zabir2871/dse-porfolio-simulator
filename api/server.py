"""
DSE live API + database for the portfolio simulator.   python server.py   ->  http://localhost:3000
"""
import os
import threading
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from dse_data import TTLCache, fetch_market, fetch_history, market_open, load_categories
import store

app = FastAPI(title="DSE live API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
market = TTLCache(fetch_market)
_hist = {}


def current():
    return market.get(60 if market_open() else 15 * 60)


@app.on_event("startup")
def start_collector():
    if os.environ.get("VERCEL"):  # serverless: no background threads, so no price recording
        return
    threading.Thread(target=store.background, args=(current, market_open), daemon=True).start()


@app.get("/api/dse-live")
def dse_live(symbols: str = Query(""), fresh: bool = False):
    try:
        data = market.get(10) if fresh else current()
    except Exception as e:
        raise HTTPException(502, str(e))
    want = {s.strip().upper() for s in symbols.split(",") if s.strip()}
    meta, cats, out = store.company_meta(), load_categories(), []
    for s in data["stocks"]:
        if want and s["symbol"] not in want:
            continue
        s, m = dict(s), meta.get(s["symbol"])
        if m:
            s["name"], s["sector"], s["pe"] = m["name"], m["sector"], m["pe"]
            if s["symbol"] not in cats and m["category"]:
                s["category"], s["categorySource"] = m["category"], "db"
        out.append(s)
    return {**data, "marketOpen": market_open(), "count": len(out), "stocks": out}


@app.get("/api/dse-history/{symbol}")
def dse_history(symbol: str):
    symbol = symbol.upper()
    try:
        return _hist.setdefault(symbol, TTLCache(lambda: fetch_history(symbol))).get(6 * 3600)
    except Exception as e:
        raise HTTPException(502, str(e))


# ---- accounts and portfolio state (database) ----
@app.post("/api/register")
async def register(req: Request):
    j = await req.json()
    try:
        name, tok = store.register(str(j.get("name", "")), str(j.get("password", "")))
    except KeyError as e:
        raise HTTPException(409, e.args[0])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"name": name, "token": tok}

@app.post("/api/login")
async def login(req: Request):
    j = await req.json()
    try:
        name, tok = store.login(str(j.get("name", "")), str(j.get("password", "")))
    except PermissionError as e:
        raise HTTPException(401, str(e))
    return {"name": name, "token": tok}

@app.get("/api/state/{user}")
def get_state(user: str, x_token: str = Header("")):
    if not store.allowed(user, x_token):
        raise HTTPException(401, "sign in required")
    js = store.get_state(user)
    if js is None:
        raise HTTPException(404, "no saved state")
    return Response(js, media_type="application/json")

@app.put("/api/state/{user}")
async def put_state(user: str, req: Request, x_token: str = Header("")):
    if not store.allowed(user, x_token):
        raise HTTPException(401, "sign in required")
    try:
        store.put_state(user[:40], (await req.body()).decode())
    except ValueError:
        raise HTTPException(400, "invalid json")
    return {"ok": True}


# ---- price history and companies ----
@app.get("/api/history/dates")
def h_dates():
    return store.history_dates()

@app.get("/api/history/times")
def h_times(date: str):
    return store.history_times(date)

@app.get("/api/history/snapshot")
def h_snap(ts: int = 0, date: str = ""):
    return store.history_snapshot(ts or None, date or None)

@app.get("/api/companies/status")
def c_status():
    return store.STATUS

@app.get("/api/company/{symbol}")
def c_one(symbol: str):
    c = store.company(symbol.upper())
    if not c:
        raise HTTPException(404, "not fetched yet")
    return c

@app.get("/healthz")
def health():
    return {"ok": True, "marketOpen": market_open(), "db": store.DB_PATH, "dataIsTemporary": store.EPHEMERAL}


HERE = Path(__file__).resolve().parent
INDEX = next((p for p in (HERE / "public" / "index.html", HERE / "index.html") if p.exists()), None)

@app.get("/")
def home():
    if INDEX:
        return FileResponse(INDEX)
    return {"message": "API is running. Put index.html next to server.py or in a 'public' folder.", "try": "/api/dse-live"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=3000)
