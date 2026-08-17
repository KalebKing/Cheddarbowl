"""Raw-CDP capture that hooks EVERY espn.com target -- the page AND its web workers -- because ESPN
runs the draft's real-time connection inside a blob web worker the page session can't see.

Discovers targets from http://localhost:9222/json/list, opens a CDP client to each target's own
debugger endpoint, enables Network, and records websocket / SSE frames (plus any draft-ish HTTP
requests, to reveal the transport if it isn't a websocket) -> data/ws_capture.jsonl.

    python -m cheddarbowl.ingest.legacy.worker_capture        # Edge must be running with --remote-debugging-port=9222
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request

import websockets

from cheddarbowl import config

CAPTURE = os.path.join(config.DATA_DIR, "ws_capture.jsonl")
DEVTOOLS = "http://localhost:9222/json/list"
_fh = open(CAPTURE, "a", encoding="utf-8")


def rec(obj: dict) -> None:
    obj["t"] = round(time.time(), 3)
    _fh.write(json.dumps(obj) + "\n")
    _fh.flush()


def targets() -> list[dict]:
    with urllib.request.urlopen(DEVTOOLS, timeout=5) as r:
        data = json.load(r)
    out = []
    for tgt in data:
        url = tgt.get("url", "")
        if "espn.com" in url and tgt.get("webSocketDebuggerUrl"):
            out.append(tgt)
    return out


async def pump(tgt: dict) -> None:
    tag = f"{tgt.get('type')}:{tgt.get('url','')[:60]}"
    wsurls: dict[str, str] = {}
    try:
        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=None) as cdp:
            await cdp.send(json.dumps({"id": 1, "method": "Network.enable"}))
            rec({"ev": "attached", "target": tag})
            print(f"[attached] {tag}", flush=True)
            async for raw in cdp:
                msg = json.loads(raw)
                m = msg.get("method", "")
                p = msg.get("params", {}) or {}
                if m == "Network.webSocketCreated":
                    wsurls[p.get("requestId")] = p.get("url", "")
                    rec({"ev": "ws-open", "url": p.get("url", ""), "target": tag})
                    print(f"[ws] OPEN {p.get('url','')}", flush=True)
                elif m == "Network.webSocketFrameReceived":
                    r = p.get("response", {}) or {}
                    if r.get("opcode") in (1, 2):
                        rec({"ev": "recv", "url": wsurls.get(p.get("requestId"), "?"),
                             "data": str(r.get("payloadData", ""))[:5000]})
                elif m == "Network.webSocketFrameSent":
                    r = p.get("response", {}) or {}
                    if r.get("opcode") in (1, 2):
                        rec({"ev": "sent", "url": wsurls.get(p.get("requestId"), "?"),
                             "data": str(r.get("payloadData", ""))[:2000]})
                elif m == "Network.eventSourceMessageReceived":
                    rec({"ev": "sse", "eventName": p.get("eventName"),
                         "data": str(p.get("data", ""))[:5000], "target": tag})
                elif m == "Network.requestWillBeSent":
                    u = (p.get("request", {}) or {}).get("url", "")
                    if any(k in u.lower() for k in ("draft", "pick", "gambit", "fantasycast", "fcm")):
                        rec({"ev": "http", "url": u[:200], "target": tag})
    except Exception as e:  # noqa: BLE001
        rec({"ev": "err", "target": tag, "msg": str(e)[:200]})
        print(f"[err] {tag}: {e}", flush=True)


async def main() -> None:
    tgts = targets()
    print(f"espn targets: {len(tgts)}", flush=True)
    for t in tgts:
        print(f"  - {t.get('type')} {t.get('url','')[:70]}", flush=True)
    rec({"ev": "note", "msg": f"worker-capture attaching to {len(tgts)} espn target(s)"})
    if not tgts:
        print("No espn targets with a debugger URL found.", flush=True)
        return
    await asyncio.gather(*(pump(t) for t in tgts))


if __name__ == "__main__":
    asyncio.run(main())
