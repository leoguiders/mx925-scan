#!/usr/bin/env python3
"""Docker health check for the scan daemon.

Healthy:   the daemon's main loop is alive and no operational problem is
           reported - also while the printer is switched off or unreachable.
Unhealthy: the state file is missing or stale (main loop stuck or dead), or
           the daemon reports a problem (output directory not writable,
           invalid config.yaml, processing failed, worker dead or hung, ...).

The printed line shows up in `docker inspect` under State.Health.
"""
import json
import os
import sys
import time

HEALTH_FILE = os.environ.get("HEALTH_FILE", "/tmp/health.json")
# Longest time the main loop may go without an update (seconds). Opening a
# BJNP connection can block for BJNP_TIMEOUT, the back-off for an unreachable
# printer is 60 s, so this leaves plenty of headroom.
MAX_AGE = float(os.environ.get("HEALTH_MAX_AGE", "300"))


def main():
    try:
        age = time.time() - os.stat(HEALTH_FILE).st_mtime
        with open(HEALTH_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError) as e:
        print(f"UNHEALTHY - no health state ({e})")
        return 1

    printer = state.get("printer", "?")
    if printer == "unreachable":
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(state.get("since", 0)))
        printer = f"unreachable since {since}"

    if age > MAX_AGE:
        print(f"UNHEALTHY - main loop not responding for {age:.0f} s (printer: {printer})")
        return 1
    problems = state.get("problems") or {}
    if problems:
        print("UNHEALTHY - " + "; ".join(f"{k}: {v}" for k, v in problems.items()))
        return 1
    print(f"OK - printer {printer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
