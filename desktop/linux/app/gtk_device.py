from __future__ import annotations

from typing import Any

from .gtk_layout import apply_typography, present_from_tray, scale_window
from .gtk_onboarding import _gtk_module, _label


class DeviceWindow:
    """One device's StatusController submenu hierarchy as a GTK window."""

    def __init__(
        self,
        ui: Any,
        device_id: str,
        theme_names: dict[str, str],
        position_names: dict[str, str],
        translation_targets: tuple[tuple[str, str], ...],
    ) -> None:
        self._ui = ui
        self._device_id = device_id
        self._Gtk = _gtk_module()
        self._refreshing = False

        Gtk = self._Gtk
        self.window = Gtk.Window(title=ui._device_name(device_id))
        self.window.set_default_size(420, 310)
        self.window.set_size_request(420, 310)
        scale_window(self.window, 420, 310)
        apply_typography(Gtk, self.window)
        self.window.set_resizable(False)
        self.window.set_position(Gtk.WindowPosition.CENTER)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(16)
        page.set_margin_bottom(16)
        page.set_margin_start(16)
        page.set_margin_end(16)
        self.window.add(page)

        self._state = _label(Gtk, "", secondary=True)
        self._state.get_style_context().add_class("heading")
        page.pack_start(self._state, False, False, 0)
        page.pack_start(Gtk.Separator(), False, False, 0)

        self._theme = Gtk.ComboBoxText()
        for value, label in theme_names.items():
            self._theme.append(value, label)
        self._theme.connect("changed", self._theme_changed)
        self._row(page, "Overlay Color", self._theme)

        self._position = Gtk.ComboBoxText()
        for value, label in position_names.items():
            self._position.append(value, label)
        self._position.connect("changed", self._position_changed)
        self._row(page, "Overlay Position", self._position)

        self._translation = Gtk.ComboBoxText()
        self._translation.append("original", "Original")
        for code, name in translation_targets:
            self._translation.append(f"translate:{code}", f"Translate to {name}")
        self._translation.connect("changed", self._translation_changed)
        self._row(page, "Translation", self._translation)

        page.pack_start(Gtk.Separator(), False, False, 0)
        self._firmware_status = _label(Gtk, "", secondary=True)
        self._firmware_status.set_line_wrap(True)
        page.pack_start(self._firmware_status, False, False, 0)
        self._firmware_action = Gtk.Button()
        self._firmware_action.set_no_show_all(True)
        self._firmware_action.connect("clicked", self._firmware_clicked)
        page.pack_start(self._firmware_action, False, False, 0)

        page.pack_start(Gtk.Separator(), False, False, 0)
        self._forget = Gtk.Button.new_with_label("Forget This Device")
        self._forget.get_style_context().add_class("destructive-action")
        self._forget.connect("clicked", self._forget_clicked)
        page.pack_end(self._forget, False, False, 0)

    def _row(self, page: Any, title: str, control: Any) -> None:
        line = self._Gtk.Box(
            orientation=self._Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        label = _label(self._Gtk, title, secondary=True)
        label.set_xalign(0)
        label.set_size_request(140, -1)
        line.pack_start(label, False, False, 0)
        line.pack_start(control, True, True, 0)
        page.pack_start(line, False, False, 0)

    def present(self, activation_time: int = 0) -> None:
        self.refresh()
        present_from_tray(self.window, activation_time)

    def _theme_changed(self, combo: Any) -> None:
        if not self._refreshing and (value := combo.get_active_id()):
            self._ui.call(self._ui.on_set_theme, self._device_id, value)

    def _position_changed(self, combo: Any) -> None:
        if not self._refreshing and (value := combo.get_active_id()):
            self._ui.call(self._ui.on_set_position, self._device_id, value)

    def _translation_changed(self, combo: Any) -> None:
        if self._refreshing:
            return
        value = combo.get_active_id()
        if value == "original":
            self._ui.call(self._ui.on_set_translation, self._device_id, "original", "")
        elif value and value.startswith("translate:"):
            self._ui.call(
                self._ui.on_set_translation,
                self._device_id,
                "translate",
                value.removeprefix("translate:"),
            )

    def _firmware_clicked(self, _button: Any) -> None:
        self._ui._request_firmware_update(self._device_id)

    def _forget_clicked(self, _button: Any) -> None:
        self._ui.call(self._ui.on_forget, self._device_id)
        self.window.destroy()

    def refresh(self) -> None:
        config = self._ui._config
        connected = self._device_id in self._ui._connected_device_ids()
        self._refreshing = True
        try:
            self.window.set_title(self._ui._device_name(self._device_id))
            self._state.set_text(self._ui._device_state(self._device_id))
            self._theme.set_sensitive(config is not None)
            self._position.set_sensitive(config is not None)
            self._translation.set_sensitive(config is not None)
            self._forget.set_sensitive(config is not None)
            self._theme.set_active_id(
                self._ui._theme_colors.get(self._device_id, "white")
            )
            self._position.set_active_id(
                self._ui._overlay_positions.get(self._device_id, "center")
            )
            translation = "original"
            if config is not None:
                profile = config.profile_for(self._device_id)
                if profile.transform == "translate":
                    translation = f"translate:{profile.translation_target}"
            self._translation.set_active_id(translation)

            self._firmware_status.set_text(self._ui._firmware_label(self._device_id))
            info = self._ui._device_info.get(self._device_id, {})
            if self._ui._updating_device == self._device_id:
                self._firmware_action.hide()
            elif self._ui._firmware_checking:
                self._firmware_action.set_label("Checking for Updates...")
                self._firmware_action.set_sensitive(False)
                self._firmware_action.show()
            elif info.get("update_available"):
                self._firmware_action.set_label(
                    f"Update to {self._ui._latest_firmware}..."
                )
                self._firmware_action.set_sensitive(connected)
                self._firmware_action.show()
            elif self._ui._latest_firmware is not None and info.get("firmware_version"):
                self._firmware_action.set_label("Firmware Up to Date")
                self._firmware_action.set_sensitive(False)
                self._firmware_action.show()
            else:
                self._firmware_action.hide()
        finally:
            self._refreshing = False
