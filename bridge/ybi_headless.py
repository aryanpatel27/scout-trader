"""Headless YBI bridge — replaces the Tampermonkey tabs.

A Playwright Chromium with its own persistent profile keeps the YBI web app open
24/7 and cycles through the five channels, forwarding every new message to the
local agent. Nothing depends on Chrome tab visibility any more, so the feed
keeps flowing while you're away (Chrome throttles/freezes background tabs; a
headless page is never "in the background").

  First time (or when your session expires) — log in yourself, headed:
      python3 bridge/ybi_headless.py --login
  Service (installed as a LaunchAgent, runs headless forever):
      python3 bridge/ybi_headless.py

Personal-use bridge: reads only what your own logged-in session displays, talks
only to 127.0.0.1. TOKEN must match [server].bridge_token in config.toml.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

URL = "https://app.youngbullinvestors.com/"
PORT = int(os.environ.get("AGENT_PORT", "8787"))
TOKEN = os.environ.get("BRIDGE_TOKEN", "ybi-tdt-local")
PROFILE = Path.home() / "Library" / "Application Support" / "agentic-trader" / "ybi-profile"
CHANNELS = {  # sidebar label -> channel key the parser/server already understand
    "Premarket Alerts": "premarket-alerts",
    "Intraday Alerts": "intraday-alerts",
    "Live Commentary": "live-commentary",
    "Targets": "targets",
    "Trading Questions": "trading-questions",
}
CYCLE_SEC = 20
HARVEST_JS = """
() => {
  let els = [...document.querySelectorAll("main [class*='message']")]
      .filter(el => !el.querySelector("[class*='message']"));
  if (!els.length) els = [...document.querySelectorAll("main p")];
  return els.map(el => (el.innerText || "").trim()).filter(t => t.length >= 6 && t.length <= 2000);
}
"""


def post(body: dict) -> None:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/ingest", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # agent down — keep cycling, it dedupes on restart
        print("post failed:", repr(e)[:80], flush=True)


def status(ok: bool, msg: str) -> None:
    post({"token": TOKEN, "type": "status", "component": "ybi-bridge", "ok": ok, "msg": msg})
    print(("OK   " if ok else "WARN ") + msg, flush=True)


def logged_in(page) -> bool:
    try:
        if page.locator("input[type=password]").count() > 0:
            return False
        return page.locator("main").count() > 0
    except Exception:
        return False


def run_login() -> int:
    from playwright.sync_api import sync_playwright
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE), headless=False,
                                                    viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL)
        print("\nA browser window opened. Log in to YBI there, wait until the channels "
              "show, then come back here and press Enter.", flush=True)
        input()
        ok = logged_in(page)
        ctx.close()
    print("Login saved — the headless bridge can use this session now." if ok
          else "Still not logged in (login form still showing). Run --login again.")
    return 0 if ok else 1


def run_service() -> int:
    from playwright.sync_api import sync_playwright
    PROFILE.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    while True:  # outer loop: full browser restart on any fatal error
        try:
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(str(PROFILE), headless=True,
                                                            viewport={"width": 1280, "height": 900})
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(URL, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                last_reload = time.time()
                warned_login = 0.0
                while True:
                    if not logged_in(page):
                        if time.time() - warned_login > 600:
                            status(False, "YBI session not logged in — run: python3 bridge/ybi_headless.py --login")
                            warned_login = time.time()
                        page.wait_for_timeout(60_000)
                        page.reload(wait_until="domcontentloaded")
                        page.wait_for_timeout(4000)
                        continue
                    n_new = 0
                    for label, key in CHANNELS.items():
                        try:
                            page.get_by_text(label, exact=True).first.click(timeout=5000)
                            page.wait_for_timeout(1500)
                            head = (page.locator("main").inner_text(timeout=3000) or "")[:300].lower()
                            if key.split("-")[0] not in head.replace(" ", "-"):
                                continue  # pane didn't switch — skip rather than mislabel
                            for text in page.evaluate(HARVEST_JS):
                                k = text[:400]
                                if k in seen:
                                    continue
                                seen.add(k)
                                n_new += 1
                                post({"token": TOKEN, "source": "ybi", "channel": key, "text": text})
                        except Exception as e:
                            print(f"channel {label}: {repr(e)[:80]}", flush=True)
                    status(True, f"cycle ok, {n_new} new message(s)")
                    if len(seen) > 20000:
                        seen = set(list(seen)[-5000:])
                    if time.time() - last_reload > 3600:  # hourly refresh keeps the socket honest
                        page.reload(wait_until="domcontentloaded")
                        page.wait_for_timeout(4000)
                        last_reload = time.time()
                    page.wait_for_timeout(CYCLE_SEC * 1000)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            status(False, f"bridge restarting after error: {repr(e)[:100]}")
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(run_login() if "--login" in sys.argv else run_service())
