#!/usr/bin/env python3
"""RELAY weather recorder: NEA real-time feeds to JSONL, for the decision-bearing
weather integration.

Records, every POLL_S seconds:
  * wind-speed  (all stations; S117 Banyan Road is the Tuas-adjacent one)
  * rainfall    (all stations)
  * lightning   (strike observations, when any)
  * 2-hour forecast (area forecasts incl. the western areas around Tuas)

Everything is keyless and public (data.gov.sg real-time APIs, Singapore Open Data
Licence v1.0). One file per UTC day, one JSON object per poll, so a replay reads a
real weather timeline rather than a fabricated one.

Run:  nohup python3 data/weather_recorder.py >> data/weather/recorder.log 2>&1 &
Stop: kill $(cat data/weather/recorder.pid)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import signal
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "weather"
OUT.mkdir(exist_ok=True)

BASE = "https://api-open.data.gov.sg/v2/real-time/api"
FEEDS = {
    "wind_speed": f"{BASE}/wind-speed",
    "rainfall": f"{BASE}/rainfall",
    "lightning": f"{BASE}/weather?api=lightning",
    "forecast_2h": f"{BASE}/two-hr-forecast",
}
POLL_S = 300          # 5 minutes: NEA updates on that order, and it keeps the file small
TIMEOUT_S = 20
# Station nearest the Tuas terminals; kept here so the consumer does not guess.
TUAS_WIND_STATION = "S117"


def fetch(url: str) -> dict:
    # data.gov.sg rejects the default urllib agent with 403; send a real one.
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "RELAY-weather-recorder/1.0 (PSA Code Sprint entry; contact via submission)",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def poll_once() -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    record: dict = {
        "_polled_at": now.isoformat(),
        "_source": "data.gov.sg real-time APIs (NEA), Singapore Open Data Licence v1.0",
        "_provenance": "RECORDED",
        "tuas_wind_station": TUAS_WIND_STATION,
        "feeds": {},
    }
    for name, url in FEEDS.items():
        try:
            record["feeds"][name] = {"ok": True, "payload": fetch(url)}
        except Exception as exc:  # noqa: BLE001 - a feed outage is data, not a crash
            record["feeds"][name] = {"ok": False, "error": repr(exc)[:200]}
    return record


def main() -> None:
    (OUT / "recorder.pid").write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    n = 0
    while True:
        rec = poll_once()
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        with (OUT / f"weather-{day}.jsonl").open("a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        n += 1
        ok = sum(1 for f in rec["feeds"].values() if f["ok"])
        print(f"[{rec['_polled_at']}] poll {n}: {ok}/{len(FEEDS)} feeds ok", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
