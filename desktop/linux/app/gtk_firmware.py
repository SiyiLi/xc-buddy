from __future__ import annotations

import time
from typing import Callable

from .gtk_onboarding import _gtk_module, _label
from .gtk_layout import apply_typography, scale_window


class FirmwareWindow:
    """Port of FirmwareUpdateWindowController.swift using native GTK widgets."""

    def __init__(
        self,
        file_name: str,
        on_cancel: Callable[[], None],
        activation_time: int = 0,
    ) -> None:
        Gtk = _gtk_module()
        self.Gtk = Gtk
        self.on_cancel = on_cancel
        self.started_at = time.monotonic()
        self.confirmed_bytes = 0

        self.window = Gtk.Window(title="Firmware Update")
        self.window.set_default_size(420, 190)
        self.window.set_size_request(420, 190)
        scale_window(self.window, 420, 190)
        apply_typography(Gtk, self.window)
        self.window.set_resizable(False)
        self.window.set_deletable(False)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        stack.set_margin_top(22)
        stack.set_margin_bottom(18)
        stack.set_margin_start(24)
        stack.set_margin_end(24)
        self.window.add(stack)

        self.title = _label(Gtk, "Updating Firmware")
        self.title.get_style_context().add_class("heading")
        self.detail = _label(Gtk, file_name, secondary=True)
        self.detail.set_ellipsize(2)
        stack.pack_start(self.title, False, False, 0)
        stack.pack_start(self.detail, False, False, 0)

        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.progress = Gtk.ProgressBar()
        self.percent = _label(Gtk, "0%")
        self.percent.set_xalign(1)
        self.percent.set_size_request(42, -1)
        progress_row.pack_start(self.progress, True, True, 0)
        progress_row.pack_start(self.percent, False, False, 0)
        stack.pack_start(progress_row, False, False, 0)

        details = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.speed = _label(Gtk, "Speed --", secondary=True)
        self.remaining = _label(Gtk, "Estimating time remaining", secondary=True)
        details.pack_start(self.speed, False, False, 0)
        details.pack_start(self.remaining, False, False, 0)
        stack.pack_start(details, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.cancel = Gtk.Button.new_with_label("Cancel")
        self.close = Gtk.Button.new_with_label("Close")
        self.close.set_sensitive(False)
        buttons.pack_start(Gtk.Box(), True, True, 0)
        buttons.pack_start(self.cancel, False, False, 0)
        buttons.pack_start(self.close, False, False, 0)
        stack.pack_end(buttons, False, False, 0)
        self.cancel.connect("clicked", self._cancel)
        self.close.connect("clicked", lambda _button: self.window.destroy())
        self.window.show_all()
        if activation_time:
            self.window.present_with_time(activation_time)
        else:
            self.window.present()

    @staticmethod
    def _format_rate(rate: float) -> str:
        if rate >= 1024 * 1024:
            return f"{rate / (1024 * 1024):.1f} MiB/s"
        return f"{rate / 1024:.1f} KiB/s"

    def update(self, written: int, total: int, confirmed: bool) -> None:
        if confirmed:
            self.confirmed_bytes = max(self.confirmed_bytes, written)
        displayed = (
            self.confirmed_bytes
            if confirmed
            else max(
                self.confirmed_bytes, min(written, self.confirmed_bytes + 64 * 1024)
            )
        )
        fraction = min(max(displayed / max(total, 1), 0), 1)
        self.progress.set_fraction(fraction)
        self.percent.set_text(f"{int(fraction * 100)}%")
        elapsed = max(0.1, time.monotonic() - self.started_at)
        rate = displayed / elapsed
        self.speed.set_text(f"Speed {self._format_rate(rate)}")
        if rate > 1 and displayed < total:
            seconds = max(0, round((total - displayed) / rate))
            eta = f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60}s"
            self.remaining.set_text(f"{eta} remaining")
        elif displayed >= total:
            self.remaining.set_text("Finishing on device")

    def finish(self, error: str | None = None) -> None:
        self.cancel.set_sensitive(False)
        self.close.set_sensitive(True)
        if error is None:
            self.title.set_text("Firmware Updated")
            self.detail.set_text("The device is rebooting into the new firmware.")
            self.progress.set_fraction(1)
            self.percent.set_text("100%")
            self.remaining.set_text("Done")
        else:
            self.title.set_text("Update Failed")
            self.detail.set_text(error)
            self.remaining.set_text("The device kept its current firmware.")

    def _cancel(self, _button) -> None:
        self.cancel.set_sensitive(False)
        self.title.set_text("Cancelling Firmware Update")
        self.detail.set_text("Stopping transfer and asking the device to abort.")
        self.remaining.set_text("Cancelling")
        self.on_cancel()
