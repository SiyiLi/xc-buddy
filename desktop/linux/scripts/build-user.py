#!/usr/bin/env python3
"""Build one immutable, personal XC Buddy Linux release artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from email import message_from_bytes
from pathlib import Path


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True, cwd=cwd, env=environment)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        (path for path in directory.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(directory).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256(path).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = message_from_bytes(archive.read(metadata_name))
    version = metadata.get("Version")
    if not version:
        raise RuntimeError(f"wheel has no package version: {wheel}")
    return version


def git_value(repository: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository), *arguments], text=True
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "unknown"


def source_date_epoch(repository: Path) -> str:
    value = git_value(repository, "show", "-s", "--format=%ct", "HEAD")
    return value if value.isdecimal() else "0"


def build_environment(path: Path) -> Path:
    python = path / "bin" / "python"
    if not python.is_file():
        try:
            run([sys.executable, "-m", "venv", str(path)])
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                "could not create the isolated build environment; install python3-venv"
            ) from error
    run([str(python), "-m", "pip", "install", "--upgrade", "build"])
    return python


def copy_clean_project(source: Path, destination: Path) -> None:
    """Keep PEP 517 metadata and build directories out of the working tree."""
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".mypy_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "*.egg-info",
            "build",
            "dist",
        ),
    )


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height) if width and height else None


def extract_linux_icon(
    source: Path, destination: Path, target_size: tuple[int, int] = (512, 512)
) -> None:
    """Extract an exact PNG raster embedded in the macOS ICNS source icon."""
    data = source.read_bytes()
    if len(data) < 8 or data[:4] != b"icns":
        raise RuntimeError(f"not an ICNS icon: {source}")
    total_length = int.from_bytes(data[4:8], "big")
    if total_length != len(data):
        raise RuntimeError(f"invalid ICNS length: {source}")

    candidates: list[tuple[tuple[int, int], bytes]] = []
    offset = 8
    while offset < total_length:
        if offset + 8 > total_length:
            raise RuntimeError(f"truncated ICNS chunk: {source}")
        chunk_length = int.from_bytes(data[offset + 4 : offset + 8], "big")
        if chunk_length < 8 or offset + chunk_length > total_length:
            raise RuntimeError(f"invalid ICNS chunk: {source}")
        payload = data[offset + 8 : offset + chunk_length]
        if dimensions := png_dimensions(payload):
            candidates.append((dimensions, payload))
        offset += chunk_length
    if not candidates:
        raise RuntimeError(f"ICNS icon contains no PNG image: {source}")

    preferred = [candidate for candidate in candidates if candidate[0] == target_size]
    if not preferred:
        raise RuntimeError(
            f"ICNS icon contains no {target_size[0]}px PNG image: {source}"
        )
    _dimensions, icon = preferred[0]
    destination.write_bytes(icon)


def write_checksums(directory: Path) -> None:
    files = sorted(
        (
            path
            for path in directory.rglob("*")
            if path.is_file() and path.name != "SHA256SUMS"
        ),
        key=lambda path: path.relative_to(directory).as_posix(),
    )
    lines = [
        f"{sha256(path)}  {path.relative_to(directory).as_posix()}\n" for path in files
    ]
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")


def write_release_archive(artifact: Path, output_dir: Path, version: str) -> Path:
    python_tag = f"py{sys.version_info.major}{sys.version_info.minor}"
    architecture = platform.machine().lower()
    archive = (
        output_dir / f"xc-buddy-linux-{architecture}-{python_tag}-{version}.tar.gz"
    )
    temporary = archive.with_name(f".{archive.name}.tmp")
    temporary.unlink(missing_ok=True)
    with tarfile.open(temporary, "w:gz") as bundle:
        bundle.add(artifact, arcname=artifact.name)
    temporary.replace(archive)
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return archive


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    linux_dir = script_dir.parent
    repository = linux_dir.parent.parent
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-env",
        type=Path,
        default=cache_root / "xc-buddy" / "build-venv",
        help="isolated Python environment used only to build artifacts",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=linux_dir / "dist" / "linux",
        help="directory that receives immutable release artifact directories",
    )
    args = parser.parse_args()

    os.environ.setdefault("PIP_NO_CACHE_DIR", "1")
    build_python = build_environment(args.build_env.expanduser())
    output_dir = args.out_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_epoch = source_date_epoch(repository)
    build_environment_variables = os.environ.copy()
    build_environment_variables["SOURCE_DATE_EPOCH"] = source_epoch

    with tempfile.TemporaryDirectory(prefix=".xc-buddy-build-", dir=output_dir) as temp:
        build_project = Path(temp) / "project"
        stage = Path(temp) / "artifact"
        payload_dir = stage / "payload"
        wheelhouse_dir = stage / "wheelhouse"
        copy_clean_project(linux_dir, build_project)
        mac_icon = repository / "desktop" / "macos" / "Resources" / "AppIcon.icns"
        if not mac_icon.is_file():
            raise RuntimeError(f"missing macOS application icon: {mac_icon}")
        shutil.copy2(mac_icon, build_project / "app" / "icons" / "AppIcon.icns")
        linux_icon = build_project / "app" / "icons" / "AppIcon.png"
        extract_linux_icon(mac_icon, linux_icon)
        grid_icon = Path(temp) / "AppIcon-256.png"
        extract_linux_icon(mac_icon, grid_icon, (256, 256))
        payload_dir.mkdir(parents=True)
        wheelhouse_dir.mkdir()

        run(
            [str(build_python), "-m", "build", "--wheel", "--outdir", str(payload_dir)],
            cwd=build_project,
            environment=build_environment_variables,
        )
        wheels = list(payload_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected exactly one wheel, found {len(wheels)}")
        wheel = wheels[0]
        version = wheel_version(wheel)
        wheel_digest = sha256(wheel)

        run(
            [
                str(build_python),
                "-m",
                "pip",
                "download",
                "--no-cache-dir",
                "--dest",
                str(wheelhouse_dir),
                "--only-binary=:all:",
                "--requirement",
                str(linux_dir / "requirements-linux.lock"),
            ]
        )
        runtime_lock = stage / "runtime-requirements.lock"
        shutil.copy2(linux_dir / "requirements-linux.lock", runtime_lock)
        shutil.copy2(linux_dir / "assets" / "ai.xc.buddy.desktop", stage)
        shutil.copy2(script_dir / "install-user.py", stage)
        shutil.copy2(mac_icon, stage / "AppIcon.icns")
        shutil.copy2(linux_icon, stage / "AppIcon.png")
        shutil.copy2(grid_icon, stage / "AppIcon-256.png")
        artifact_digest = content_digest(stage)
        build_id = f"{version}-{artifact_digest[:12]}"

        manifest = {
            "artifact_content_sha256": artifact_digest,
            "build_id": build_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_dirty": bool(
                git_value(repository, "status", "--porcelain", "--untracked-files=all")
            ),
            "source_date_epoch": source_epoch,
            "source_revision": git_value(repository, "rev-parse", "HEAD"),
            "runtime_requirements_sha256": sha256(runtime_lock),
            "version": version,
            "wheel_filename": wheel.name,
            "wheel_sha256": wheel_digest,
        }
        (stage / "build-info.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        write_checksums(stage)

        destination = output_dir / build_id
        if destination.exists():
            existing = destination / "build-info.json"
            if existing.is_file():
                try:
                    existing_manifest = json.loads(existing.read_text(encoding="utf-8"))
                except ValueError:
                    existing_manifest = {}
                if existing_manifest.get("artifact_content_sha256") == artifact_digest:
                    print(f"Artifact already exists: {destination}")
                    archive = write_release_archive(destination, output_dir, version)
                    print(f"Built XC Buddy release archive: {archive}")
                    return
            raise RuntimeError(f"artifact destination already exists: {destination}")
        stage.replace(destination)
    archive = write_release_archive(destination, output_dir, version)
    print(f"Built XC Buddy artifact: {destination}")
    print(f"Built XC Buddy release archive: {archive}")


if __name__ == "__main__":
    main()
