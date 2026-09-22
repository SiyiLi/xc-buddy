from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .gtk_layout import apply_typography, present_from_tray, scale_window
from .gtk_onboarding import _gtk_module, _label


async def confirm_app_update(
    current_version: str,
    latest_version: str,
    activation_time: int = 0,
    on_open: Callable[[Any], None] | None = None,
) -> bool:
    Gtk = _gtk_module()
    dialog = Gtk.MessageDialog(
        transient_for=None,
        flags=Gtk.DialogFlags.MODAL,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.NONE,
        text="A new version of XC Buddy is available!",
    )
    apply_typography(Gtk, dialog)
    dialog.format_secondary_text(
        f"XC Buddy {latest_version} is now available—you have "
        f"{current_version}. Would you like to install it now?"
    )
    dialog.add_button("Later", Gtk.ResponseType.CANCEL)
    dialog.add_button("Install Update", Gtk.ResponseType.ACCEPT)
    result = asyncio.get_running_loop().create_future()

    def responded(_dialog, response) -> None:
        if not result.done():
            result.set_result(response == Gtk.ResponseType.ACCEPT)
        dialog.destroy()

    dialog.connect("response", responded)
    dialog.show_all()
    if on_open is not None:
        on_open(dialog)
    present_from_tray(dialog, activation_time)
    return bool(await result)


def show_update_message(title: str, detail: str, activation_time: int = 0) -> Any:
    Gtk = _gtk_module()
    dialog = Gtk.MessageDialog(
        transient_for=None,
        flags=Gtk.DialogFlags.MODAL,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.CLOSE,
        text=title,
    )
    apply_typography(Gtk, dialog)
    dialog.format_secondary_text(detail)
    dialog.connect("response", lambda window, _response: window.destroy())
    dialog.show_all()
    present_from_tray(dialog, activation_time)
    return dialog


class AppUpdateWindow:
    def __init__(self, activation_time: int = 0) -> None:
        Gtk = _gtk_module()
        self.window = Gtk.Window(title="App Update")
        self.window.set_default_size(420, 150)
        self.window.set_size_request(420, 150)
        scale_window(self.window, 420, 150)
        apply_typography(Gtk, self.window)
        self.window.set_resizable(False)
        self.window.set_deletable(False)
        self.window.set_position(Gtk.WindowPosition.CENTER)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(22)
        page.set_margin_bottom(18)
        page.set_margin_start(24)
        page.set_margin_end(24)
        self.window.add(page)

        self.title = _label(Gtk, "Updating XC Buddy")
        self.title.get_style_context().add_class("heading")
        self.detail = _label(
            Gtk, "Downloading and verifying the update...", secondary=True
        )
        self.detail.set_line_wrap(True)
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        self.close = Gtk.Button.new_with_label("Close")
        self.close.set_no_show_all(True)
        self.close.connect("clicked", lambda _button: self.window.destroy())

        page.pack_start(self.title, False, False, 0)
        page.pack_start(self.detail, False, False, 0)
        page.pack_start(self.spinner, False, False, 0)
        page.pack_end(self.close, False, False, 0)
        self.window.show_all()
        present_from_tray(self.window, activation_time)

    def restarting(self) -> None:
        self.title.set_text("Update Installed")
        self.detail.set_text("Restarting XC Buddy...")

    def fail(self, message: str) -> None:
        self.spinner.stop()
        self.spinner.hide()
        self.title.set_text("Update Failed")
        self.detail.set_text(message)
        self.window.set_deletable(True)
        self.close.show()
