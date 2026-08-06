#!/usr/bin/env python3
"""Run NapCat while keeping message bodies and credentials out of journald."""

import os
import re
import signal
import subprocess
import sys
import time


_SEVERITY_PATTERN = re.compile(
    r"(?i)(\[(?:warn|warning|error|fatal|critical)\]|"
    r"\b(?:warning|error|fatal|critical|traceback|exception|unhandled|failed|failure)\b)"
)
_LIFECYCLE_PATTERN = re.compile(
    r"(?i)(napcat|onebot|websocket).{0,80}"
    r"(start|started|ready|listen|listening|connect|connected|loaded)"
)
_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:access_?token|token|authorization|password|passkey)[\"']?"
    r"\s*[:=]\s*[\"']?)([^\"'\s,&}]+)"
)
_LONG_ID_PATTERN = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PAYLOAD_PATTERN = re.compile(
    r"(?i)(接收\s*<-|发送\s*->|\[CQ:|"
    r"[\"'](?:raw_message|message|content|jsonStr)[\"']\s*:)"
)


def should_emit(line):
    return bool(_SEVERITY_PATTERN.search(line) or _LIFECYCLE_PATTERN.search(line))


def sanitize_line(line, limit=2000):
    text = _CONTROL_PATTERN.sub("", str(line).replace("\r", "").rstrip("\n"))
    text = _SECRET_PATTERN.sub(r"\1<redacted>", text)
    text = _LONG_ID_PATTERN.sub("<id>", text)
    payload = _PAYLOAD_PATTERN.search(text)
    if payload:
        text = text[:payload.start()].rstrip() + " <payload redacted>"
    if len(text) > limit:
        text = text[:limit] + "...<truncated>"
    return text


def _write(message):
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _command_from_environment():
    account = os.getenv("NAPCAT_QUICK_ACCOUNT", "").strip()
    if not account.isdigit():
        raise RuntimeError("NAPCAT_QUICK_ACCOUNT must be set to a numeric account id")
    binary = os.getenv("NAPCAT_BINARY", "/root/Napcat/opt/QQ/qq")
    if not os.path.isfile(binary):
        raise RuntimeError("NapCat QQ binary does not exist")
    return ["/usr/bin/xvfb-run", "-a", binary, "--no-sandbox", "-q", account]


def run():
    try:
        command = _command_from_environment()
    except RuntimeError as error:
        _write("[napcat-log-filter] ERROR: {}".format(error))
        return 2

    child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    forwarded_signal = [None]

    def forward_signal(signum, _frame):
        forwarded_signal[0] = signum
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)
    _write("[napcat-log-filter] started")

    suppressed = 0
    last_summary = time.monotonic()
    assert child.stdout is not None
    for raw_line in child.stdout:
        line = sanitize_line(raw_line)
        if should_emit(line):
            _write(line)
        else:
            suppressed += 1
        now = time.monotonic()
        if suppressed and (suppressed >= 5000 or now - last_summary >= 300):
            _write("[napcat-log-filter] suppressed {} non-warning lines".format(suppressed))
            suppressed = 0
            last_summary = now

    return_code = child.wait()
    if suppressed:
        _write("[napcat-log-filter] suppressed {} non-warning lines".format(suppressed))
    _write("[napcat-log-filter] child exited with status {}".format(return_code))
    if forwarded_signal[0] and return_code in {
        -forwarded_signal[0], 128 + forwarded_signal[0]
    }:
        return 0
    return return_code


if __name__ == "__main__":
    raise SystemExit(run())
