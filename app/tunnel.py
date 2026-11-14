"""A public HTTPS address so Twilio can reach this laptop during a call.

Only /twilio/* is served through it (enforced in app/main.py).
"""
from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

import requests

from app.telephony import CONFIG

_proc: Optional[subprocess.Popen] = None
_CLOUDFLARE_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class TunnelError(Exception):
    pass


def start(port: int = 8000, timeout: float = 20.0) -> str:
    """Return a public URL, starting cloudflared or ngrok if needed."""
    if CONFIG.public_url:
        return CONFIG.public_url
    if shutil.which("cloudflared"):
        url = _start_cloudflared(port, timeout)
        CONFIG.tunnel = "cloudflared"
    elif shutil.which("ngrok"):
        url = _start_ngrok(port, timeout)
        CONFIG.tunnel = "ngrok"
    else:
        raise TunnelError(
            "No tunnel tool found. Install cloudflared (winget install Cloudflare.cloudflared) "
            "or ngrok, or set PUBLIC_URL yourself."
        )
    CONFIG.public_url = url
    return url


def _start_cloudflared(port: int, timeout: float) -> str:
    global _proc
    _proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines: queue.Queue[str] = queue.Queue()

    def pump():
        for line in _proc.stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if _proc.poll() is not None:
                break
            continue
        m = _CLOUDFLARE_URL.search(line)
        if m:
            return m.group(0)
    stop()
    raise TunnelError("cloudflared did not report a public URL in time.")


def _start_ngrok(port: int, timeout: float) -> str:
    global _proc
    _proc = subprocess.Popen(
        ["ngrok", "http", str(port), "--log", "stdout"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            tunnels = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=1).json().get("tunnels", [])
            for t in tunnels:
                if t.get("public_url", "").startswith("https://"):
                    return t["public_url"]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.5)
    stop()
    raise TunnelError("ngrok did not report a public URL in time. Is it logged in (ngrok config add-authtoken)?")


def stop() -> None:
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
    _proc = None
