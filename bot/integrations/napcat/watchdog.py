import asyncio
import json
import os
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path

import websockets


CONFIG_PATH = Path(
    "/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config/"
    "onebot11_3127014580.json"
)
STATE_PATH = Path("/run/napcat-login-watchdog.json")
PREFERRED_PORT = int(os.getenv("NAPCAT_WATCHDOG_PORT", "3001"))
FAILURES_BEFORE_RESTART = 2
RESTART_COOLDOWN_SECONDS = 300


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"failures": 0, "last_restart": 0}


def save_state(state):
    temporary_path = STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary_path, STATE_PATH)


def get_websocket_url():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    servers = config.get("network", {}).get("websocketServers", [])
    enabled_servers = [server for server in servers if server.get("enable")]
    if not enabled_servers:
        raise RuntimeError("no enabled OneBot WebSocket server")
    server = next(
        (item for item in enabled_servers if int(item.get("port", 0)) == PREFERRED_PORT),
        enabled_servers[0],
    )
    host = server.get("host") or "127.0.0.1"
    port = int(server["port"])
    token = server.get("token") or ""
    url = f"ws://{host}:{port}"
    if token:
        url += "?access_token=" + urllib.parse.quote(token)
    return url


async def check_online():
    async with websockets.connect(
        get_websocket_url(), open_timeout=5, close_timeout=2,
        ping_interval=None, max_size=1024 * 1024,
    ) as websocket:
        echo = "watchdog-" + uuid.uuid4().hex[:10]
        await websocket.send(json.dumps(
            {"action": "get_status", "params": {}, "echo": echo}
        ))
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            timeout = max(0.1, deadline - time.monotonic())
            response = json.loads(await asyncio.wait_for(websocket.recv(), timeout))
            if response.get("echo") != echo:
                continue
            data = response.get("data") or {}
            return bool(
                response.get("status") == "ok"
                and data.get("online")
                and data.get("good")
            )
    return False


def main():
    state = load_state()
    now = int(time.time())
    try:
        online = asyncio.run(check_online())
        reason = "OneBot reports offline" if not online else ""
    except Exception as error:
        online = False
        reason = f"health check failed: {type(error).__name__}: {error}"

    if online:
        if state.get("failures", 0):
            print("NapCat login recovered; failure counter reset")
        state["failures"] = 0
        save_state(state)
        return

    state["failures"] = int(state.get("failures", 0)) + 1
    print(f"NapCat login unhealthy ({state['failures']}): {reason}")
    last_restart = int(state.get("last_restart", 0))
    cooldown_remaining = RESTART_COOLDOWN_SECONDS - (now - last_restart)
    if state["failures"] < FAILURES_BEFORE_RESTART:
        save_state(state)
        return
    if cooldown_remaining > 0:
        print(f"Restart suppressed by cooldown ({cooldown_remaining}s remaining)")
        save_state(state)
        return

    result = subprocess.run(
        ["systemctl", "restart", "napcat.service"],
        check=False,
        timeout=90,
    )
    state["last_restart"] = now
    state["failures"] = 0
    save_state(state)
    if result.returncode:
        raise SystemExit(result.returncode)
    print("Restarted napcat.service to trigger automatic login")


if __name__ == "__main__":
    main()
