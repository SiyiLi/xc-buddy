"""Installed-artifact identity for the Linux client."""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


BUILD_INFO_ENV = "XC_BUDDY_BUILD_INFO"
_BUILD_FIELDS = ("build_id", "source_revision", "wheel_sha256")


def package_version() -> str:
    try:
        return version("xc-buddy")
    except PackageNotFoundError:
        return "unknown"


def load_build_info(path: Path | str | None = None) -> dict[str, str]:
    raw_path = path or os.environ.get(BUILD_INFO_ENV)
    if not raw_path:
        return {}
    candidate = Path(raw_path)
    try:
        payload: Any = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        field: value
        for field in _BUILD_FIELDS
        if isinstance(value := payload.get(field), str) and value
    }


def version_text(path: Path | str | None = None) -> str:
    build_id = load_build_info(path).get("build_id")
    suffix = f" ({build_id})" if build_id else ""
    return f"XC Buddy {package_version()}{suffix}"


def doctor_build_text(path: Path | str | None = None) -> str:
    info = load_build_info(path)
    build_id = info.get("build_id")
    if not build_id:
        return "source or untracked install"
    revision = info.get("source_revision", "unknown")
    return f"{build_id} ({revision})"
