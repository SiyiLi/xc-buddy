from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import logging
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

from . import config as config_file
from .bluetooth import BluetoothManager
from .build_identity import doctor_build_text, package_version, version_text
from .coordinator import Coordinator
from .input import InputInjector
from .services import latest_app_release, version_is_older
from .updater import install_app_release, standard_update_context


WEBSITE = "https://github.com/SiyiLi/xc-buddy/"
LOG = logging.getLogger(__name__)


class _EventRoot:
    """No-window event driver; XC Buddy's visible desktop UI is GTK."""

    @staticmethod
    def withdraw() -> None:
        return

    @staticmethod
    def update() -> None:
        return

    @staticmethod
    def destroy() -> None:
        return


def _create_event_root(tk_module):
    # Read screen DPI without retaining a hidden Tk window. All active desktop
    # windows and the StatusNotifier menu are GTK/GNOME Shell owned.
    scale_probe = tk_module.Tk()
    scale_probe.withdraw()
    try:
        _correct_gtk_scale(scale_probe)
    finally:
        scale_probe.destroy()
    return _EventRoot()


def _configure_gtk_backend() -> None:
    """Use positionable, server-decorated GTK windows inside GNOME Wayland."""
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ.setdefault("GDK_BACKEND", "x11")


def _desktop_integration_missing() -> list[str]:
    if os.environ.get("WAYLAND_DISPLAY"):
        missing = []
        if not (
            (shutil.which("wl-copy") and shutil.which("wl-paste"))
            or InputInjector.gtk_clipboard_available()
        ):
            missing.append("wl-clipboard or GTK clipboard access")
        if InputInjector._is_gnome_wayland():
            if not InputInjector.atspi_available():
                missing.append("AT-SPI keyboard synthesis")
            if not shutil.which("xmodmap"):
                missing.append("xmodmap")
        elif not (shutil.which("wtype") or shutil.which("ydotool")):
            missing.append("wtype or ydotool")
        return missing
    if os.environ.get("DISPLAY"):
        missing = []
        if not (shutil.which("xclip") or shutil.which("xsel")):
            missing.append("xclip or xsel")
        if not shutil.which("xdotool"):
            missing.append("xdotool")
        return missing
    return ["a graphical X11 or Wayland session"]


async def _start_core_before_desktop_ui(coordinator, ui) -> None:
    """Keep sender/receiver operation independent from panel integration."""
    await coordinator.start()
    try:
        await ui.start_tray()
    except Exception:
        LOG.exception("XC Buddy desktop controls could not be started")


def doctor() -> int:
    checks = {
        "Bluetooth service": shutil.which("bluetoothctl"),
    }
    if os.environ.get("WAYLAND_DISPLAY"):
        checks["Clipboard"] = (
            shutil.which("wl-copy") if shutil.which("wl-paste") else None
        )
        if not checks["Clipboard"] and InputInjector.gtk_clipboard_available():
            checks["Clipboard"] = "GTK"
        if InputInjector._is_gnome_wayland():
            checks["Input injection"] = (
                "AT-SPI"
                if InputInjector.atspi_available() and shutil.which("xmodmap")
                else None
            )
        else:
            checks["Input injection"] = shutil.which("wtype") or shutil.which("ydotool")
    else:
        checks["Clipboard"] = shutil.which("xclip") or shutil.which("xsel")
        checks["Input injection"] = shutil.which("xdotool")
    failed = False
    for label, value in checks.items():
        print(
            f"{'OK' if value else 'MISSING':7} {label}{f': {value}' if value else ''}"
        )
        failed |= not bool(value)
    missing = [
        name
        for name in ("bleak", "dbus_fast", "websockets", "tkinter")
        if importlib.util.find_spec(name) is None
    ]
    if not missing:
        print("OK      Python runtime dependencies")
    else:
        print(f"MISSING Python runtime dependencies: {', '.join(missing)}")
        failed = True
    print(f"Build:   {doctor_build_text()}")
    print(f"Config:  {config_file.config_path()}")
    return int(failed)


async def run() -> Path | None:
    _configure_gtk_backend()
    import tkinter as tk

    from .ui import DesktopUI

    root = _create_event_root(tk)
    loop = asyncio.get_running_loop()
    ui = DesktopUI(root, loop)
    configuration = config_file.load()
    first_launch = not config_file.config_path().exists()
    ui.configuration(configuration)
    coordinator = Coordinator(configuration, ui)
    stopping = asyncio.Event()
    restart_lock = asyncio.Lock()
    update_check_lock = asyncio.Lock()
    background_tasks: set[asyncio.Task] = set()
    restart_launcher: Path | None = None

    def spawn(coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    def save_candidate(
        candidate,
        failure_status: str = "Settings save failed",
        *,
        raise_error: bool = False,
    ) -> bool:
        nonlocal configuration
        try:
            config_file.save(candidate)
        except OSError:
            if raise_error:
                raise
            ui.status(failure_status)
            return False
        configuration = candidate
        ui.configuration(configuration)
        return True

    async def apply_candidate(candidate) -> None:
        async with restart_lock:
            await coordinator.update_config(candidate)

    def schedule_candidate(
        candidate,
        failure_status: str = "Settings save failed",
        *,
        raise_error: bool = False,
    ) -> None:
        if save_candidate(candidate, failure_status, raise_error=raise_error):
            spawn(apply_candidate(candidate))

    async def pair(persist: bool = True, activation_time: int = 0) -> bool:
        nonlocal configuration
        try:
            name = await ui.choose_device(
                BluetoothManager.discover,
                configuration.paired_device_ids,
                activation_time,
            )
            if name and (device_id := BluetoothManager.device_id(name)):
                candidate = copy.deepcopy(configuration)
                if device_id not in candidate.paired_device_ids:
                    candidate.paired_device_ids.append(device_id)
                if persist:
                    if coordinator.running:
                        if not save_candidate(candidate, "Pair save failed"):
                            return False
                        await apply_candidate(candidate)
                    elif not save_candidate(candidate, "Pair save failed"):
                        return False
                else:
                    configuration = candidate
                    ui.configuration(configuration)
                ui.status("Ready")
                return True
        except Exception as error:
            ui.error("", f"Bluetooth scan failed: {error}")
        return False

    def forget(device_id: str) -> None:
        candidate = copy.deepcopy(configuration)
        candidate.paired_device_ids = [
            value for value in candidate.paired_device_ids if value != device_id
        ]
        candidate.device_theme_colors.pop(device_id, None)
        candidate.device_overlay_positions.pop(device_id, None)
        candidate.device_outputs.pop(device_id, None)
        schedule_candidate(candidate, "Forget device failed")

    def set_theme(device_id: str, value: str) -> None:
        candidate = copy.deepcopy(configuration)
        if value == "white":
            candidate.device_theme_colors.pop(device_id, None)
        else:
            candidate.device_theme_colors[device_id] = value
        if save_candidate(candidate, "Theme save failed"):
            coordinator.update_device_theme_colors(candidate.device_theme_colors)

    def set_position(device_id: str, value: str) -> None:
        candidate = copy.deepcopy(configuration)
        if value == "center":
            candidate.device_overlay_positions.pop(device_id, None)
        else:
            candidate.device_overlay_positions[device_id] = value
        save_candidate(candidate, "Position save failed")

    def set_translation(device_id: str, mode: str, language: str) -> None:
        candidate = copy.deepcopy(configuration)
        profile = candidate.profile_for(device_id)
        profile.transform = mode
        if language:
            profile.translation_target = language
        if (
            profile.transform == candidate.output.transform
            and profile.translation_target == candidate.output.translation_target
        ):
            candidate.device_outputs.pop(device_id, None)
        else:
            candidate.device_outputs[device_id] = profile
        schedule_candidate(candidate, "Output save failed")

    def set_relay(mode: str) -> None:
        candidate = copy.deepcopy(configuration)
        candidate.relay_mode = mode
        schedule_candidate(candidate, "Relay mode save failed")

    def set_output(target: str) -> None:
        candidate = copy.deepcopy(configuration)
        candidate.output.target = target
        schedule_candidate(candidate, "Output save failed")

    def set_interaction(mode: str) -> None:
        candidate = copy.deepcopy(configuration)
        candidate.interaction_mode = mode
        schedule_candidate(candidate, "Input save failed")

    def set_auto_enter(enabled: bool) -> None:
        candidate = copy.deepcopy(configuration)
        candidate.auto_enter = enabled
        schedule_candidate(candidate, "Input save failed")

    def open_config_folder() -> None:
        directory = config_file.config_path().parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(
                ["xdg-open", str(directory)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            ui.status(f"Could not open config folder: {error}")

    ui.on_pair = lambda activation_time=0: spawn(pair(activation_time=activation_time))

    def open_settings(activation_time: int = 0) -> None:
        logging.getLogger(__name__).debug("Settings action reached main event loop")
        candidate = copy.deepcopy(config_file.load())
        ui.edit_settings(
            candidate,
            lambda: schedule_candidate(candidate, raise_error=True),
            activation_time,
        )

    ui.on_settings = open_settings
    ui.on_restore = coordinator.restore_last
    ui.on_update = lambda device_id: spawn(coordinator.update_firmware(device_id))
    ui.on_cancel_update = coordinator.cancel_firmware_update
    ui.on_forget = forget
    ui.on_set_theme = set_theme
    ui.on_set_position = set_position
    ui.on_set_translation = set_translation
    ui.on_set_relay = set_relay
    ui.on_set_output = set_output
    ui.on_set_interaction = set_interaction
    ui.on_set_auto_enter = set_auto_enter
    ui.on_website = lambda: webbrowser.open(WEBSITE)

    update_context = standard_update_context()
    if update_context is not None:

        async def check_for_app_updates(activation_time: int = 0) -> None:
            nonlocal restart_launcher
            if update_check_lock.locked():
                return
            async with update_check_lock:
                try:
                    current_version = package_version()
                    release = await asyncio.to_thread(
                        latest_app_release, current_version
                    )
                    if not version_is_older(current_version, release.version):
                        ui.app_update_current(current_version, activation_time)
                        return
                    accepted = await ui.confirm_app_update(
                        current_version, release.version, activation_time
                    )
                    if not accepted:
                        return
                    ui.app_update_started(activation_time)
                    restart_launcher = await asyncio.to_thread(
                        install_app_release, release, update_context
                    )
                    ui.app_update_restarting()
                    stopping.set()
                except Exception as error:
                    LOG.exception("XC Buddy app update failed")
                    ui.app_update_failed(str(error), activation_time)

        ui.on_check_updates = lambda activation_time=0: spawn(
            check_for_app_updates(activation_time)
        )

    ui.on_open_config = open_config_folder
    ui.on_quit = stopping.set

    if first_launch:
        completed = await ui.onboard(
            configuration,
            BluetoothManager.discover,
            BluetoothManager.device_id,
            _desktop_integration_missing,
            lambda: config_file.save(configuration),
        )
        if not completed:
            ui.shutdown()
            root.destroy()
            return None
        await coordinator.update_config(configuration)

    await _start_core_before_desktop_ui(coordinator, ui)
    try:
        while not stopping.is_set():
            root.update()
            ui.pump()
            await asyncio.sleep(0.02)
    finally:
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        await coordinator.stop()
        ui.shutdown()
        root.destroy()
    return restart_launcher


def _correct_gtk_scale(root) -> None:
    """Undo GNOME 2x scaling when X11 reports a standard-DPI physical screen."""
    if "GDK_SCALE" in os.environ or "GDK_DPI_SCALE" in os.environ:
        return
    try:
        physical_dpi = root.winfo_screenwidth() * 25.4 / root.winfo_screenmmwidth()
        resources = subprocess.run(
            ["xrdb", "-query"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        configured_dpi = next(
            float(line.split(":", 1)[1].strip())
            for line in resources.splitlines()
            if line.lower().startswith("xft.dpi:")
        )
    except (OSError, StopIteration, ValueError, ZeroDivisionError):
        return
    if physical_dpi < 144 and configured_dpi >= 168:
        ui_scale = max(1.0, min(1.25, physical_dpi / 96))
        readable_dpi = physical_dpi * 1.1
        os.environ["GDK_SCALE"] = "1"
        os.environ["GDK_DPI_SCALE"] = f"{readable_dpi / configured_dpi:.3f}"
        os.environ["XC_BUDDY_UI_SCALE"] = f"{ui_scale:.3f}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="XC Buddy Linux desktop companion")
    parser.add_argument(
        "--doctor", action="store_true", help="check desktop integration dependencies"
    )
    parser.add_argument("--version", action="version", version=version_text())
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.doctor:
        raise SystemExit(doctor())
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.verbose:
        for logger_name in ("bleak", "dbus_fast", "websockets"):
            logging.getLogger(logger_name).setLevel(logging.INFO)
    try:
        restart_launcher = asyncio.run(run())
    except KeyboardInterrupt:
        return
    if restart_launcher is not None:
        os.execv(str(restart_launcher), [str(restart_launcher)])


if __name__ == "__main__":
    main()
