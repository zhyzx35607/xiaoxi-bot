import asyncio
import glob
import json
import os
import re
import time
import urllib.parse
import uuid
from pathlib import Path

import websockets


_CONFIG_DIR = Path(os.getenv(
    "NAPCAT_CONFIG_DIR",
    "/opt/napcat/opt/QQ/resources/app/app_launcher/napcat/config",
))


def _default_config_path():
    account = os.getenv("NAPCAT_QUICK_ACCOUNT", "").strip()
    if account.isdigit():
        return _CONFIG_DIR / "onebot11_{}.json".format(account)
    candidates = sorted(Path(path) for path in glob.glob(str(_CONFIG_DIR / "onebot11_*.json")))
    return candidates[0] if len(candidates) == 1 else _CONFIG_DIR / "onebot11.json"


CONFIG_PATH = _default_config_path()
STATE_PATH = Path(os.getenv(
    "NAPCAT_WATCHDOG_STATE_PATH",
    "/run/napcat-login-watchdog/state.json",
))
RESTART_REQUEST_PATH = Path(os.getenv(
    "NAPCAT_RESTART_REQUEST_PATH",
    "/run/napcat-login-watchdog/restart.request",
))
PREFERRED_PORT = int(os.getenv("NAPCAT_WATCHDOG_PORT", "3001"))
FAILURES_BEFORE_RESTART = 2
RESTART_COOLDOWN_SECONDS = 300
CHECK_INTERVAL_SECONDS = max(30, int(os.getenv("NAPCAT_WATCHDOG_INTERVAL", "30")))


class NapCatConfigError(RuntimeError):
    """NapCat OneBot config cannot be read or parsed; restarting cannot fix it."""


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"failures": 0, "last_restart": 0}


def save_state(state):
    STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary_path, STATE_PATH)


def request_restart():
    RESTART_REQUEST_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = RESTART_REQUEST_PATH.with_suffix(".tmp")
    temporary_path.write_text(str(int(time.time())), encoding="ascii")
    os.replace(temporary_path, RESTART_REQUEST_PATH)


def get_websocket_url():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NapCatConfigError(
            "cannot read OneBot config {}: {}".format(CONFIG_PATH, error)) from error
    servers = config.get("network", {}).get("websocketServers", [])
    enabled_servers = [server for server in servers if server.get("enable")]
    if not enabled_servers:
        raise NapCatConfigError("no enabled OneBot WebSocket server")
    server = next(
        (item for item in enabled_servers if int(item.get("port", 0)) == PREFERRED_PORT),
        enabled_servers[0],
    )
    host = server.get("host") or "127.0.0.1"
    try:
        port = int(server["port"])
    except (KeyError, TypeError, ValueError) as error:
        raise NapCatConfigError("OneBot WebSocket server has no valid port") from error
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


def _safe_error_text(error):
    text = str(error)
    text = re.sub(r"(?i)(access_token=)[^&\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(token=)[^&\s]+", r"\1<redacted>", text)
    return text[:240]


async def run_check():
    state = load_state()
    now = int(time.time())
    try:
        online = await check_online()
        reason = "OneBot reports offline" if not online else ""
    except NapCatConfigError as error:
        # A broken NapCat config cannot be fixed by restarting NapCat;
        # alert without feeding the restart counter.
        print(f"NapCat config error (not counted towards restart): {_safe_error_text(error)}")
        return False
    except Exception as error:
        online = False
        reason = f"health check failed: {type(error).__name__}: {_safe_error_text(error)}"

    if online:
        if state.get("failures", 0):
            print("NapCat login recovered; failure counter reset")
        state["failures"] = 0
        save_state(state)
        return True

    state["failures"] = int(state.get("failures", 0)) + 1
    print(f"NapCat login unhealthy ({state['failures']}): {reason}")
    last_restart = int(state.get("last_restart", 0))
    cooldown_remaining = RESTART_COOLDOWN_SECONDS - (now - last_restart)
    if state["failures"] < FAILURES_BEFORE_RESTART:
        save_state(state)
        return False
    if cooldown_remaining > 0:
        print(f"Restart suppressed by cooldown ({cooldown_remaining}s remaining)")
        save_state(state)
        return False

    request_restart()
    state["last_restart"] = now
    state["failures"] = 0
    save_state(state)
    print("Requested napcat.service restart through systemd path unit")
    return False


async def run_loop():
    while True:
        await run_check()
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def main():
    loop_enabled = os.getenv("NAPCAT_WATCHDOG_LOOP", "").lower() in {
        "1", "true", "yes", "on",
    }
    asyncio.run(run_loop() if loop_enabled else run_check())


if __name__ == "__main__":
    main()
