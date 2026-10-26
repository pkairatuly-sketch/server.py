"""
Flash -> Roblox bridge.

Runs a .swf in Ruffle inside headless Chromium (Playwright), grabs screenshots,
shrinks them to a tiny 3-3-2 palette image, and serves batches of frames to a
Roblox server script over HTTP. Roblox sends keyboard/mouse input back in the
same request.

Setup:
    pip install flask pillow numpy playwright
    playwright install chromium

Run:
    BRIDGE_KEY=some-long-random-string python server.py path/to/game.swf
    cloudflared tunnel --url http://localhost:8000   # gives you a public https URL

Only use .swf files you own or have permission to use.
"""
import os
import queue
import struct
import sys
import threading
import time
from collections import deque
from io import BytesIO

import numpy as np
from flask import Flask, Response, abort, request, send_file
from PIL import Image
from playwright.sync_api import sync_playwright

# ---- settings ---------------------------------------------------------------
PORT = 8000
W, H = 96, 72            # frame size sent to Roblox (keep 4:3 for most Flash games)
VW, VH = 640, 480        # size of the browser viewport / Flash stage
FPS = 8                  # capture rate
MAX_BATCH = 6            # max frames returned per /sync call
SECRET = os.environ.get("BRIDGE_KEY", "change-me")
# -----------------------------------------------------------------------------

if len(sys.argv) < 2:
    sys.exit("usage: python server.py path/to/game.swf  OR  python server.py https://site/game [--ruffle]")
TARGET = sys.argv[1]
URL_MODE = TARGET.startswith("http://") or TARGET.startswith("https://")
INJECT_RUFFLE = "--ruffle" in sys.argv  # for old Flash sites that don't ship their own emulator
AUTO_PLAY = "--play" in sys.argv  # try to click the site's Play button on load (e.g. slither.io)
SWF_PATH = None if URL_MODE else os.path.abspath(TARGET)

app = Flask(__name__)
frames = deque(maxlen=64)       # (seq, bytes)
seq = 0
lock = threading.Lock()
inputs = queue.Queue()

PLAYER_HTML = """<!doctype html>
<html><body style="margin:0;background:#000;overflow:hidden">
<script>window.RufflePlayer = {config: {autoplay: "on", unmuteOverlay: "hidden"}};</script>
<script src="https://unpkg.com/@ruffle-rs/ruffle"></script>
<script>
window.addEventListener("load", () => {
  const player = window.RufflePlayer.newest().createPlayer();
  player.style.width = "%dpx";
  player.style.height = "%dpx";
  document.body.appendChild(player);
  player.load("/game.swf");
});
</script></body></html>""" % (VW, VH)


@app.get("/player")
def player():
    return Response(PLAYER_HTML, mimetype="text/html")


@app.get("/game.swf")
def swf():
    return send_file(SWF_PATH, mimetype="application/x-shockwave-flash")


@app.post("/sync")
def sync():
    if request.headers.get("X-Key") != SECRET:
        abort(403)
    data = request.get_json(silent=True) or {}
    for ev in (data.get("inputs") or [])[:100]:
        if isinstance(ev, dict):
            inputs.put(ev)
    try:
        since = int(data.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    with lock:
        if since > seq:          # server restarted, client is ahead
            since = 0
        fresh = [f for f in frames if f[0] > since][-MAX_BATCH:]
    body = struct.pack("BBB", len(fresh), W, H)
    body += b"".join(struct.pack("<I", s) + d for s, d in fresh)
    return Response(body, mimetype="application/octet-stream")


def to_332(png_bytes: bytes) -> bytes:
    """Shrink to W x H and pack each pixel into one byte (RRRGGGBB)."""
    img = Image.open(BytesIO(png_bytes)).convert("RGB").resize((W, H), Image.BILINEAR)
    a = np.asarray(img, dtype=np.uint8)
    packed = ((a[..., 0] >> 5) << 5) | ((a[..., 1] >> 5) << 2) | (a[..., 2] >> 6)
    return packed.astype(np.uint8).tobytes()


def apply_input(page, ev):
    try:
        t = ev.get("t")
        if t == "kd":
            page.keyboard.down(str(ev["k"]))
        elif t == "ku":
            page.keyboard.up(str(ev["k"]))
        elif t in ("click", "move", "md", "mu"):
            x = min(max(float(ev["x"]), 0), 1) * VW
            y = min(max(float(ev["y"]), 0), 1) * VH
            page.mouse.move(x, y)
            if t == "click":
                page.mouse.down()
                page.mouse.up()
            elif t == "md":
                page.mouse.down()
            elif t == "mu":
                page.mouse.up()
    except Exception as e:  # bad key name etc.
        print("input error:", e)


def main():
    global seq
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, threaded=True),
        daemon=True,
    ).start()
    time.sleep(1)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": VW, "height": VH})
        if URL_MODE:
            if INJECT_RUFFLE:
                page.add_init_script(
                    "document.addEventListener('DOMContentLoaded',()=>{"
                    "const s=document.createElement('script');"
                    "s.src='https://unpkg.com/@ruffle-rs/ruffle';"
                    "document.head.appendChild(s);});"
                )
            page.goto(TARGET)
        else:
            page.goto(f"http://127.0.0.1:{PORT}/player")
        page.wait_for_timeout(3000)
        if URL_MODE and AUTO_PLAY:
            for selector in ("#playh .btnt", "#playh", "text=Play"):
                try:
                    page.click(selector, timeout=3000)
                    print("clicked play via", selector)
                    break
                except Exception:
                    continue
            page.wait_for_timeout(1500)
        elif not URL_MODE:
            page.mouse.click(VW / 2, VH / 2)  # give the Flash stage keyboard focus

        interval = 1.0 / FPS
        while True:
            t0 = time.time()
            while True:
                try:
                    apply_input(page, inputs.get_nowait())
                except queue.Empty:
                    break
            shot = page.screenshot(type="jpeg", quality=60)
            data = to_332(shot)
            with lock:
                seq += 1
                frames.append((seq, data))
            time.sleep(max(0, interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
