# Codex integration

XC Buddy listens only on `127.0.0.1` (default port `17321`). It never binds a wildcard or LAN interface. Configure the same bearer token in the XC Buddy settings/config and the helper environment.

Install XC Buddy's Codex lifecycle hooks at user level so they run for Codex
sessions in every folder:

```sh
/usr/bin/python3 scripts/install-xc-buddy-codex-hooks.py
```

The installer copies the lifecycle helper to `~/.codex/hooks/`, then merges XC
Buddy's event handlers into `~/.codex/hooks.json` without replacing unrelated
global hooks. It reuses `codex_bridge_token` from XC Buddy's application config
and removes an older XC Buddy `notify = [...]` entry to avoid duplicate events.
Running it again updates the installed helper and XC Buddy handlers. The global
hooks map:

- `UserPromptSubmit` -> Working
- `PermissionRequest` -> Approval needed
- `PreToolUse` -> Working
- `Stop` -> Idle and `Codex done`
- `SessionEnd` -> Idle and `Codex done` as a process-exit fallback

Remove any repository-local XC Buddy `.codex/hooks.json`; Codex runs all
matching hook sources, so leaving it enabled causes duplicate completion events.

Start a new Codex session in any folder, open `/hooks`, and review and trust the
user-level XC Buddy hook definitions. Codex records trust against the exact hook
hash, so repeat that review after changing `~/.codex/hooks.json`. Keep XC Buddy
running while Codex works because the helper exits quietly when XC Buddy is not
listening.

Equivalent XC Buddy config:

```toml
codex_bridge_port = 17321
codex_bridge_token = "replace-with-the-token-from-xc-buddy"
codex_success_chime = true
```

Codex lifecycle hooks pass JSON on standard input. The helper reduces it to the
session ID, turn ID, and lifecycle event before sending it to XC Buddy; prompt
text and transcripts are not forwarded. The helper uses only Python's standard
library, times out after 0.5 seconds, and exits successfully when XC Buddy is
not running.

The `UserPromptSubmit` hook aligns XC Buddy with Codex even when a prompt is
typed manually. Auto-enter still marks the bridge as Working immediately, and
the lifecycle hook confirms the same state. An approval starts a one-minute
timer. Another approval refreshes that timer, while `PreToolUse` cancels it and
returns the Stick to Working. If the full minute expires, the Stick plays the
configured Codex chime and remains on Approval needed until a tool call starts.
The `Stop` hook changes it back to Idle and sends `codex_done` to the connected
stick. Completion and error notices are live-only and are not replayed after a
Stick reconnect. `SessionEnd` provides the same reset when a CLI process exits
before a normal Stop event, so XC Buddy is not left showing Working after Codex
has closed.

## Relay receiver lifecycle ownership

Receiver XC Buddy is the StickS3's single lifecycle owner. Relay events never
control the Stick directly: XC Buddy applies both normalized relay events and
local Stick voice-input events through one shared lifecycle handler for state,
display, approval timing, chimes, and BLE output.

In Receiver mode, local voice input behaves exactly as it does in Disabled
mode. `pending_confirmation` is temporary and must be replaced when the paste
completes: a paste submitted to the frontmost Codex app changes the Stick to
Working; a paste to any non-Codex app returns it to Ready. These local
transitions intentionally replace the relay state currently displayed on the
Stick, but Receiver never publishes them to the relay. Only Sender publishes
normalized lifecycle events.

The helper remains compatible with the legacy top-level `notify` command,
which passes an `agent-turn-complete` JSON object as the helper's first
argument. The lifecycle hooks are preferred because they expose both start and
stop state.

## Receiver contract

```http
POST /codex/events HTTP/1.1
Host: 127.0.0.1:17321
Authorization: Bearer <token>
Content-Type: application/json

{"type":"agent-turn-start"}
```

Lifecycle mapping:

| Event type | XC Buddy/device state |
| --- | --- |
| `agent-turn-start` | working |
| contains `approval` | approval needed |
| `tool-call-start` | working and cancel approval timer |
| contains `error` or `fail` | error |
| contains `complete` or `done` | ready / done |
| any other accepted event | working |

Firmware `0.1.3` and later displays `Codex done` and its version for ten
seconds after `agent-turn-complete`, then returns to the Ready screen. Firmware
`0.1.2` and later plays the completion chime; `0.1.3` adds the delayed approval
notification. Codex chimes can be disabled in XC Buddy Settings under Codex
Bridge.

The listener requires the bearer token when `codex_bridge_token` is non-empty. Leaving it empty is supported for local development but is not recommended. The bridge does not scrape terminal output or send data outside the machine.
