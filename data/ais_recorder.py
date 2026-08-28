#!/usr/bin/env python3
"""RELAY day-one AIS recorder (aisstream.io -> JSONL).

Records the Singapore box continuously; this recording IS our deterministic
replay (aisstream has no replay/SLA upstream). One connection only (account
cap is 3). Auto-reconnects with backoff; rotates one file per UTC day.

Run:  nohup python3 data/ais_recorder.py >> data/ais/recorder.log 2>&1 &
Stop: kill $(cat data/ais/recorder.pid)
"""
import asyncio
import datetime as dt
import json
import os
import pathlib
import signal
import sys

import websockets

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "ais"
OUT.mkdir(exist_ok=True)

# Singapore Strait + anchorages + Tuas/Pasir Panjang approaches.
BOX = [[[1.03, 103.30], [1.50, 104.25]]]
URL = "wss://stream.aisstream.io/v0/stream"
TYPES = ["PositionReport", "ShipStaticData", "StandardClassBPositionReport"]


def _key() -> str:
    k = os.environ.get("AISSTREAM_API_KEY")
    if not k:
        env = ROOT.parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("AISSTREAM_API_KEY="):
                    k = line.split("=", 1)[1].strip()
    if not k:
        sys.exit("AISSTREAM_API_KEY not set (env or ../.env)")
    return k


async def record() -> None:
    key = _key()
    sub = json.dumps(
        {"APIKey": key, "BoundingBoxes": BOX, "FilterMessageTypes": TYPES}
    )
    backoff = 1
    n = 0
    while True:
        try:
            async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(sub)
                backoff = 1
                print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] connected", flush=True)
                async for raw in ws:
                    now = dt.datetime.now(dt.timezone.utc)
                    path = OUT / f"ais-{now:%Y%m%d}.jsonl"
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    msg["_received_at"] = now.isoformat()
                    with path.open("a") as f:
                        f.write(json.dumps(msg, separators=(",", ":")) + "\n")
                    n += 1
                    if n % 5000 == 0:
                        print(f"[{now.isoformat()}] {n} messages", flush=True)
        except Exception as exc:  # noqa: BLE001, must survive anything
            print(
                f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] "
                f"reconnect in {backoff}s after: {exc!r}",
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main() -> None:
    (OUT / "recorder.pid").write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    asyncio.run(record())


if __name__ == "__main__":
    main()
