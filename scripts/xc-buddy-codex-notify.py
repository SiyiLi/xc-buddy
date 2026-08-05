#!/usr/bin/env python3
"""Forward selected Codex notify events to the local XC Buddy app."""

import json
import os
import sys
import urllib.request


def main() -> int:
    try:
        event = json.loads(sys.argv[1])
        event_type = str(event.get("type") or event.get("event") or "").lower()
        if event_type != "agent-turn-complete" and "approval" not in event_type:
            return 0
        port = int(os.environ.get("XC_BUDDY_CODEX_PORT", "17321"))
        if not 1 <= port <= 65535:
            return 0
        body = json.dumps(event, separators=(",", ":")).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/codex/events",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        token = os.environ.get("XC_BUDDY_CODEX_TOKEN", "")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=0.5):
            pass
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
