from __future__ import annotations

import asyncio
import logging
import math
import re
import tkinter as tk
from functools import partial
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Awaitable, Callable, cast

from .bluetooth import ConnectedXCDevice
from .gtk_layout import present_from_tray
from .status import tray_presentation

logger = logging.getLogger(__name__)


COLORS = {
    "white": "#f4f4f4",
    "pink": "#ff8ac5",
    "green": "#80df9a",
    "yellow": "#ffe27a",
    "blue": "#79b8ff",
    "purple": "#c39bff",
}

OVERLAY_BACKGROUNDS = {
    "white": "#ffffff",
    "pink": "#ffd6e6",
    "green": "#d6f2d6",
    "yellow": "#fff0b8",
    "blue": "#d1e8ff",
    "purple": "#e6d6ff",
}

THEME_NAMES = {
    "white": "White",
    "pink": "Pink",
    "green": "Green",
    "yellow": "Yellow",
    "blue": "Blue",
    "purple": "Purple",
}

POSITION_NAMES = {
    "center": "Center",
    "top_left": "Top Left",
    "top_right": "Top Right",
    "bottom_left": "Bottom Left",
    "bottom_right": "Bottom Right",
}

TRANSLATION_TARGETS = (
    ("en", "English"),
    ("zh-Hans", "Chinese (Simplified)"),
    ("zh-Hant", "Chinese (Traditional)"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("ru", "Russian"),
    ("fr", "French"),
    ("de", "German"),
    ("es", "Spanish"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("nl", "Dutch"),
    ("sv", "Swedish"),
    ("pl", "Polish"),
    ("tr", "Turkish"),
    ("ar", "Arabic"),
    ("hi", "Hindi"),
    ("id", "Indonesian"),
    ("vi", "Vietnamese"),
    ("th", "Thai"),
)


def _minute_text(seconds: int) -> str:
    if seconds % 60 == 0:
        return str(seconds // 60)
    return f"{seconds / 60:.2f}".rstrip("0").rstrip(".")


def _choose_directory(variable: tk.StringVar) -> None:
    selected = filedialog.askdirectory(initialdir=variable.get())
    if selected:
        variable.set(selected)


class DesktopUI:
    def __init__(self, root: tk.Tk, loop: asyncio.AbstractEventLoop) -> None:
        self.root, self.loop = root, loop
        root.withdraw()
        self.overlay_windows: dict[str, tk.Toplevel] = {}
        self.overlay_afters: dict[str, str] = {}
        self.subtitle_window: tk.Toplevel | None = None
        self.subtitle_frame: tk.Frame | None = None
        self.subtitle_lanes: dict[str, tuple[tk.Label, str | None]] = {}
        self._status = "Starting"
        self._codex = "Starting"
        self._relay = "Disabled"
        self._config: Any = None
        self._connections: dict[str, ConnectedXCDevice] = {}
        self._paired_ids: set[str] = set()
        self._device_info: dict[str, dict] = {}
        self._latest_firmware: str | None = None
        self._firmware_checking = False
        self._updating_device: str | None = None
        self._firmware_confirmed = 0
        self._has_recoverable_input = False
        self._theme_colors: dict[str, str] = {}
        self._overlay_positions: dict[str, str] = {}
        self.on_pair: Callable[..., object] = lambda: None
        self.on_settings: Callable[..., object] = lambda: None
        self.on_restore: Callable[..., object] = lambda: None
        self.on_update: Callable[..., object] = lambda _device_id: None
        self.on_cancel_update: Callable[..., object] = lambda: None
        self.on_forget: Callable[..., object] = lambda _device_id: None
        self.on_set_theme: Callable[..., object] = lambda _device_id, _value: None
        self.on_set_position: Callable[..., object] = lambda _device_id, _value: None
        self.on_set_translation: Callable[..., object] = (
            lambda _device_id, _mode, _language: None
        )
        self.on_set_relay: Callable[..., object] = lambda _mode: None
        self.on_set_output: Callable[..., object] = lambda _target: None
        self.on_set_interaction: Callable[..., object] = lambda _mode: None
        self.on_set_auto_enter: Callable[..., object] = lambda _enabled: None
        self.on_website: Callable[..., object] = lambda: None
        self.on_check_updates: Callable[..., object] | None = None
        self.on_open_config: Callable[..., object] = lambda: None
        self.on_quit: Callable[..., object] = lambda: None
        self.tray: Any = None
        self._menu_api: Any = None
        self._gtk_windows: list[Any] = []
        self._runtime_presenter: Any = None
        self._firmware_window: Any = None
        self._app_update_window: Any = None
        self._settings_window: Any = None
        self._pairing_window: Any = None
        self._control_window: Any = None
        self._device_windows: dict[str, Any] = {}
        self._pending_window_activation_time = 0

    async def start_tray(self) -> None:
        from . import native_indicator

        self._menu_api = native_indicator
        try:
            tray = native_indicator.NativeIndicator(
                "xc-buddy", "XC Buddy", self._menu(native_indicator)
            )
            tray.on_connection_changed = self._tray_connection_changed
            registered = await tray.start()
            self.tray = tray
            self._refresh()
            if registered:
                return
            logger.warning(
                "StatusNotifier tray unavailable: %s", tray.registration_error
            )
            self._show_control_window()
        except Exception as error:
            logger.warning("StatusNotifier tray unavailable: %s", error)
            self._menu_api = None
            self.tray = None
            self._show_control_window()

    def _menu(self, menu_api=None):
        if menu_api is None:
            from . import native_indicator

            menu_api = native_indicator

        device_items = [
            menu_api.MenuItem(
                self._device_name(device_id),
                lambda indicator, _item, device_id=device_id: self.call(
                    self._show_device_window,
                    device_id,
                    getattr(indicator, "activation_time", 0),
                ),
            )
            for device_id in self._device_ids()
        ]
        config = self._config
        return menu_api.Menu(
            menu_api.MenuItem(lambda _i: self._device_summary(), None, enabled=False),
            menu_api.MenuItem(
                lambda _i: f"Target: {self._output_name()}", None, enabled=False
            ),
            menu_api.MenuItem(lambda _i: f"Codex: {self._codex}", None, enabled=False),
            menu_api.MenuItem(lambda _i: self._relay_summary(), None, enabled=False),
            menu_api.Menu.SEPARATOR,
            menu_api.MenuItem(
                "Relay Mode",
                menu_api.Menu(
                    *[
                        menu_api.MenuItem(
                            label,
                            self._menu_action(self.on_set_relay, value),
                            checked=lambda _item, value=value: bool(config)
                            and config.relay_mode == value,
                            radio=True,
                        )
                        for value, label in (
                            ("disabled", "Disabled"),
                            ("sender", "Sender"),
                            ("receiver", "Receiver"),
                        )
                    ]
                ),
            ),
            menu_api.Menu.SEPARATOR,
            *(
                [
                    menu_api.MenuItem(
                        "Restore Last Input",
                        lambda _i, _item: self.call(self.on_restore),
                    ),
                    menu_api.Menu.SEPARATOR,
                ]
                if self._has_recoverable_input
                else []
            ),
            *device_items,
            *([menu_api.Menu.SEPARATOR] if device_items else []),
            menu_api.MenuItem(
                "Output",
                menu_api.Menu(
                    *[
                        menu_api.MenuItem(
                            label,
                            self._menu_action(self.on_set_output, value),
                            checked=lambda _item, value=value: bool(config)
                            and config.output.target == value,
                            radio=True,
                        )
                        for value, label in (
                            ("focused_app", "Focused App"),
                            ("subtitle", "Subtitle"),
                        )
                    ]
                ),
            ),
            menu_api.MenuItem(
                "Press Return After Paste",
                lambda _icon, _item: self.call(
                    self.on_set_auto_enter, not bool(config and config.auto_enter)
                ),
                checked=lambda _item: bool(config and config.auto_enter),
            ),
            menu_api.MenuItem(
                "Interaction",
                menu_api.Menu(
                    *[
                        menu_api.MenuItem(
                            label,
                            self._menu_action(self.on_set_interaction, value),
                            checked=lambda _item, value=value: bool(config)
                            and config.interaction_mode == value,
                            radio=True,
                        )
                        for value, label in (
                            ("hold_to_talk", "Hold to Talk"),
                            ("click_to_talk", "Click to Talk"),
                        )
                    ]
                ),
            ),
            menu_api.Menu.SEPARATOR,
            menu_api.MenuItem(
                "Pair Device...",
                lambda indicator, _item: self.call(
                    self.on_pair, getattr(indicator, "activation_time", 0)
                ),
            ),
            menu_api.MenuItem(
                "Settings...",
                lambda indicator, _item: self.call(
                    self.on_settings, getattr(indicator, "activation_time", 0)
                ),
            ),
            menu_api.Menu.SEPARATOR,
            menu_api.MenuItem("Website", lambda _i, _item: self.call(self.on_website)),
            *(
                [
                    menu_api.MenuItem(
                        "Check for App Updates...",
                        lambda indicator, _item: self.call(
                            self.on_check_updates,
                            getattr(indicator, "activation_time", 0),
                        ),
                    )
                ]
                if self.on_check_updates is not None
                else []
            ),
            menu_api.MenuItem("Quit", lambda _i, _item: self.call(self.on_quit)),
        )

    def _menu_action(self, callback: Callable[..., object], *arguments: object):
        def action(_indicator, _item) -> None:
            self.call(callback, *arguments)

        return action

    def _device_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                self._paired_ids
                | {device.device_id for device in self._connections.values()}
                | set(self._device_info)
            )
        )

    def _device_name(self, device_id: str) -> str:
        connected = next(
            (
                device
                for device in self._connections.values()
                if device.device_id == device_id
            ),
            None,
        )
        return connected.name if connected is not None else f"XC-{device_id}"

    def _connected_device_ids(self) -> set[str]:
        return {device.device_id for device in self._connections.values()}

    def _device_summary(self) -> str:
        if self._config and self._config.relay_mode == "sender":
            return "Device: Bluetooth paused"
        if self._connections:
            connected = min(
                self._connections.values(), key=lambda device: device.device_id
            )
            return f"Device: {connected.name} connected"
        if self._paired_ids:
            return f"Device: XC-{sorted(self._paired_ids)[0]} scanning"
        return "Device: Not paired"

    def _device_state(self, device_id: str) -> str:
        if self._config and self._config.relay_mode == "sender":
            return "Bluetooth paused"
        return "Connected" if device_id in self._connected_device_ids() else "Scanning"

    def _relay_summary(self) -> str:
        if (
            self._config
            and self._config.relay_mode in ("sender", "receiver")
            and self._relay in ("Connecting", "Connected", "Reconnecting")
        ):
            return f"Relay: {self._relay} as a {self._config.relay_mode}"
        return f"Relay: {self._relay}"

    def _output_name(self) -> str:
        if self._config and self._config.output.target == "subtitle":
            return "Subtitle"
        return "Focused App"

    def _firmware_label(self, device_id: str) -> str:
        version = self._device_info.get(device_id, {}).get("firmware_version")
        if not version:
            return (
                "Reading Firmware..."
                if device_id in self._connected_device_ids()
                else "Connect Device to Read Firmware"
            )
        return f"Firmware {version}"

    def _request_firmware_update(
        self, device_id: str, activation_time: int = 0
    ) -> None:
        self._pending_window_activation_time = activation_time
        self.on_update(device_id)

    def call(self, callback: Callable, *args: object) -> None:
        def invoke() -> None:
            try:
                callback(*args)
            except Exception:
                logger.exception(
                    "UI callback failed: %s", getattr(callback, "__name__", callback)
                )
            else:
                logger.debug(
                    "UI callback completed: %s",
                    getattr(callback, "__name__", callback),
                )

        self.loop.call_soon_threadsafe(invoke)

    def _refresh(self) -> None:
        if self.tray:
            presentation = tray_presentation(self._status, bool(self._connections))
            self.tray.set_state(
                presentation.icon_name,
                presentation.accessibility_description,
                presentation.visible_title,
            )
            self.tray.menu = self._menu(self._menu_api)
            self.tray.update_menu()
        device_ids = set(self._device_ids())
        for device_id, window in tuple(self._device_windows.items()):
            if device_id in device_ids:
                window.refresh()
            else:
                window.window.destroy()

    def pump(self) -> None:
        if self.tray and hasattr(self.tray, "pump"):
            self.tray.pump()
        if self._runtime_presenter is not None or self._gtk_windows:
            from .gtk_onboarding import _gtk_module

            Gtk = _gtk_module()
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)

    def _runtime(self):
        if self._runtime_presenter is None:
            from .gtk_runtime import RuntimePresenter

            self._runtime_presenter = RuntimePresenter(self.loop)
            self._runtime_presenter.preferences(
                self._theme_colors, self._overlay_positions
            )
        return self._runtime_presenter

    def status(self, value: str) -> None:
        self._status = value
        self._refresh()

    def relay_status(self, value: str) -> None:
        self._relay = value
        self._refresh()

    def codex_status(self, value: str) -> None:
        self._codex = "Idle" if value.startswith("Listening") else value
        self._refresh()

    def configuration(self, config: Any) -> None:
        self._config = config
        paired = set(config.paired_device_ids)
        self._device_info = {
            device_id: info
            for device_id, info in self._device_info.items()
            if device_id in paired
        }
        self.preferences(
            config.device_theme_colors,
            config.device_overlay_positions,
            config.paired_device_ids,
        )

    def connections(self, value: dict[str, ConnectedXCDevice]) -> None:
        self._connections = value
        self._refresh()

    def recoverable_input(self, available: bool) -> None:
        self._has_recoverable_input = available
        self._refresh()

    def device_info(self, device_id: str, info: dict) -> None:
        self._device_info[device_id] = info
        self._refresh()

    def firmware_release(self, version: str, availability: dict[str, bool]) -> None:
        self._latest_firmware = version
        for device_id, available in availability.items():
            self._device_info.setdefault(device_id, {})["update_available"] = available
        self._refresh()

    def firmware_checking(self, checking: bool) -> None:
        self._firmware_checking = checking
        self._refresh()

    def preferences(
        self, colors: dict[str, str], positions: dict[str, str], paired_ids=()
    ) -> None:
        self._theme_colors = dict(colors)
        self._overlay_positions = dict(positions)
        self._paired_ids = set(paired_ids)
        if self._runtime_presenter is not None:
            self._runtime_presenter.preferences(colors, positions)
        for device_id, (label, _timer) in self.subtitle_lanes.items():
            label.configure(
                fg=COLORS.get(self._theme_colors.get(device_id, "white"), "#f4f4f4")
            )
        for key, window in self.overlay_windows.items():
            device_id = "" if key == "__default__" else key
            background = OVERLAY_BACKGROUNDS.get(
                self._theme_colors.get(device_id, "white"), "#ffffff"
            )
            window.configure(bg=background)
            for child in window.winfo_children():
                if isinstance(child, tk.Label):
                    child.configure(bg=background, fg="#171717")
        self._reposition_overlays()
        self._refresh()

    def listening(self, device_id: str) -> None:
        self.status("Listening")
        self._runtime().show_overlay(device_id, "listening", "Listening...")

    def processing(self, device_id: str, text: str) -> None:
        self.status(text)
        self._runtime().show_overlay(device_id, "listening", text)

    def final(self, device_id: str, text: str, hidden: Callable) -> None:
        self.status("Ready" if text else "No speech")
        self._runtime().show_overlay(
            device_id, "countdown", text or "No speech", seconds=1.2, hidden=hidden
        )

    def paused(self, device_id: str, text: str) -> None:
        self._runtime().show_overlay(
            device_id,
            "paused",
            text or "No speech",
            hint="Front: Send    Side: Cancel",
        )

    def error(self, device_id: str, text: str, hidden: Callable | None = None) -> None:
        self.status(f"ASR error: {text}")
        self._runtime().show_overlay(
            device_id,
            "error",
            text or "Unknown ASR error",
            hint="ASR Error",
            seconds=2,
            hidden=hidden or (lambda: self.status("Ready")),
        )

    def subtitle(self, device_id: str, text: str, color: str) -> None:
        self._runtime().subtitles.show(device_id, text, color)
        return
        if self.subtitle_window is None:
            window = self.subtitle_window = tk.Toplevel(self.root)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.configure(bg="#151515")
            self.subtitle_frame = tk.Frame(window, bg="#151515", padx=18, pady=12)
            self.subtitle_frame.pack()
        existing = self.subtitle_lanes.pop(device_id, None)
        if existing:
            existing[0].destroy()
            if existing[1]:
                self.root.after_cancel(existing[1])
        label = tk.Label(
            self.subtitle_frame,
            text=f"XC-{device_id}  {text}",
            bg="#151515",
            fg=COLORS.get(color, "white"),
            font=("Sans", 28, "bold"),
            padx=18,
            pady=10,
            wraplength=1200,
            justify="left",
        )
        label.pack(anchor="w")
        timer = self.root.after(7000, lambda: self._hide_subtitle(device_id))
        self.subtitle_lanes[device_id] = (label, timer)
        self._position_subtitles()

    def _hide_subtitle(self, device_id: str) -> None:
        lane = self.subtitle_lanes.pop(device_id, None)
        if lane:
            if lane[1]:
                try:
                    self.root.after_cancel(lane[1])
                except tk.TclError:
                    pass
            lane[0].destroy()
        if not self.subtitle_lanes and self.subtitle_window:
            self.subtitle_window.destroy()
            self.subtitle_window = None
            self.subtitle_frame = None
        else:
            self._position_subtitles()

    def hide_subtitles(self) -> None:
        if self._runtime_presenter is not None:
            self._runtime_presenter.subtitles.hide_all()
        return
        for device_id in list(self.subtitle_lanes):
            self._hide_subtitle(device_id)

    def _position_subtitles(self) -> None:
        if not self.subtitle_window:
            return
        self.subtitle_window.update_idletasks()
        width, height = (
            self.subtitle_window.winfo_width(),
            self.subtitle_window.winfo_height(),
        )
        x = (self.subtitle_window.winfo_screenwidth() - width) // 2
        y = self.subtitle_window.winfo_screenheight() - height - 90
        self.subtitle_window.geometry(f"+{x}+{y}")

    def firmware_progress(
        self, device_id: str, written: int, total: int, confirmed: bool
    ) -> None:
        if self._updating_device != device_id:
            self._firmware_confirmed = 0
        self._updating_device = device_id
        if confirmed:
            self._firmware_confirmed = max(self._firmware_confirmed, written)
        displayed = (
            self._firmware_confirmed
            if confirmed
            else max(
                self._firmware_confirmed,
                min(written, self._firmware_confirmed + 64 * 1024),
            )
        )
        percent = int(displayed * 100 / max(total, 1))
        self.status(f"Updating XC-{device_id}: {percent}%")
        if self._firmware_window is not None:
            self._firmware_window.update(written, total, confirmed)

    def firmware_started(self, file_name: str) -> None:
        from .gtk_firmware import FirmwareWindow

        if self._firmware_window is not None:
            self._firmware_window.window.destroy()
        firmware_window = FirmwareWindow(
            file_name,
            lambda: self.call(self.on_cancel_update),
            self._pending_window_activation_time,
        )
        self._pending_window_activation_time = 0
        self._firmware_window = firmware_window
        self._gtk_windows.append(firmware_window.window)

        def closed(window) -> None:
            if window in self._gtk_windows:
                self._gtk_windows.remove(window)
            if self._firmware_window is firmware_window:
                self._firmware_window = None

        firmware_window.window.connect("destroy", closed)

    def firmware_succeeded(self) -> None:
        if self._firmware_window is not None:
            self._firmware_window.finish()

    def firmware_failed(self, error: str) -> None:
        if self._firmware_window is not None:
            self._firmware_window.finish(error)

    def firmware_finished(self) -> None:
        self._updating_device = None
        self._firmware_confirmed = 0
        self._refresh()

    async def confirm_app_update(
        self, current_version: str, latest_version: str, activation_time: int = 0
    ) -> bool:
        from .gtk_app_update import confirm_app_update

        return await confirm_app_update(
            current_version,
            latest_version,
            activation_time,
            self._track_gtk_window,
        )

    def app_update_current(self, version: str, activation_time: int = 0) -> None:
        from .gtk_app_update import show_update_message

        self._track_gtk_window(
            show_update_message(
                "You're up to date!",
                f"XC Buddy {version} is currently the newest version available.",
                activation_time,
            )
        )

    def app_update_started(self, activation_time: int = 0) -> None:
        from .gtk_app_update import AppUpdateWindow

        if self._app_update_window is not None:
            self._app_update_window.window.destroy()
        self._app_update_window = AppUpdateWindow(activation_time)
        self._track_gtk_window(self._app_update_window.window)

    def app_update_failed(self, error: str, activation_time: int = 0) -> None:
        from .gtk_app_update import show_update_message

        if self._app_update_window is not None:
            self._app_update_window.fail(error)
            return
        self._track_gtk_window(
            show_update_message("Update Failed", error, activation_time)
        )

    def app_update_restarting(self) -> None:
        if self._app_update_window is not None:
            self._app_update_window.restarting()

    def _track_gtk_window(self, window: Any) -> None:
        if window not in self._gtk_windows:
            self._gtk_windows.append(window)

        def closed(closed_window) -> None:
            if closed_window in self._gtk_windows:
                self._gtk_windows.remove(closed_window)

        window.connect("destroy", closed)

    def _overlay(
        self,
        device_id: str,
        text: str,
        hidden: Callable | None,
        seconds: float,
        color: str = "white",
    ) -> None:
        key = device_id or "__default__"
        self.hide_overlay(device_id)
        theme = self._theme_colors.get(device_id, "white")
        background = OVERLAY_BACKGROUNDS.get(theme, "#ffffff")
        foreground = "#171717" if color == "white" else color
        window = tk.Toplevel(self.root)
        self.overlay_windows[key] = window
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg=background)
        label = tk.Label(
            window,
            text=(f"XC-{device_id}  " if device_id else "") + text,
            bg=background,
            fg=foreground,
            font=("Sans", 18, "bold"),
            padx=28,
            pady=18,
            wraplength=1200,
            justify="left",
        )
        label.pack()
        self._reposition_overlays()
        if seconds:

            def close() -> None:
                self.hide_overlay(device_id)
                if hidden:
                    hidden()

            self.overlay_afters[key] = self.root.after(int(seconds * 1000), close)

    def hide_overlay(self, device_id: str | None = None) -> None:
        if self._runtime_presenter is not None:
            self._runtime_presenter.hide_overlay(device_id)
        return
        keys = (
            list(self.overlay_windows)
            if device_id is None
            else [device_id or "__default__"]
        )
        for key in keys:
            timer = self.overlay_afters.pop(key, None)
            if timer:
                try:
                    self.root.after_cancel(timer)
                except tk.TclError:
                    pass
            window = self.overlay_windows.pop(key, None)
            if window:
                window.destroy()
        self._reposition_overlays()

    def _reposition_overlays(self) -> None:
        grouped: dict[str, list[tuple[str, tk.Toplevel]]] = {}
        for key, window in self.overlay_windows.items():
            device_id = "" if key == "__default__" else key
            position = self._overlay_positions.get(device_id, "center")
            grouped.setdefault(position, []).append((key, window))
        margin, gap = 36, 12
        for position, members in grouped.items():
            members.sort(key=lambda pair: pair[0])
            sizes = []
            for _key, window in members:
                window.update_idletasks()
                sizes.append((window.winfo_width(), window.winfo_height()))
            screen_width = members[0][1].winfo_screenwidth()
            screen_height = members[0][1].winfo_screenheight()
            total_height = sum(height for _width, height in sizes) + gap * (
                len(sizes) - 1
            )
            if position.startswith("bottom"):
                y = screen_height - margin - total_height
            elif position.startswith("top"):
                y = margin
            else:
                y = (screen_height - total_height) // 2
            for (_key, window), (width, height) in zip(members, sizes):
                if position.endswith("left"):
                    x = margin
                elif position.endswith("right"):
                    x = screen_width - width - margin
                else:
                    x = (screen_width - width) // 2
                window.geometry(f"+{max(0, x)}+{max(0, y)}")
                y += height + gap

    def _show_device_window(self, device_id: str, activation_time: int = 0) -> None:
        if device_id not in self._device_ids():
            return
        existing = self._device_windows.get(device_id)
        if existing is not None:
            existing.present(activation_time)
            return

        from .gtk_device import DeviceWindow

        device_window = DeviceWindow(
            self,
            device_id,
            THEME_NAMES,
            POSITION_NAMES,
            TRANSLATION_TARGETS,
        )
        self._device_windows[device_id] = device_window
        self._gtk_windows.append(device_window.window)

        def closed(window) -> None:
            if window in self._gtk_windows:
                self._gtk_windows.remove(window)
            if self._device_windows.get(device_id) is device_window:
                self._device_windows.pop(device_id)

        device_window.window.connect("destroy", closed)
        device_window.window.show_all()
        device_window.present(activation_time)

    def _show_control_window(self, activation_time: int = 0) -> None:
        """Keep basic controls available on desktops without a tray host."""
        if self._control_window is not None:
            present_from_tray(self._control_window, activation_time)
            return

        from .gtk_layout import apply_typography, scale_window
        from .gtk_onboarding import _gtk_module, _label

        Gtk = _gtk_module()
        window = Gtk.Window(title="XC Buddy")
        window.set_default_size(420, 230)
        window.set_size_request(420, 230)
        scale_window(window, 420, 230)
        apply_typography(Gtk, window)
        window.set_resizable(False)
        window.set_position(Gtk.WindowPosition.CENTER)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(20)
        page.set_margin_bottom(20)
        page.set_margin_start(22)
        page.set_margin_end(22)
        window.add(page)
        title = _label(Gtk, "XC Buddy")
        title.get_style_context().add_class("heading")
        page.pack_start(title, False, False, 0)
        page.pack_start(
            _label(Gtk, "System tray unavailable", secondary=True),
            False,
            False,
            0,
        )
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for label, callback in (
            ("Pair Device…", self.on_pair),
            ("Settings…", self.on_settings),
            ("Restore Last Input", self.on_restore),
        ):
            button = Gtk.Button.new_with_label(label)
            button.connect(
                "clicked", lambda _button, callback=callback: self.call(callback)
            )
            actions.pack_start(button, False, False, 0)
        page.pack_start(actions, False, False, 0)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        website = Gtk.Button.new_with_label("Website")
        website.connect("clicked", lambda _button: self.call(self.on_website))
        quit_button = Gtk.Button.new_with_label("Quit")
        quit_button.connect("clicked", lambda _button: self.call(self.on_quit))
        footer.pack_start(website, False, False, 0)
        footer.pack_end(quit_button, False, False, 0)
        page.pack_end(footer, False, False, 0)

        self._control_window = window
        self._gtk_windows.append(window)

        def closed(window) -> None:
            if window in self._gtk_windows:
                self._gtk_windows.remove(window)
            if self._control_window is window:
                self._control_window = None

        window.connect("destroy", closed)
        window.show_all()
        present_from_tray(window, activation_time)

    def _hide_control_window(self) -> None:
        window = self._control_window
        if window is None:
            return
        self._control_window = None
        window.destroy()

    def _tray_connection_changed(self, connected: bool) -> None:
        if connected:
            self._hide_control_window()
        else:
            self._show_control_window()

    async def choose_device(
        self,
        discover: Callable[[], Awaitable[list[tuple[str, str, int]]]],
        existing_device_ids: list[str],
        activation_time: int = 0,
    ) -> str | None:
        from .bluetooth import BluetoothManager
        from .gtk_pairing import choose_device

        if self._pairing_window is not None:
            present_from_tray(self._pairing_window, activation_time)
            return None

        def window_created(window) -> None:
            self._pairing_window = window

        try:
            return await choose_device(
                discover,
                BluetoothManager.device_id,
                existing_device_ids,
                activation_time,
                window_created,
            )
        finally:
            self._pairing_window = None

    async def _choose_device_tk(
        self, devices: list[tuple[str, str, int]]
    ) -> str | None:
        if not devices:
            messagebox.showinfo(
                "Pair XC",
                "No XC device was found. Ensure Bluetooth is on and the Stick is awake.",
            )
            return None
        choices = "\n".join(
            f"{index + 1}. {name} ({rssi} dBm)"
            for index, (_address, name, rssi) in enumerate(devices)
        )
        selection = simpledialog.askinteger(
            "Pair XC",
            f"Select a device:\n\n{choices}",
            minvalue=1,
            maxvalue=len(devices),
        )
        return devices[selection - 1][1] if selection else None

    async def onboard(
        self,
        config: Any,
        discover: Callable[[], Awaitable[list[tuple[str, str, int]]]],
        device_id_for_name: Callable[[str], str],
        integration_missing: Callable[[], list[str]],
        save_config: Callable[[], None],
    ) -> bool:
        from .gtk_onboarding import onboard

        return await onboard(
            config,
            discover,
            device_id_for_name,
            integration_missing,
            save_config,
        )

    async def _onboard_tk(
        self,
        config: Any,
        discover: Callable[[], Awaitable[list[tuple[str, str, int]]]],
        device_id_for_name: Callable[[str], str],
        integration_missing: Callable[[], list[str]],
        save_config: Callable[[], None],
    ) -> bool:
        """Run first launch in one four-step window, matching the macOS flow."""
        window = tk.Toplevel(self.root)
        window.title("Set Up XC Buddy")
        window.geometry("680x470")
        window.minsize(680, 470)
        window.configure(bg="#f5f5f7")

        result: asyncio.Future[bool] = self.loop.create_future()
        scan_task: asyncio.Task | None = None
        current_step = 0
        devices: list[tuple[str, str, int]] = []
        selected_name = ""
        step_names = ("Pair Device", "Transcription", "Desktop Access", "Ready")
        status = tk.StringVar()
        api_key = tk.StringVar(value=config.openai_api_key)

        outer = tk.Frame(window, bg="#f5f5f7")
        outer.pack(fill="both", expand=True)
        sidebar = tk.Frame(outer, bg="#e9e9ec", width=170, padx=20, pady=25)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        main = tk.Frame(outer, bg="#f5f5f7", padx=28, pady=25)
        main.pack(side="left", fill="both", expand=True)

        step_labels = []
        for index, name in enumerate(step_names):
            label = tk.Label(
                sidebar,
                text=f"{index + 1}.  {name}",
                bg="#e9e9ec",
                fg="#77777d",
                anchor="w",
                font=("Sans", 11),
                pady=5,
            )
            label.pack(fill="x")
            step_labels.append(label)

        title = tk.Label(
            main,
            bg="#f5f5f7",
            fg="#171719",
            anchor="w",
            font=("Sans", 20, "bold"),
        )
        title.pack(fill="x")
        detail = tk.Label(
            main,
            bg="#f5f5f7",
            fg="#68686e",
            anchor="w",
            justify="left",
            wraplength=440,
            font=("Sans", 10),
        )
        detail.pack(fill="x", pady=(6, 18))
        content = tk.Frame(main, bg="#f5f5f7")
        content.pack(fill="both", expand=True)

        footer = tk.Frame(main, bg="#f5f5f7")
        footer.pack(fill="x", side="bottom")
        status_label = tk.Label(
            footer,
            textvariable=status,
            bg="#f5f5f7",
            fg="#68686e",
            anchor="w",
            justify="left",
            wraplength=260,
        )
        status_label.pack(side="left", fill="x", expand=True)
        back_button = ttk.Button(footer, text="Back")
        back_button.pack(side="left", padx=(8, 6))
        next_button = ttk.Button(footer, text="Continue")
        next_button.pack(side="left")

        def complete(value: bool) -> None:
            nonlocal scan_task
            if scan_task:
                scan_task.cancel()
                scan_task = None
            if not result.done():
                result.set_result(value)
            window.destroy()

        def selected_device() -> bool:
            nonlocal selected_name
            selection = device_list.curselection()
            if not selection or selection[0] >= len(devices):
                return False
            selected_name = devices[selection[0]][1]
            device_id = device_id_for_name(selected_name)
            if not device_id:
                return False
            config.paired_device_ids = [device_id]
            status.set(f"Selected XC-{device_id}")
            next_button.configure(state="normal")
            return True

        async def scan() -> None:
            nonlocal devices, scan_task
            scan_button.configure(state="disabled")
            status.set("Scanning for nearby XC devices…")
            try:
                devices = await discover()
                device_list.delete(0, "end")
                for _address, name, rssi in devices:
                    device_list.insert("end", f"{name:<20}  {rssi:>4} dBm")
                if not devices:
                    status.set("No XC device found. Wake the Stick and scan again.")
                else:
                    status.set(
                        f"{len(devices)} device{'s' if len(devices) != 1 else ''} found"
                    )
                    wanted = next(
                        (
                            index
                            for index, (_address, name, _rssi) in enumerate(devices)
                            if name == selected_name
                        ),
                        0,
                    )
                    device_list.selection_set(wanted)
                    device_list.activate(wanted)
                    selected_device()
            except asyncio.CancelledError:
                return
            except Exception as error:
                status.set(f"Bluetooth scan failed: {error}")
            finally:
                scan_button.configure(state="normal")
                scan_task = None

        def start_scan() -> None:
            nonlocal scan_task
            if scan_task is None:
                scan_task = asyncio.create_task(scan())

        def clear_content() -> None:
            for child in content.winfo_children():
                cast(tk.Widget, child).pack_forget()

        def desktop_access_ready() -> bool:
            missing = integration_missing()
            if missing:
                access_status.configure(
                    text="Install these desktop helpers, then check again:\n\n"
                    + "\n".join(f"• {item}" for item in missing),
                    fg="#a62d2d",
                )
                status.set("Desktop access is not ready yet.")
                next_button.configure(state="disabled")
                return False
            access_status.configure(
                text="Clipboard and input-injection helpers are ready.",
                fg="#257442",
            )
            status.set("")
            next_button.configure(state="normal")
            return True

        def render() -> None:
            clear_content()
            status.set("")
            for index, label in enumerate(step_labels):
                label.configure(
                    fg="#171719" if index <= current_step else "#85858b",
                    font=(
                        "Sans",
                        11,
                        "bold" if index == current_step else "normal",
                    ),
                )
            back_button.configure(state="normal" if current_step else "disabled")
            next_button.configure(text="Finish" if current_step == 3 else "Continue")

            if current_step == 0:
                title.configure(text="Pair XC")
                detail.configure(
                    text="Choose a nearby XC-XXXX device. XC Buddy needs a paired device before it can listen."
                )
                device_list.pack(fill="both", expand=True)
                controls = tk.Frame(content, bg="#f5f5f7")
                controls.pack(fill="x", pady=(10, 0))
                scan_button.pack(in_=controls, side="left")
                next_button.configure(
                    state="normal" if config.paired_device_ids else "disabled"
                )
                if not devices:
                    start_scan()
            elif current_step == 1:
                title.configure(text="Configure transcription")
                detail.configure(text="Enter the API key used to transcribe audio.")
                row = tk.Frame(content, bg="#f5f5f7")
                row.pack(fill="x", pady=(18, 0))
                tk.Label(
                    row,
                    text="API Key",
                    bg="#f5f5f7",
                    fg="#68686e",
                    width=11,
                    anchor="e",
                ).pack(side="left", padx=(0, 12))
                key_entry = ttk.Entry(row, textvariable=api_key, show="•", width=42)
                key_entry.pack(side="left", fill="x", expand=True)
                key_entry.focus_set()
                next_button.configure(state="normal")
            elif current_step == 2:
                title.configure(text="Allow text insertion")
                detail.configure(
                    text="XC Buddy pastes recognized text at your cursor, so Linux clipboard and input-injection helpers are required."
                )
                access_status.pack(fill="x", pady=(16, 14))
                check_button.pack(anchor="w")
                desktop_access_ready()
            else:
                title.configure(text="XC Buddy is ready")
                detail.configure(
                    text="The device and transcription settings are configured. Finish setup to start scanning and connecting."
                )
                device = (
                    f"XC-{config.paired_device_ids[0]}"
                    if config.paired_device_ids
                    else "Not paired"
                )
                access = "Ready" if not integration_missing() else "Not ready"
                for summary_label, value in (
                    ("Device", device),
                    ("Transcription", "Configured"),
                    ("Desktop access", access),
                ):
                    tk.Label(
                        content,
                        text=f"{summary_label}:  {value}",
                        bg="#f5f5f7",
                        fg="#171719",
                        anchor="w",
                        font=("Sans", 11),
                        pady=5,
                    ).pack(fill="x")
                next_button.configure(state="normal")

        def go_back() -> None:
            nonlocal current_step
            if current_step:
                config.openai_api_key = api_key.get().strip()
                current_step -= 1
                render()

        def go_next() -> None:
            nonlocal current_step
            status.set("")
            if current_step == 0 and not selected_device():
                status.set("Select an XC device first.")
                return
            if current_step == 1:
                config.openai_api_key = api_key.get().strip()
                if not config.openai_api_key:
                    status.set("Enter the API key used to transcribe audio.")
                    return
            if current_step == 2 and not desktop_access_ready():
                return
            if current_step == 3:
                try:
                    save_config()
                except OSError as error:
                    status.set(f"Save failed: {error}")
                    return
                complete(True)
                return
            current_step += 1
            render()

        device_list = tk.Listbox(
            content,
            height=10,
            activestyle="none",
            exportselection=False,
            font=("Monospace", 11),
            selectbackground="#315efb",
        )
        device_list.bind("<<ListboxSelect>>", lambda _event: selected_device())
        device_list.bind("<Double-Button-1>", lambda _event: go_next())
        scan_button = ttk.Button(content, text="Scan Again", command=start_scan)
        access_status = tk.Label(
            content,
            bg="#f5f5f7",
            justify="left",
            anchor="w",
            wraplength=430,
            font=("Sans", 11),
        )
        check_button = ttk.Button(
            content, text="Check Again", command=desktop_access_ready
        )
        back_button.configure(command=go_back)
        next_button.configure(command=go_next)
        window.bind("<Return>", lambda _event: go_next())
        window.protocol("WM_DELETE_WINDOW", lambda: complete(False))
        window.update_idletasks()
        x = (window.winfo_screenwidth() - window.winfo_width()) // 2
        y = (window.winfo_screenheight() - window.winfo_height()) // 2
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        window.lift()
        window.focus_force()
        render()

        while not result.done():
            try:
                self.root.update()
            except tk.TclError:
                return False
            await asyncio.sleep(0.02)
        return result.result()

    def onboarding_intro(self) -> None:
        messagebox.showinfo(
            "Set Up XC Buddy",
            "Setup has four steps: pair your XC device, enter the transcription "
            "API key, verify desktop input integration, and finish.",
        )

    def request_api_key(self, current: str = "") -> str | None:
        return simpledialog.askstring(
            "Transcription",
            "Enter the API key used to transcribe audio.",
            initialvalue=current,
            show="•",
        )

    def retry_pairing(self) -> bool:
        return messagebox.askretrycancel(
            "Pair XC",
            "An XC device must be paired before setup can continue. Wake the device "
            "and retry, or cancel to quit setup.",
        )

    def desktop_integration_result(self, missing: list[str]) -> bool:
        if missing:
            return messagebox.askretrycancel(
                "Desktop Integration",
                "XC Buddy cannot inject text until these helpers are installed:\n\n"
                + "\n".join(f"• {item}" for item in missing)
                + "\n\nInstall them, then choose Retry.",
            )
        messagebox.showinfo(
            "Desktop Integration", "Clipboard and input-injection helpers are ready."
        )
        return True

    def onboarding_complete(self) -> None:
        messagebox.showinfo(
            "XC Buddy Ready",
            "Setup is complete. XC Buddy will remain available from the system tray.",
        )

    def edit_settings(
        self, config, on_save: Callable, activation_time: int = 0
    ) -> None:
        from .gtk_settings import show_settings

        if self._settings_window is not None:
            present_from_tray(self._settings_window, activation_time)
            return

        logger.debug("Creating XC Buddy Settings window")
        window = show_settings(
            config,
            on_save,
            lambda: self.call(self.on_open_config),
            activation_time,
        )
        self._settings_window = window
        logger.debug("XC Buddy Settings window presented")
        self._gtk_windows.append(window)

        def closed(_window) -> None:
            if window in self._gtk_windows:
                self._gtk_windows.remove(window)
            if self._settings_window is window:
                self._settings_window = None

        window.connect("destroy", closed)

    def _edit_settings_tk(self, config, on_save: Callable) -> None:
        window = tk.Toplevel(self.root)
        window.title("XC Buddy Settings")
        window.geometry("720x650")
        canvas = tk.Canvas(window)
        scroll = ttk.Scrollbar(window, orient="vertical", command=canvas.yview)
        frame = ttk.Frame(canvas, padding=18)
        frame.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        fields: dict[str, tk.StringVar] = {}
        rows = [
            ("Transcription API key", "openai_api_key", config.openai_api_key),
            ("Transcription base URL", "openai_base_url", config.openai_base_url),
            ("Transcription model", "openai_model", config.openai_model),
            ("Language (optional)", "openai_language", config.openai_language),
            ("Transcription prompt", "openai_prompt", config.openai_prompt),
            ("LLM base URL", "llm_base_url", config.llm_base_url),
            ("LLM API key", "llm_api_key", config.llm_api_key),
            ("LLM model", "llm_model", config.llm_model),
            (
                "Hotwords (comma separated)",
                "asr_hotwords",
                ",".join(config.asr_hotwords),
            ),
            (
                "Translation language",
                "translation_target",
                config.output.translation_target,
            ),
            ("Codex bridge port", "codex_bridge_port", str(config.codex_bridge_port)),
            ("Codex bridge token", "codex_bridge_token", config.codex_bridge_token),
            ("Relay URL", "relay_url", config.relay_url),
            ("Relay sender token", "relay_sender_token", config.relay_sender_token),
            (
                "Relay receiver token",
                "relay_receiver_token",
                config.relay_receiver_token,
            ),
            ("Debug audio directory", "debug_audio_dir", str(config.debug_audio_dir)),
            (
                "Display dim seconds",
                "display_dim_seconds",
                str(config.power_timers.display_dim_seconds),
            ),
            (
                "Display off minutes",
                "display_off_seconds",
                _minute_text(config.power_timers.display_off_seconds),
            ),
            (
                "Idle sleep minutes",
                "idle_deep_sleep_seconds",
                _minute_text(config.power_timers.idle_deep_sleep_seconds),
            ),
            (
                "Codex sleep minutes",
                "codex_deep_sleep_seconds",
                _minute_text(config.power_timers.codex_deep_sleep_seconds),
            ),
            (
                "Device colors (ID:color)",
                "device_theme_colors",
                ",".join(f"{k}:{v}" for k, v in config.device_theme_colors.items()),
            ),
            (
                "Overlay positions (ID:position)",
                "device_overlay_positions",
                ",".join(
                    f"{k}:{v}" for k, v in config.device_overlay_positions.items()
                ),
            ),
            (
                "Device text profiles (ID:mode:language)",
                "device_output_profiles",
                ",".join(
                    f"{device_id}:{profile.transform}:{profile.translation_target}"
                    for device_id, profile in config.device_outputs.items()
                ),
            ),
        ]
        for row, (label, key, value) in enumerate(rows):
            ttk.Label(frame, text=label).grid(
                row=row, column=0, sticky="e", padx=8, pady=4
            )
            variable = fields[key] = tk.StringVar(value=value)
            entry = ttk.Entry(
                frame,
                textvariable=variable,
                width=58,
                show="•" if "key" in key or "token" in key else "",
            )
            entry.grid(row=row, column=1, sticky="ew")
            if key == "debug_audio_dir":
                ttk.Button(
                    frame,
                    text="Choose…",
                    command=partial(_choose_directory, variable),
                ).grid(row=row, column=2, padx=(6, 0))
        base = len(rows)
        interaction = tk.StringVar(value=config.interaction_mode)
        relay = tk.StringVar(value=config.relay_mode)
        target = tk.StringVar(value=config.output.target)
        transform = tk.StringVar(value=config.output.transform)
        auto_enter = tk.BooleanVar(value=config.auto_enter)
        chime = tk.BooleanVar(value=config.codex_success_chime)
        debug_audio = tk.BooleanVar(value=config.debug_audio_cache)
        for offset, (label, variable, values) in enumerate(
            (
                ("Interaction", interaction, ("hold_to_talk", "click_to_talk")),
                ("Output", target, ("focused_app", "subtitle")),
                ("Transform", transform, ("original", "translate")),
                ("Relay mode", relay, ("disabled", "sender", "receiver")),
            )
        ):
            ttk.Label(frame, text=label).grid(
                row=base + offset, column=0, sticky="e", padx=8, pady=4
            )
            ttk.Combobox(
                frame, textvariable=variable, values=values, state="readonly"
            ).grid(row=base + offset, column=1, sticky="w")
        ttk.Checkbutton(
            frame, text="Press Enter after paste", variable=auto_enter
        ).grid(row=base + 4, column=1, sticky="w")
        ttk.Checkbutton(
            frame, text="Codex success/approval chime", variable=chime
        ).grid(row=base + 5, column=1, sticky="w")
        ttk.Checkbutton(frame, text="Save debug Ogg audio", variable=debug_audio).grid(
            row=base + 6, column=1, sticky="w"
        )
        ttk.Label(frame, text="Paired devices").grid(
            row=base + 7, column=0, sticky="ne", padx=8, pady=4
        )
        paired = tk.StringVar(value=", ".join(config.paired_device_ids))
        ttk.Entry(frame, textvariable=paired, width=58).grid(row=base + 7, column=1)

        def save() -> None:
            try:
                for key in (
                    "openai_api_key",
                    "openai_base_url",
                    "openai_model",
                    "openai_language",
                    "openai_prompt",
                    "llm_base_url",
                    "llm_api_key",
                    "llm_model",
                    "codex_bridge_token",
                    "relay_url",
                    "relay_sender_token",
                    "relay_receiver_token",
                ):
                    setattr(config, key, fields[key].get().strip())
                config.asr_hotwords = list(
                    dict.fromkeys(
                        x.strip()
                        for x in re.split(r"[,\r\n]", fields["asr_hotwords"].get())
                        if x.strip()
                    )
                )
                try:
                    port = int(float(fields["codex_bridge_port"].get()))
                except (ValueError, OverflowError):
                    port = 0
                config.codex_bridge_port = max(1, min(65535, port))
                config.interaction_mode, config.relay_mode = (
                    interaction.get(),
                    relay.get(),
                )
                config.output.target, config.output.transform = (
                    target.get(),
                    transform.get(),
                )
                config.output.translation_target = (
                    fields["translation_target"].get().strip() or "en"
                )
                config.auto_enter, config.codex_success_chime = (
                    auto_enter.get(),
                    chime.get(),
                )
                config.debug_audio_cache = debug_audio.get()
                from pathlib import Path

                config.debug_audio_dir = Path(
                    fields["debug_audio_dir"].get()
                ).expanduser()
                timer_ranges = {
                    "display_dim_seconds": (5, 3600, 1),
                    "display_off_seconds": (30, 86400, 60),
                    "idle_deep_sleep_seconds": (60, 86400, 60),
                    "codex_deep_sleep_seconds": (60, 86400, 60),
                }
                for key, (low, high, multiplier) in timer_ranges.items():
                    try:
                        raw = float(fields[key].get())
                    except ValueError:
                        raw = 0
                    if not math.isfinite(raw):
                        value = low
                    elif multiplier == 1:
                        value = max(low, min(high, int(raw)))
                    else:
                        value = max(low, min(high, math.floor(raw * multiplier + 0.5)))
                    setattr(config.power_timers, key, value)
                from .config import normalize_device_id

                config.paired_device_ids = list(
                    dict.fromkeys(
                        filter(
                            None,
                            (normalize_device_id(x) for x in paired.get().split(",")),
                        )
                    )
                )

                def parse_map(text: str, allowed: set[str]) -> dict[str, str]:
                    result = {}
                    for item in text.split(","):
                        if ":" not in item:
                            continue
                        raw_id, value = item.split(":", 1)
                        normalized_value = value.strip().lower()
                        if (
                            device_id := normalize_device_id(raw_id)
                        ) and normalized_value in allowed:
                            result[device_id] = normalized_value
                    return result

                config.device_theme_colors = parse_map(
                    fields["device_theme_colors"].get(), set(THEME_NAMES)
                )
                config.device_overlay_positions = parse_map(
                    fields["device_overlay_positions"].get(), set(POSITION_NAMES)
                )
                from .config import OutputProfile

                config.device_outputs = {}
                for item in fields["device_output_profiles"].get().split(","):
                    parts = [part.strip() for part in item.split(":", 2)]
                    if len(parts) < 2 or not (
                        device_id := normalize_device_id(parts[0])
                    ):
                        continue
                    mode = (
                        parts[1]
                        if parts[1] in ("original", "translate")
                        else "original"
                    )
                    language = (
                        parts[2]
                        if len(parts) == 3 and parts[2]
                        else config.output.translation_target
                    )
                    config.device_outputs[device_id] = OutputProfile(
                        config.output.target, mode, language
                    )
                on_save()
                window.destroy()
            except Exception as error:
                messagebox.showerror("Invalid settings", str(error), parent=window)

        ttk.Button(
            frame,
            text="Open Config Folder",
            command=lambda: self.call(self.on_open_config),
        ).grid(row=base + 8, column=0, sticky="w", pady=18)
        ttk.Button(frame, text="Save and Apply", command=save).grid(
            row=base + 8, column=1, sticky="e", pady=18
        )

    def shutdown(self) -> None:
        self.hide_overlay()
        self.hide_subtitles()
        for device_window in tuple(self._device_windows.values()):
            device_window.window.destroy()
        self._device_windows.clear()
        if self._control_window is not None:
            self._control_window.destroy()
            self._control_window = None
        if self._runtime_presenter is not None:
            self._runtime_presenter.destroy()
            self._runtime_presenter = None
        if self.tray:
            self.tray.stop()
