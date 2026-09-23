"""Verified in-place updates for the standard immutable Linux installation."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .build_identity import BUILD_INFO_ENV
from .services import AppRelease, download_app_release


LAUNCHER_ENV = "XC_BUDDY_LAUNCHER"


@dataclass(frozen=True)
class UpdateContext:
    data_home: Path
    config_home: Path
    bin_dir: Path
    launcher: Path


def standard_update_context() -> UpdateContext | None:
    """Return install locations only for the managed per-user launch path."""
    raw_build_info = os.environ.get(BUILD_INFO_ENV, "")
    if not raw_build_info:
        return None
    build_info = Path(raw_build_info).expanduser()
    current = build_info.parent
    if build_info.name != "build-info.json" or current.name != "current":
        return None
    data_root = current.parent
    if data_root.name != "xc-buddy" or not build_info.is_file():
        return None
    launcher = Path(
        os.environ.get(LAUNCHER_ENV, Path.home() / ".local" / "bin" / "xc-buddy")
    ).expanduser()
    if not launcher.is_file():
        return None
    config_home = Path(
        os.environ.get(
            "XC_BUDDY_CONFIG_HOME",
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"),
        )
    ).expanduser()
    return UpdateContext(
        data_home=data_root.parent,
        config_home=config_home,
        bin_dir=launcher.parent,
        launcher=launcher,
    )


def _safe_extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            raise RuntimeError("The Linux update archive is empty.")
        for member in members:
            relative = Path(member.name)
            if (
                member.name in {"", ".", ".."}
                or relative.is_absolute()
                or ".." in relative.parts
                or not (member.isfile() or member.isdir())
            ):
                raise RuntimeError("The Linux update archive contains an unsafe path.")
            try:
                (destination / relative).resolve().relative_to(destination.resolve())
            except ValueError as error:
                raise RuntimeError(
                    "The Linux update archive contains an unsafe path."
                ) from error
        bundle.extractall(destination)
    roots = {
        member.name.split("/", 1)[0]
        for member in members
        if member.name.split("/", 1)[0]
    }
    if len(roots) != 1:
        raise RuntimeError("The Linux update archive has an invalid layout.")
    artifact = destination / roots.pop()
    if not (artifact / "build-info.json").is_file():
        raise RuntimeError("The Linux update archive has no build manifest.")
    if not (artifact / "install-user.py").is_file():
        raise RuntimeError("The Linux update archive has no installer.")
    return artifact


def install_app_release(release: AppRelease, context: UpdateContext) -> Path:
    """Download, verify, install, and select an immutable release."""
    archive_data = download_app_release(release)
    with tempfile.TemporaryDirectory(prefix="xc-buddy-update-") as directory:
        temporary = Path(directory)
        archive = temporary / release.asset_name
        archive.write_bytes(archive_data)
        artifact = _safe_extract(archive, temporary / "extracted")
        base_python = Path(getattr(sys, "_base_executable", sys.executable))
        subprocess.run(
            [
                str(base_python),
                str(artifact / "install-user.py"),
                str(artifact),
                "--data-home",
                str(context.data_home),
                "--config-home",
                str(context.config_home),
                "--bin-dir",
                str(context.bin_dir),
            ],
            check=True,
        )
    return context.launcher
