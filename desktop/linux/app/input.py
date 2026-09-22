from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


class InputError(RuntimeError):
    pass


class InputInjector:
    """Clipboard-preserving text injection for X11 and Wayland desktops."""

    def paste(self, text: str, press_enter: bool) -> None:
        if not text:
            return
        if os.environ.get("WAYLAND_DISPLAY"):
            self._wayland(text, press_enter)
        elif os.environ.get("DISPLAY"):
            self._x11(text, press_enter)
        else:
            raise InputError(
                "no graphical session; select subtitle output or run inside a desktop session"
            )

    def frontmost_application_is_codex(self) -> bool:
        return "codex" in self._frontmost_application().lower()

    @staticmethod
    def _is_gnome_wayland() -> bool:
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        return bool(
            os.environ.get("WAYLAND_DISPLAY")
            and (os.environ.get("GNOME_SHELL_SESSION_MODE") or "gnome" in desktop)
        )

    @staticmethod
    def _gi():
        """Load the Ubuntu GI bindings used by the GTK desktop windows."""
        try:
            import gi
        except ImportError:
            system_packages = Path("/usr/lib/python3/dist-packages")
            if system_packages.is_dir() and str(system_packages) not in sys.path:
                sys.path.append(str(system_packages))
            import gi
        return gi

    @classmethod
    def atspi_available(cls) -> bool:
        try:
            gi = cls._gi()
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            return Atspi is not None
        except (ImportError, ValueError):
            return False

    def _frontmost_application(self) -> str:
        try:
            if (
                os.environ.get("DISPLAY")
                and not os.environ.get("WAYLAND_DISPLAY")
                and shutil.which("xdotool")
            ):
                window = self._output(["xdotool", "getactivewindow"]).strip()
                return self._output(
                    ["xdotool", "getwindowclassname", window]
                ) + self._output(["xdotool", "getwindowname", window])
            if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which(
                "hyprctl"
            ):
                active = json.loads(self._output(["hyprctl", "activewindow", "-j"]))
                return " ".join(
                    str(active.get(key, ""))
                    for key in ("class", "initialClass", "title")
                )
            if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
                tree = json.loads(self._output(["swaymsg", "-t", "get_tree", "-r"]))
                focused_node = self._focused_sway_node(tree)
                if focused_node:
                    properties = focused_node.get("window_properties") or {}
                    return " ".join(
                        str(value)
                        for value in (
                            focused_node.get("app_id", ""),
                            focused_node.get("name", ""),
                            properties.get("class", ""),
                            properties.get("title", ""),
                        )
                    )
            if os.environ.get("KDE_FULL_SESSION") and shutil.which("kdotool"):
                window = self._output(["kdotool", "getactivewindow"]).strip()
                return self._output(
                    ["kdotool", "getwindowclassname", window]
                ) + self._output(["kdotool", "getwindowname", window])
            if self._is_gnome_wayland():
                # GNOME intentionally denies Shell.Introspect.GetWindows to
                # ordinary clients. AT-SPI exposes the actually focused UI
                # without relying on a privileged Shell API.
                return self._focused_accessible_application()
            if os.environ.get("DISPLAY") and shutil.which("xdotool"):
                window = self._output(["xdotool", "getactivewindow"]).strip()
                return self._output(
                    ["xdotool", "getwindowclassname", window]
                ) + self._output(["xdotool", "getwindowname", window])
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
        return ""

    @staticmethod
    def _output(command: list[str]) -> str:
        return subprocess.check_output(
            command,
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _focused_accessible_application() -> str:
        try:
            gi = InputInjector._gi()
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            Atspi.init()
            desktop = Atspi.get_desktop(0)
            remaining = 4096

            def focused(node, path: tuple[str, ...], depth: int) -> str:
                nonlocal remaining
                if remaining <= 0 or depth > 32:
                    return ""
                remaining -= 1
                try:
                    name = (node.get_name() or "").strip()
                    role = (node.get_role_name() or "").strip()
                    next_path = (*path, name) if name else path
                    states = node.get_state_set()
                    if states.contains(Atspi.StateType.FOCUSED):
                        application = node.get_application()
                        app_name = (
                            (application.get_name() or "").strip()
                            if application is not None
                            else ""
                        )
                        details = " ".join(part for part in next_path if part)
                        descriptor = f"{app_name} {details} role={role}".strip()
                        # GNOME Shell keeps one of its own stage objects marked
                        # focused alongside the focused application. It is not
                        # the user's text destination, so keep traversing.
                        if app_name.lower() != "gnome-shell":
                            return descriptor
                    count = max(0, node.get_child_count())
                except Exception:
                    return ""
                for index in range(count):
                    try:
                        child = node.get_child_at_index(index)
                    except Exception:
                        continue
                    if child is not None and (
                        result := focused(child, next_path, depth + 1)
                    ):
                        return result
                return ""

            return focused(desktop, (), 0)
        except (ImportError, ValueError, RuntimeError):
            return ""

    @classmethod
    def _focused_sway_node(cls, node: dict) -> dict | None:
        if node.get("focused"):
            return node
        for child in (*node.get("nodes", []), *node.get("floating_nodes", [])):
            if focused := cls._focused_sway_node(child):
                return focused
        return None

    @staticmethod
    def _focused_gnome_window(gvariant: str) -> str:
        for properties in re.findall(r"\{([^{}]+)\}", gvariant):
            if re.search(r"['\"]has-focus['\"]\s*:\s*<true>", properties):
                return properties
        return ""

    def _wayland(self, text: str, press_enter: bool) -> None:
        try:
            restore_clipboard = self._wayland_clipboard(text)
        except (
            ImportError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            raise InputError(f"Wayland clipboard failed: {error}") from error
        if self._is_gnome_wayland():
            focused = self._focused_accessible_application()
            try:
                # GNOME requests Wayland clipboard data asynchronously. Give
                # wl-copy ownership time to propagate before synthesizing the
                # paste accelerator.
                time.sleep(0.2)
                self._atspi_paste(
                    press_enter,
                    terminal=self._requires_terminal_paste(focused),
                )
            except (
                ImportError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                raise InputError(f"GNOME Wayland paste failed: {error}") from error
        elif shutil.which("wtype"):
            subprocess.run(["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"], check=True)
            if press_enter:
                time.sleep(0.12)
                subprocess.run(["wtype", "-k", "Return"], check=True)
        elif shutil.which("ydotool"):
            subprocess.run(
                ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], check=True
            )
            if press_enter:
                time.sleep(0.12)
                subprocess.run(["ydotool", "key", "28:1", "28:0"], check=True)
        else:
            raise InputError("Wayland input needs wtype or ydotool")
        time.sleep(0.08 if press_enter else 0.2)
        restore_clipboard()

    def _wayland_clipboard(self, text: str):
        if shutil.which("wl-copy") and shutil.which("wl-paste"):
            old = subprocess.run(
                ["wl-paste", "--no-newline"], capture_output=True
            ).stdout
            subprocess.run(
                ["wl-copy", "--type", "text/plain;charset=utf-8"],
                input=text.encode(),
                check=True,
            )

            def restore() -> None:
                if self._clipboard_matches(["wl-paste", "--no-newline"], text):
                    subprocess.run(["wl-copy"], input=old, check=False)

            return restore
        return self._gtk_clipboard_text(text)

    @classmethod
    def _gtk_clipboard(cls):
        gi = cls._gi()
        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, Gtk

        return Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    @classmethod
    def gtk_clipboard_available(cls) -> bool:
        """Return whether the GTK fallback can access this desktop clipboard."""
        try:
            return cls._gtk_main(cls._gtk_clipboard) is not None
        except (ImportError, OSError, RuntimeError, ValueError):
            return False

    @classmethod
    def _gtk_main(cls, callback):
        if threading.current_thread() is threading.main_thread():
            return callback()
        cls._gi()
        from gi.repository import GLib

        completed = threading.Event()
        result = []

        def invoke():
            try:
                result.append((True, callback()))
            except Exception as error:
                result.append((False, error))
            finally:
                completed.set()
            return False

        GLib.idle_add(invoke)
        if not completed.wait(2):
            raise RuntimeError("GTK clipboard did not respond")
        success, value = result[0]
        if success:
            return value
        raise value

    @classmethod
    def _gtk_clipboard_text(cls, text: str):
        def replace():
            clipboard = cls._gtk_clipboard()
            old = clipboard.wait_for_text()
            clipboard.set_text(text, -1)
            return old

        old = cls._gtk_main(replace)

        def restore() -> None:
            def restore_if_unchanged() -> None:
                clipboard = cls._gtk_clipboard()
                if clipboard.wait_for_text() == text:
                    clipboard.set_text(old or "", -1)

            cls._gtk_main(restore_if_unchanged)

        return restore

    @staticmethod
    def _requires_terminal_paste(focused: str) -> bool:
        value = focused.lower()
        return "role=terminal" in value or any(
            name in value
            for name in (
                "gnome-terminal",
                "kgx",
                "konsole",
                "kitty",
                "alacritty",
                "wezterm",
            )
        )

    @classmethod
    def _atspi_paste(cls, press_enter: bool, terminal: bool) -> None:
        if not shutil.which("xmodmap"):
            raise RuntimeError(
                "xmodmap is required to resolve the active keyboard layout"
            )
        mapping = cls._output(["xmodmap", "-pk"])
        keycodes = {
            "control": cls._x_keycode(mapping, "Control_L", "Control_R"),
            "shift": cls._x_keycode(mapping, "Shift_L", "Shift_R"),
            "v": cls._x_keycode(mapping, "v", "V"),
            "return": cls._x_keycode(mapping, "Return", "KP_Enter"),
        }

        gi = cls._gi()
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        Atspi.init()
        modifiers = [keycodes["control"]]
        if terminal:
            modifiers.append(keycodes["shift"])
        pressed: list[int] = []
        try:
            for keycode in modifiers:
                cls._atspi_event(Atspi, keycode, Atspi.KeySynthType.PRESS)
                pressed.append(keycode)
            cls._atspi_event(
                Atspi,
                keycodes["v"],
                Atspi.KeySynthType.PRESSRELEASE,
            )
        finally:
            for keycode in reversed(pressed):
                cls._atspi_event(Atspi, keycode, Atspi.KeySynthType.RELEASE)
        if press_enter:
            # GNOME Terminal completes Ctrl+Shift+V asynchronously; sending
            # Return sooner can submit an empty line while the paste arrives.
            time.sleep(0.3)
            cls._atspi_event(
                Atspi,
                keycodes["return"],
                Atspi.KeySynthType.PRESSRELEASE,
            )

    @staticmethod
    def _atspi_event(atspi, keycode: int, event_type) -> None:
        if atspi.generate_keyboard_event(keycode, "", event_type) is False:
            raise RuntimeError(f"AT-SPI rejected keyboard event for keycode {keycode}")

    @staticmethod
    def _x_keycode(mapping: str, *keysyms: str) -> int:
        requested = set(keysyms)
        for line in mapping.splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2 or not fields[0].isdigit():
                continue
            symbols = set(re.findall(r"\(([^)]+)\)", fields[1]))
            if requested & symbols:
                # xmodmap reports XKB keycodes. AT-SPI's Wayland keyboard
                # synthesizer consumes Linux evdev keycodes, whose standard
                # XKB representation carries an offset of eight.
                keycode = int(fields[0]) - 8
                if keycode > 0:
                    return keycode
                break
        raise RuntimeError(f"active keyboard layout has no key for {'/'.join(keysyms)}")

    def _x11(self, text: str, press_enter: bool) -> None:
        if not shutil.which("xdotool"):
            raise InputError("X11 input needs xdotool")
        clipboard = (
            "xclip"
            if shutil.which("xclip")
            else "xsel"
            if shutil.which("xsel")
            else None
        )
        if clipboard == "xclip":
            old = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"], capture_output=True
            ).stdout
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-i"],
                input=text.encode(),
                check=True,
            )
        elif clipboard == "xsel":
            old = subprocess.run(
                ["xsel", "--clipboard", "--output"], capture_output=True
            ).stdout
            subprocess.run(
                ["xsel", "--clipboard", "--input"], input=text.encode(), check=True
            )
        else:
            raise InputError("X11 input needs xclip or xsel")
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], check=True)
        if press_enter:
            time.sleep(0.12)
            subprocess.run(["xdotool", "key", "--clearmodifiers", "Return"], check=True)
        time.sleep(0.08 if press_enter else 0.2)
        restore = (
            [clipboard, "-selection", "clipboard", "-i"]
            if clipboard == "xclip"
            else [clipboard, "--clipboard", "--input"]
        )
        read = (
            [clipboard, "-selection", "clipboard", "-o"]
            if clipboard == "xclip"
            else [clipboard, "--clipboard", "--output"]
        )
        if self._clipboard_matches(read, text):
            subprocess.run(restore, input=old, check=False)

    @staticmethod
    def _clipboard_matches(command: list[str], expected: str) -> bool:
        try:
            current = subprocess.run(command, capture_output=True, check=True).stdout
            return current == expected.encode()
        except (OSError, subprocess.SubprocessError):
            return False
