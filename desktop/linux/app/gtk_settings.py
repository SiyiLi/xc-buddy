from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

from .build_identity import package_version
from .gtk_onboarding import _gtk_module, _label
from .gtk_layout import apply_typography, present_from_tray, scale_window


def show_settings(
    config: Any,
    on_save: Callable[[], None],
    on_open_config: Callable[[], None],
    activation_time: int = 0,
):
    """Port of SettingsWindowController.swift using native GTK widgets."""
    Gtk = _gtk_module()
    window = Gtk.Window(title="XC Buddy Settings")
    window.set_default_size(560, 640)
    window.set_size_request(560, 640)
    scale_window(window, 560, 640)
    apply_typography(Gtk, window)
    window.set_resizable(False)
    window.set_position(Gtk.WindowPosition.CENTER)

    css = Gtk.CssProvider()
    css.load_from_data(
        b"""
        .xc-settings entry, .xc-settings button {
            min-height: 24px;
            padding: 2px 6px;
        }
        .xc-settings tab {
            min-height: 24px;
            padding: 3px 8px;
        }
        """
    )
    Gtk.StyleContext.add_provider_for_screen(
        window.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    window.get_style_context().add_class("xc-settings")

    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    root.set_margin_top(24)
    root.set_margin_bottom(24)
    root.set_margin_start(24)
    root.set_margin_end(24)
    window.add(root)

    notebook = Gtk.Notebook()
    notebook.set_size_request(-1, 520)
    root.pack_start(notebook, True, True, 0)

    fields: dict[str, Any] = {}

    def entry(key: str, value: object, *, secure: bool = False):
        control = Gtk.Entry()
        control.set_text(str(value))
        control.set_visibility(not secure)
        # Gtk.Entry otherwise derives its natural width from long URL/prompt
        # values and expands the whole fixed-size window beyond the Mac frame.
        control.set_width_chars(1)
        control.set_size_request(300, -1)
        fields[key] = control
        return control

    def page() -> Any:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        return box

    def section(box, text: str) -> None:
        label = _label(Gtk, text, secondary=True)
        label.get_style_context().add_class("heading")
        box.pack_start(label, False, False, 0)

    def row(box, text: str, control) -> None:
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = _label(Gtk, text, secondary=True)
        label.set_xalign(1)
        label.set_size_request(120, -1)
        line.pack_start(label, False, False, 0)
        line.pack_start(control, True, True, 0)
        box.pack_start(line, False, False, 0)

    def hint(box, text: str) -> None:
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        spacer = Gtk.Box()
        spacer.set_size_request(120, -1)
        label = _label(Gtk, text, secondary=True)
        label.get_style_context().add_class("xc-caption")
        label.set_line_wrap(True)
        label.set_max_width_chars(36)
        label.set_size_request(300, -1)
        line.pack_start(spacer, False, False, 0)
        line.pack_start(label, True, True, 0)
        box.pack_start(line, False, False, 0)

    ai_page = page()
    section(ai_page, "Transcription")
    row(ai_page, "API Key", entry("openai_api_key", config.openai_api_key))
    row(ai_page, "Base URL", entry("openai_base_url", config.openai_base_url))
    row(ai_page, "Model", entry("openai_model", config.openai_model))
    row(ai_page, "Prompt", entry("openai_prompt", config.openai_prompt))
    hotwords = Gtk.TextView()
    hotwords.get_buffer().set_text(",".join(config.asr_hotwords))
    hotwords.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    hotwords_scroll = Gtk.ScrolledWindow()
    hotwords_scroll.set_shadow_type(Gtk.ShadowType.IN)
    hotwords_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    hotwords_scroll.set_size_request(300, 78)
    hotwords_scroll.add(hotwords)
    row(ai_page, "Hotwords", hotwords_scroll)
    hint(ai_page, "Separate hotwords with commas or new lines.")
    section(ai_page, "LLM")
    row(ai_page, "Base URL", entry("llm_base_url", config.llm_base_url))
    row(ai_page, "API Key", entry("llm_api_key", config.llm_api_key))
    row(ai_page, "Model", entry("llm_model", config.llm_model))
    notebook.append_page(ai_page, Gtk.Label(label="AI Services"))

    relay_page = page()
    section(relay_page, "Codex Bridge (Loopback Only)")
    row(
        relay_page,
        "Port",
        entry("codex_bridge_port", config.codex_bridge_port),
    )
    codex_chime = Gtk.CheckButton.new_with_label("Play on Stick when Codex finishes")
    codex_chime.set_active(config.codex_success_chime)
    row(relay_page, "Codex Chimes", codex_chime)
    section(relay_page, "XC Body Relay")
    row(relay_page, "Relay URL", entry("relay_url", config.relay_url))
    row(
        relay_page,
        "Sender Token",
        entry("relay_sender_token", config.relay_sender_token, secure=True),
    )
    row(
        relay_page,
        "Receiver Token",
        entry("relay_receiver_token", config.relay_receiver_token, secure=True),
    )
    hint(relay_page, "Tokens are stored with the local XC Buddy configuration.")
    notebook.append_page(relay_page, Gtk.Label(label="Codex & Relay"))

    advanced_page = page()
    section(advanced_page, "Stick Power")

    def timer_row(text: str, timers: tuple[tuple[str, str, str, str], ...]) -> None:
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        for name, key, value, unit in timers:
            timer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            name_label = _label(Gtk, name, secondary=True)
            name_label.set_xalign(1)
            name_label.set_size_request(44, -1)
            control = entry(key, value)
            control.set_alignment(1)
            control.set_size_request(52, -1)
            unit_label = _label(Gtk, unit, secondary=True)
            unit_label.set_size_request(24, -1)
            timer.pack_start(name_label, False, False, 0)
            timer.pack_start(control, False, False, 0)
            timer.pack_start(unit_label, False, False, 0)
            controls.pack_start(timer, False, False, 0)
        row(advanced_page, text, controls)

    def minute_text(seconds: int) -> str:
        if seconds % 60 == 0:
            return str(seconds // 60)
        return f"{seconds / 60:.2f}".rstrip("0").rstrip(".")

    timers = config.power_timers
    timer_row(
        "Display",
        (
            ("Dim", "display_dim_seconds", str(timers.display_dim_seconds), "sec"),
            (
                "Off",
                "display_off_seconds",
                minute_text(timers.display_off_seconds),
                "min",
            ),
        ),
    )
    timer_row(
        "Deep Sleep",
        (
            (
                "Idle",
                "idle_deep_sleep_seconds",
                minute_text(timers.idle_deep_sleep_seconds),
                "min",
            ),
            (
                "Codex",
                "codex_deep_sleep_seconds",
                minute_text(timers.codex_deep_sleep_seconds),
                "min",
            ),
        ),
    )
    section(advanced_page, "Debug")
    debug_audio = Gtk.CheckButton.new_with_label("Save debug audio files")
    debug_audio.set_active(config.debug_audio_cache)
    row(advanced_page, "Audio Cache", debug_audio)
    directory = entry("debug_audio_dir", config.debug_audio_dir)
    directory.set_size_request(260, -1)
    directory.set_editable(False)
    directory_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    directory_row.pack_start(directory, True, True, 0)
    choose_button = Gtk.Button.new_with_label("Choose...")
    directory_row.pack_start(choose_button, False, False, 0)
    row(advanced_page, "Audio Folder", directory_row)
    notebook.append_page(advanced_page, Gtk.Label(label="Device & Advanced"))

    footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    open_button = Gtk.Button.new_with_label("Open Config Folder")
    version = _label(Gtk, f"Version: {package_version()}", secondary=True)
    status = _label(Gtk, "", secondary=True)
    save_button = Gtk.Button.new_with_label("Save")
    save_button.get_style_context().add_class("suggested-action")
    footer.pack_start(open_button, False, False, 0)
    footer.pack_start(version, False, False, 0)
    footer.pack_start(status, True, True, 0)
    footer.pack_start(save_button, False, False, 0)
    root.pack_end(footer, False, False, 0)

    def choose_directory(_button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Choose Audio Folder",
            parent=window,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            "Cancel",
            Gtk.ResponseType.CANCEL,
            "Choose",
            Gtk.ResponseType.OK,
        )
        dialog.set_current_folder(directory.get_text())
        if dialog.run() == Gtk.ResponseType.OK:
            directory.set_text(dialog.get_filename())
        dialog.destroy()

    def bounded_number(key: str, low: int, high: int, multiplier: int = 1) -> int:
        try:
            raw = float(fields[key].get_text())
        except ValueError:
            raw = 0
        if not math.isfinite(raw):
            return low
        return max(low, min(high, math.floor(raw * multiplier + 0.5)))

    def save(_button=None) -> None:
        try:
            for key in (
                "openai_api_key",
                "openai_base_url",
                "openai_model",
                "openai_prompt",
                "llm_base_url",
                "llm_api_key",
                "llm_model",
                "relay_url",
                "relay_sender_token",
                "relay_receiver_token",
            ):
                setattr(config, key, fields[key].get_text().strip())
            start, end = hotwords.get_buffer().get_bounds()
            text = hotwords.get_buffer().get_text(start, end, True)
            import re

            config.asr_hotwords = list(
                dict.fromkeys(
                    value.strip()
                    for value in re.split(r"[,\r\n]", text)
                    if value.strip()
                )
            )
            config.codex_bridge_port = bounded_number("codex_bridge_port", 1, 65535)
            config.codex_success_chime = codex_chime.get_active()
            config.relay_url = fields["relay_url"].get_text().strip()
            config.relay_sender_token = fields["relay_sender_token"].get_text().strip()
            config.relay_receiver_token = (
                fields["relay_receiver_token"].get_text().strip()
            )
            config.debug_audio_cache = debug_audio.get_active()
            config.debug_audio_dir = Path(directory.get_text()).expanduser()
            config.power_timers.display_dim_seconds = bounded_number(
                "display_dim_seconds", 5, 3600
            )
            config.power_timers.display_off_seconds = bounded_number(
                "display_off_seconds", 30, 86400, 60
            )
            config.power_timers.idle_deep_sleep_seconds = bounded_number(
                "idle_deep_sleep_seconds", 60, 86400, 60
            )
            config.power_timers.codex_deep_sleep_seconds = bounded_number(
                "codex_deep_sleep_seconds", 60, 86400, 60
            )
            on_save()
            status.set_text("Saved.")
            window.destroy()
        except Exception as error:
            dialog = Gtk.MessageDialog(
                transient_for=window,
                modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK,
                text="Could Not Save Settings",
            )
            dialog.format_secondary_text(str(error))
            dialog.run()
            dialog.destroy()

    choose_button.connect("clicked", choose_directory)
    open_button.connect("clicked", lambda _button: on_open_config())
    save_button.connect("clicked", save)
    window.show_all()
    present_from_tray(window, activation_time)
    fields["openai_api_key"].grab_focus()
    return window
