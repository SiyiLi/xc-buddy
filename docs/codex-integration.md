# Codex integration

XC Buddy listens only on `127.0.0.1` (default port `17321`). It never binds a wildcard or LAN interface. Configure the same bearer token in the XC Buddy settings/config and the helper environment.

Add this to `~/.codex/config.toml` manually; XC Buddy does not edit Codex configuration:

```toml
notify = ["env", "XC_BUDDY_CODEX_TOKEN=replace-with-the-token-from-xc-buddy", "/usr/bin/python3", "/absolute/path/to/xc-buddy/scripts/xc-buddy-codex-notify.py"]
```

Start a new Codex session after changing the file; Codex reads this setting at startup.

Equivalent XC Buddy config:

```toml
codex_bridge_port = 17321
codex_bridge_token = "replace-with-the-token-from-xc-buddy"
```

Codex passes a JSON object as the helper's first argument. The helper forwards `agent-turn-complete` and event types containing `approval`, uses only Python's standard library, times out after 0.5 seconds, and exits successfully when XC Buddy is not running.

## Receiver contract

```http
POST /codex/events HTTP/1.1
Host: 127.0.0.1:17321
Authorization: Bearer <token>
Content-Type: application/json

{"type":"agent-turn-complete"}
```

Lifecycle mapping:

| Event type | XC Buddy/device state |
| --- | --- |
| contains `approval` | approval needed |
| contains `error` or `fail` | error |
| contains `complete` or `done` | ready / done |
| any other accepted event | working |

Firmware `0.1.1` displays `Codex done` and its version for five seconds after
`agent-turn-complete`, then returns to the Ready screen.

The listener requires the bearer token when `codex_bridge_token` is non-empty. Leaving it empty is supported for local development but is not recommended. The bridge does not scrape terminal output or send data outside the machine.
