#!/usr/bin/env python3
"""Install XC Buddy lifecycle hooks in the user-level Codex config."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil


HELPER_NAME = "xc-buddy-codex-notify.py"
EVENTS = {
    "UserPromptSubmit": "Notifying XC Buddy that Codex is working",
    "PermissionRequest": "Notifying XC Buddy that approval is needed",
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
    return parser.parse_args()


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


def install(codex_home: Path) -> tuple[Path, Path]:
    source_helper = Path(__file__).with_name(HELPER_NAME)
    if not source_helper.is_file():
        raise FileNotFoundError(f"XC Buddy helper not found: {source_helper}")

    hooks_directory = codex_home / "hooks"
    hooks_directory.mkdir(parents=True, exist_ok=True)
    installed_helper = hooks_directory / HELPER_NAME
    shutil.copy2(source_helper, installed_helper)

    hooks_path = codex_home / "hooks.json"
    config = load_hooks(hooks_path)
    command = f"/usr/bin/python3 {shlex.quote(str(installed_helper))}"
    for event, status_message in EVENTS.items():
        config["hooks"][event] = replace_xc_buddy_hook(
            config["hooks"].get(event), command, status_message
        )

    temporary_path = hooks_path.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    temporary_path.replace(hooks_path)
    return hooks_path, installed_helper


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        hooks_path, helper_path = install(codex_home.expanduser())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Unable to install XC Buddy hooks: {error}")
        return 1
    print(f"Installed XC Buddy hooks in {hooks_path}")
    print(f"Installed lifecycle helper at {helper_path}")
    print("Start a new Codex session, open /hooks, and trust the new user hook definitions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
