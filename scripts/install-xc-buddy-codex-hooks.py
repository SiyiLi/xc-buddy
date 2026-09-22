#!/usr/bin/env python3
"""Install XC Buddy lifecycle hooks in the user-level Codex config."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys


HELPER_NAME = "xc-buddy-codex-notify.py"
EVENTS = {
    "UserPromptSubmit": "Notifying XC Buddy that Codex is working",
    "PermissionRequest": "Notifying XC Buddy that approval is needed",
    "PreToolUse": "Notifying XC Buddy that Codex resumed work",
    "Stop": "Notifying XC Buddy that Codex is done",
    "SessionEnd": "Returning XC Buddy to idle when Codex closes",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Codex config directory (defaults to CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--app-config",
        type=Path,
        help="XC Buddy config path (defaults to the current platform location)",
    )
    return parser.parse_args()


def default_app_config() -> Path:
    override = os.environ.get("XC_BUDDY_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/XC Buddy/config.toml"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "xc-buddy/config.toml"


def load_hooks(path: Path) -> dict:
    if not path.exists():
        return {
            "description": "User-level lifecycle hooks, including XC Buddy.",
            "hooks": {},
        }
    with path.open(encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict) or not isinstance(config.get("hooks"), dict):
        raise ValueError(f"{path} must contain a JSON object with a hooks object")
    return config


def load_bridge_token(path: Path) -> str:
    if not path.exists():
        return ""
    token_pattern = re.compile(
        r"^\s*codex_bridge_token\s*=\s*(\"(?:\\.|[^\"\\])*\"|'[^']*')\s*(?:#.*)?$"
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        match = token_pattern.match(line)
        if not match:
            continue
        value = match.group(1)
        return json.loads(value) if value.startswith('"') else value[1:-1]
    return ""


def remove_legacy_notify(path: Path) -> bool:
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    filtered = [
        line
        for line in lines
        if not (re.match(r"^\s*notify\s*=", line) and HELPER_NAME in line)
    ]
    if len(filtered) == len(lines):
        return False
    temporary_path = path.with_suffix(".toml.tmp")
    temporary_path.write_text("".join(filtered), encoding="utf-8")
    shutil.copymode(path, temporary_path)
    temporary_path.replace(path)
    return True


def is_xc_buddy_handler(handler) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and HELPER_NAME in str(handler.get("command", ""))
    )


def replace_xc_buddy_hook(groups, command: str, status_message: str) -> list:
    updated = []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                updated.append(group)
                continue
            handlers = [handler for handler in group["hooks"] if not is_xc_buddy_handler(handler)]
            if handlers:
                preserved = dict(group)
                preserved["hooks"] = handlers
                updated.append(preserved)
    updated.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 2,
                    "statusMessage": status_message,
                }
            ]
        }
    )
    return updated


def install(
    codex_home: Path, app_config: Path | None = None
) -> tuple[Path, Path, bool]:
    source_helper = Path(__file__).with_name(HELPER_NAME)
    if not source_helper.is_file():
        raise FileNotFoundError(f"XC Buddy helper not found: {source_helper}")

    hooks_directory = codex_home / "hooks"
    hooks_directory.mkdir(parents=True, exist_ok=True)
    installed_helper = hooks_directory / HELPER_NAME
    shutil.copy2(source_helper, installed_helper)
    installed_helper.chmod(0o700)

    hooks_path = codex_home / "hooks.json"
    config = load_hooks(hooks_path)
    bridge_token = load_bridge_token(app_config or default_app_config())
    command_parts = ["/usr/bin/env"]
    if bridge_token:
        command_parts.append(f"XC_BUDDY_CODEX_TOKEN={bridge_token}")
    command_parts.extend(["/usr/bin/python3", str(installed_helper)])
    command = shlex.join(command_parts)
    for event, status_message in EVENTS.items():
        config["hooks"][event] = replace_xc_buddy_hook(
            config["hooks"].get(event), command, status_message
        )

    temporary_path = hooks_path.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    temporary_path.chmod(0o600)
    temporary_path.replace(hooks_path)
    removed_legacy_notify = remove_legacy_notify(codex_home / "config.toml")
    return hooks_path, installed_helper, removed_legacy_notify


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        hooks_path, helper_path, removed_legacy_notify = install(
            codex_home.expanduser(),
            args.app_config.expanduser() if args.app_config else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Unable to install XC Buddy hooks: {error}")
        return 1
    print(f"Installed XC Buddy hooks in {hooks_path}")
    print(f"Installed lifecycle helper at {helper_path}")
    if removed_legacy_notify:
        print("Removed the legacy XC Buddy notify entry from config.toml")
    print("Start a new Codex session, open /hooks, and trust the new user hook definitions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
