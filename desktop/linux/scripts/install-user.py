#!/usr/bin/env python3
"""Install a verified XC Buddy artifact into an isolated per-user runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


APP_ICON_NAME = "ai.xc.buddy"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_digest(directory: Path) -> str:
    """Match the payload digest produced by build-user.py."""
    digest = hashlib.sha256()
    files = sorted(
        (
            path
            for path in directory.rglob("*")
            if path.is_file()
            and path not in {directory / "build-info.json", directory / "SHA256SUMS"}
        ),
        key=lambda path: path.relative_to(directory).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256(path).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def safe_name(value: str, field: str) -> None:
    candidate = Path(value)
    if value in {".", ".."} or candidate.is_absolute() or candidate.parts != (value,):
        raise RuntimeError(f"unsafe {field}")


def run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True, env=environment)


def verify_artifact(artifact: Path) -> dict[str, object]:
    checksums = artifact / "SHA256SUMS"
    manifest_path = artifact / "build-info.json"
    if not checksums.is_file() or not manifest_path.is_file():
        raise RuntimeError("artifact is missing SHA256SUMS or build-info.json")
    declared_paths: set[str] = set()
    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(f"invalid checksum entry: {line!r}")
        relative_path = Path(relative)
        if (
            not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in declared_paths
        ):
            raise RuntimeError(f"unsafe checksum path: {relative}")
        candidate = artifact / relative
        try:
            candidate.resolve().relative_to(artifact.resolve())
        except ValueError as error:
            raise RuntimeError(f"unsafe artifact path: {relative}") from error
        if not candidate.is_file() or sha256(candidate) != digest:
            raise RuntimeError(f"checksum mismatch: {relative}")
        declared_paths.add(relative)
    actual_paths = {
        path.relative_to(artifact).as_posix()
        for path in artifact.rglob("*")
        if path.is_file() and path != checksums
    }
    if declared_paths != actual_paths:
        raise RuntimeError("checksum coverage does not match artifact contents")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise RuntimeError("invalid build-info.json") from error
    if not isinstance(manifest, dict):
        raise RuntimeError("build-info.json must contain an object")
    for field in (
        "build_id",
        "artifact_content_sha256",
        "runtime_requirements_sha256",
        "version",
        "wheel_filename",
        "wheel_sha256",
    ):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise RuntimeError(f"build-info.json is missing {field}")
    safe_name(manifest["build_id"], "build_id")
    safe_name(manifest["wheel_filename"], "wheel_filename")
    artifact_digest = content_digest(artifact)
    if manifest["artifact_content_sha256"] != artifact_digest:
        raise RuntimeError("artifact payload does not match build-info.json")
    expected_build_id = f"{manifest['version']}-{artifact_digest[:12]}"
    if manifest["build_id"] != expected_build_id:
        raise RuntimeError("build ID does not match artifact payload")
    wheel = artifact / "payload" / manifest["wheel_filename"]
    if not wheel.is_file() or sha256(wheel) != manifest["wheel_sha256"]:
        raise RuntimeError("wheel does not match build-info.json")
    if (
        sha256(artifact / "runtime-requirements.lock")
        != manifest["runtime_requirements_sha256"]
    ):
        raise RuntimeError("runtime dependency lock does not match build-info.json")
    return manifest


def write_file(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)


def desktop_command(path: Path) -> str:
    value = str(path)
    for original, escaped in (
        ("\\", "\\\\"),
        ('"', '\\"'),
        ("$", "\\$"),
        ("`", "\\`"),
    ):
        value = value.replace(original, escaped)
    return f'"{value}"'


def install_release(
    artifact: Path, manifest: dict[str, object], data_root: Path
) -> Path:
    build_id = str(manifest["build_id"])
    releases = data_root / "releases"
    release = releases / build_id
    if release.exists():
        installed_info = release / "build-info.json"
        executable = release / "venv" / "bin" / "xc-buddy"
        if (
            installed_info.is_file()
            and executable.is_file()
            and sha256(installed_info) == sha256(artifact / "build-info.json")
        ):
            return release
        raise RuntimeError(f"existing release does not match artifact: {release}")

    releases.mkdir(parents=True, exist_ok=True)
    release.mkdir(mode=0o700)
    try:
        venv = release / "venv"
        try:
            run([sys.executable, "-m", "venv", str(venv)])
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                "could not create the isolated runtime; install python3-venv"
            ) from error
        python = venv / "bin" / "python"
        wheelhouse = artifact / "wheelhouse"
        runtime_lock = artifact / "runtime-requirements.lock"
        wheel = artifact / "payload" / str(manifest["wheel_filename"])
        run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--no-deps",
                "--requirement",
                str(runtime_lock),
            ]
        )
        run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--no-index",
                "--no-deps",
                str(wheel),
            ]
        )
        run([str(python), "-m", "pip", "--no-cache-dir", "check"])
        shutil.copy2(artifact / "build-info.json", release)
        environment = os.environ.copy()
        environment["XC_BUDDY_BUILD_INFO"] = str(release / "build-info.json")
        output = subprocess.check_output(
            [str(venv / "bin" / "xc-buddy"), "--version"],
            text=True,
            env=environment,
        ).strip()
        expected = f"XC Buddy {manifest['version']} ({build_id})"
        if output != expected:
            raise RuntimeError(f"installed artifact identity mismatch: {output!r}")
    except Exception:
        shutil.rmtree(release)
        raise
    return release


def select_release(data_root: Path, release: Path) -> None:
    current = data_root / "current"
    pending = data_root / ".current.next"
    pending.unlink(missing_ok=True)
    pending.symlink_to(os.path.relpath(release, data_root))
    pending.replace(current)


def install_app_icon(artifact: Path, data_home: Path) -> str:
    for filename, size in (("AppIcon.png", "512x512"), ("AppIcon-256.png", "256x256")):
        source = artifact / filename
        if not source.is_file():
            raise RuntimeError(f"artifact is missing {filename} from the macOS icon")
        destination = (
            data_home / "icons" / "hicolor" / size / "apps" / f"{APP_ICON_NAME}.png"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    return APP_ICON_NAME


def refresh_icon_cache(data_home: Path) -> None:
    executable = shutil.which("gtk-update-icon-cache")
    if executable:
        subprocess.run(
            [
                executable,
                "--force",
                "--ignore-theme-index",
                str(data_home / "icons" / "hicolor"),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def refresh_desktop_database(data_home: Path) -> None:
    executable = shutil.which("update-desktop-database")
    if executable:
        subprocess.run(
            [executable, str(data_home / "applications")],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def install_launchers(
    artifact: Path, data_root: Path, bin_dir: Path, config_home: Path, icon_name: str
) -> Path:
    launcher = bin_dir / "xc-buddy"
    write_file(
        launcher,
        "#!/bin/sh\n"
        "set -eu\n"
        "# XC Buddy managed launcher.\n"
        f"app_root={shlex.quote(str(data_root))}\n"
        'export XC_BUDDY_LAUNCHER="$0"\n'
        'export XC_BUDDY_BUILD_INFO="$app_root/current/build-info.json"\n'
        'exec "$app_root/current/venv/bin/xc-buddy" "$@"\n',
        0o755,
    )
    entry = (artifact / "ai.xc.buddy.desktop").read_text(encoding="utf-8")
    if "Exec=xc-buddy" not in entry:
        raise RuntimeError("desktop entry does not contain Exec=xc-buddy")
    if "Icon=__XC_BUDDY_APP_ICON__" not in entry:
        raise RuntimeError("desktop entry does not contain its icon placeholder")
    configured_entry = entry.replace(
        "Exec=xc-buddy", f"Exec={desktop_command(launcher)}", 1
    )
    configured_entry = configured_entry.replace(
        "Icon=__XC_BUDDY_APP_ICON__", f"Icon={icon_name}", 1
    )
    data_home = data_root.parent
    write_file(
        data_home / "applications" / "ai.xc.buddy.desktop", configured_entry, 0o644
    )
    write_file(
        config_home / "autostart" / "ai.xc.buddy.desktop", configured_entry, 0o644
    )
    return launcher


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifact", type=Path, help="artifact directory from build-user.py"
    )
    parser.add_argument(
        "--data-home",
        type=Path,
        help="override XDG data home; useful for isolated installation tests",
    )
    parser.add_argument(
        "--config-home",
        type=Path,
        help="override XDG config home; useful for isolated installation tests",
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        help="override the per-user launcher directory; useful for isolated tests",
    )
    args = parser.parse_args()

    artifact = args.artifact.expanduser().resolve()
    manifest = verify_artifact(artifact)
    data_home = (
        args.data_home
        or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    ).expanduser()
    config_home = (
        args.config_home
        or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    ).expanduser()
    bin_dir = (args.bin_dir or Path.home() / ".local" / "bin").expanduser()
    data_root = data_home / "xc-buddy"
    release = install_release(artifact, manifest, data_root)
    icon_name = install_app_icon(artifact, data_home)
    refresh_icon_cache(data_home)
    select_release(data_root, release)
    launcher = install_launchers(artifact, data_root, bin_dir, config_home, icon_name)
    refresh_desktop_database(data_home)
    print(f"Installed XC Buddy {manifest['version']} ({manifest['build_id']})")
    print(f"Launcher: {launcher}")
    print(f"Run: {launcher} --version")


if __name__ == "__main__":
    main()
