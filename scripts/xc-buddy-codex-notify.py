#!/usr/bin/env python3
"""Forward Codex lifecycle events to the local XC Buddy app."""

import json
import os
import sys
import urllib.request


def read_event() -> dict:
    if len(sys.argv) > 1:
        return json.loads(sys.argv[1])
    return json.load(sys.stdin)


def bridge_event(event: dict):
    raw_type = event.get("hook_event_name") or event.get("type") or event.get("event") or ""
    event_type = str(raw_type).lower()
    mapped_type = {
        "userpromptsubmit": "agent-turn-start",
        "permissionrequest": "approval-requested",
        "stop": "agent-turn-complete",
        "sessionend": "agent-turn-complete",
    }.get(event_type)
    if mapped_type is None:
        if event_type != "agent-turn-complete" and "approval" not in event_type:
            return None
        mapped_type = event_type

    payload = {"type": mapped_type}
    session_id = event.get("session_id") or event.get("thread-id")
    turn_id = event.get("turn_id") or event.get("turn-id")
    if session_id:
        payload["session-id"] = session_id
    if turn_id:
        payload["turn-id"] = turn_id
    return payload


def main() -> int:
    raw_event = {}
    try:
        raw_event = read_event()
        event = bridge_event(raw_event)
        if event is None:
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
    finally:
        # Stop hooks expect a JSON result on stdout. An empty object means that
        # XC Buddy observed the event without asking Codex to continue.
        if str(raw_event.get("hook_event_name", "")).lower() == "stop":
            print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
