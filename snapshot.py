"""Save an end-of-day snapshot to eod/YYYY-MM-DD.json. Schedule at ~15:00 Dhaka, Sun-Thu.
   Windows Task Scheduler / cron:  python snapshot.py
"""
import json
from pathlib import Path
from dse_data import fetch_market

d = fetch_market()
out = Path(__file__).parent / "eod"
out.mkdir(exist_ok=True)
f = out / f"{d['asOf'].replace(',', '').replace(' ', '-')}.json"
f.write_text(json.dumps(d, indent=1))
print(f"Saved {d['count']} stocks to {f}")
