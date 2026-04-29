"""Example alert handler script.

Configured in `config.yaml` under alerts.handlers as:

    - type: script
      command: ["python", "examples/alert_action.py"]

The anomaly is sent on stdin as a single JSON object. Anything you write to
stdout/stderr is captured and discarded by netmon (the script's exit code
matters; non-zero is logged as a warning).

This example just appends the alert to a flat file. Replace with whatever
your environment needs: a Slack post, a PagerDuty trigger, an iptables rule,
a service restart. Keep it idempotent — netmon will not retry on failure.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

OUT = Path("logs") / "alerts.jsonl"


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"bad payload: {e}", file=sys.stderr)
        return 2

    OUT.parent.mkdir(parents=True, exist_ok=True)
    record = {"received_at": datetime.utcnow().isoformat() + "Z", "alert": payload}
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
