"""Attach to a normally-launched Edge (started with --remote-debugging-port=9222) over CDP and record
websocket frames -> data/ws_capture.jsonl.

Uses a RAW CDP Network session (not Playwright's page.on("websocket")) so it captures frames on
websockets that were ALREADY OPEN before we attached -- essential for hooking a draft mid-stream
without reloading the page. Because we attach to a real user-driven browser, ESPN's SPA renders and
connects normally (no navigator.webdriver stall).

    python -m cheddarbowl.ingest.legacy.attach_capture
"""
from __future__ import annotations

import json
import os
import time

from cheddarbowl import config
from playwright.sync_api import sync_playwright

CAPTURE = os.path.join(config.DATA_DIR, "ws_capture.jsonl")
CDP = "http://localhost:9222"


def main() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fh = open(CAPTURE, "a", encoding="utf-8")

    def rec(obj: dict) -> None:
        obj["t"] = round(time.time(), 3)
        fh.write(json.dumps(obj) + "\n")
        fh.flush()

    urls: dict[str, str] = {}   # requestId -> ws url

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        print(f"attached: {browser.version}", flush=True)
        sessions = []
        for ctx in browser.contexts:
            for pg in ctx.pages:
                sess = ctx.new_cdp_session(pg)

                def created(params, _pg=pg):
                    urls[params["requestId"]] = params.get("url", "")
                    rec({"ev": "ws-open", "url": params.get("url", ""), "page": _pg.url[:80]})
                    print(f"[ws] OPEN {params.get('url','')}", flush=True)

                def recv(params):
                    r = params.get("response", {}) or {}
                    if r.get("opcode") in (1, 2):   # 1=text 2=binary (skip ping/pong/close)
                        rec({"ev": "recv", "url": urls.get(params.get("requestId"), "?"),
                             "op": r.get("opcode"), "data": str(r.get("payloadData", ""))[:4000]})

                def sent(params):
                    r = params.get("response", {}) or {}
                    if r.get("opcode") in (1, 2):
                        rec({"ev": "sent", "url": urls.get(params.get("requestId"), "?"),
                             "op": r.get("opcode"), "data": str(r.get("payloadData", ""))[:2000]})

                sess.on("Network.webSocketCreated", created)
                sess.on("Network.webSocketFrameReceived", recv)
                sess.on("Network.webSocketFrameSent", sent)
                sess.send("Network.enable")
                sessions.append(sess)
                print(f"[wired-cdp] {pg.url[:80]}", flush=True)

        rec({"ev": "note", "msg": f"raw-CDP attached to {len(sessions)} page(s); Network.enable sent"})
        print(">>> Keep drafting; capturing frames on the open socket now.", flush=True)
        n = 0
        while True:
            time.sleep(3)
            n += 1
            if n % 10 == 0:
                print(f"  … listening ({n*3}s)", flush=True)


if __name__ == "__main__":
    main()
