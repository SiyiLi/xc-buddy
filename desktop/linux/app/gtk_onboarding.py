from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from .gtk_layout import apply_typography, scale_window


def _gtk_module():
    try:
        import gi
    except ImportError:
        system_packages = Path("/usr/lib/python3/dist-packages")
        if system_packages.is_dir() and str(system_packages) not in sys.path:
            sys.path.append(str(system_packages))
        import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    icon = Path(__file__).with_name("icons") / "AppIcon.png"
    if icon.is_file():
        Gtk.Window.set_default_icon_from_file(str(icon))
    return Gtk


def _label(Gtk, text: str = "", *, secondary: bool = False):
    label = Gtk.Label(label=text, xalign=0)
    if secondary:
        label.get_style_context().add_class("xc-secondary")
    return label


async def onboard(
    config: Any,
    discover: Callable[[], Awaitable[list[tuple[str, str, int]]]],
    device_id_for_name: Callable[[str], str],
    integration_missing: Callable[[], list[str]],
    save_config: Callable[[], None],
) -> bool:
    """Render the macOS onboarding layout with native GTK widgets."""
    Gtk = _gtk_module()
    window = Gtk.Window(title="Set Up XC Buddy")
    window.set_default_size(680, 470)
    window.set_size_request(680, 470)
    scale_window(window, 680, 470)
    apply_typography(Gtk, window)
    window.set_resizable(False)
    window.set_position(Gtk.WindowPosition.CENTER)

    css = Gtk.CssProvider()
    css.load_from_data(
        b"""
        .xc-window { background: #f4f4f5; }
        .xc-sidebar { background: #e7e7e9; }
        .xc-title { color: #1d1d1f; font-size: 22pt; font-weight: 600; }
        .xc-secondary { color: #6e6e73; font-size: 13pt; }
        .xc-step { color: #86868b; font-size: 13pt; }
        .xc-step-reached { color: #1d1d1f; }
        .xc-step-current { color: #1d1d1f; font-weight: 600; }
        """
    )
    Gtk.StyleContext.add_provider_for_screen(
        window.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    window.get_style_context().add_class("xc-window")

    result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    scan_task: asyncio.Task | None = None
    step = 0
    devices: list[tuple[str, str, int]] = []
    selected_name = ""
    rebuilding_devices = False
    step_names = ("Pair Device", "Transcription", "Accessibility", "Ready")

    root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    window.add(root)

    sidebar = Gtk.EventBox()
    sidebar.set_size_request(170, -1)
    sidebar.get_style_context().add_class("xc-sidebar")
    root.pack_start(sidebar, False, False, 0)

    step_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    step_list.set_margin_top(24)
    step_list.set_margin_bottom(24)
    step_list.set_margin_start(20)
    step_list.set_margin_end(20)
    sidebar.add(step_list)

    step_labels = []
    for index, name in enumerate(step_names):
        label = _label(Gtk, f"{index + 1}. {name}")
        label.get_style_context().add_class("xc-step")
        step_list.pack_start(label, False, False, 0)
        step_labels.append(label)

    main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    main.set_margin_top(26)
    main.set_margin_bottom(24)
    main.set_margin_end(28)
    root.pack_start(main, True, True, 0)

    title = _label(Gtk)
    title.get_style_context().add_class("xc-title")
    main.pack_start(title, False, False, 0)

    detail = _label(Gtk, secondary=True)
    detail.set_line_wrap(True)
    detail.set_lines(2)
    detail.set_max_width_chars(64)
    main.pack_start(detail, False, False, 0)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    main.pack_start(content, True, True, 0)

    footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    status = _label(Gtk, secondary=True)
    status.set_ellipsize(3)
    footer.pack_start(status, True, True, 0)
    back_button = Gtk.Button.new_with_label("Back")
    next_button = Gtk.Button.new_with_label("Continue")
    next_button.get_style_context().add_class("suggested-action")
    footer.pack_start(back_button, False, False, 0)
    footer.pack_start(next_button, False, False, 0)
    main.pack_end(footer, False, False, 0)

    model = Gtk.ListStore(str, str, int)
    table = Gtk.TreeView(model=model)
    table.set_headers_visible(True)
    table.set_activate_on_single_click(False)
    for heading, model_index, width in (
        ("Device", 0, 210),
        ("ID", 1, 90),
        ("RSSI", 2, 70),
    ):
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn(heading, renderer, text=model_index)
        column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        column.set_fixed_width(width)
        table.append_column(column)
    selection = table.get_selection()
    selection.set_mode(Gtk.SelectionMode.SINGLE)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_shadow_type(Gtk.ShadowType.IN)
    scroll.set_size_request(440, 220)
    scroll.add(table)
    scan_status = _label(Gtk, "Scanning", secondary=True)

    api_key = Gtk.Entry()
    api_key.set_text(config.openai_api_key)
    api_key.set_width_chars(36)
    api_key.set_visibility(True)
    api_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    key_label = _label(Gtk, "API Key", secondary=True)
    key_label.set_xalign(1)
    key_label.set_size_request(100, -1)
    api_key.set_size_request(300, -1)
    api_row.pack_start(key_label, False, False, 0)
    api_row.pack_start(api_key, False, False, 0)

    access_status = _label(Gtk)
    access_status.set_line_wrap(True)
    access_button = Gtk.Button.new_with_label("Check Desktop Access")

    def set_status(text: str) -> None:
        status.set_text(text)

    def complete(value: bool) -> None:
        nonlocal scan_task
        if scan_task is not None:
            scan_task.cancel()
            scan_task = None
        if not result.done():
            result.set_result(value)
        window.destroy()

    def select_current_device() -> bool:
        nonlocal selected_name
        if rebuilding_devices:
            return bool(selected_name)
        tree_model, tree_iter = selection.get_selected()
        if tree_iter is None:
            scan_status.set_text("Select a device")
            next_button.set_sensitive(False)
            return False
        selected_name = tree_model[tree_iter][0]
        device_id = device_id_for_name(selected_name)
        if not device_id:
            return False
        config.paired_device_ids = [device_id]
        scan_status.set_text(f"Selected XC-{device_id}")
        next_button.set_sensitive(True)
        return True

    async def scan() -> None:
        nonlocal devices, scan_task, rebuilding_devices
        scan_status.set_text("Scanning")
        try:
            known: dict[str, tuple[str, str, int]] = {}
            while not result.done():
                try:
                    discovered = await discover()
                except asyncio.CancelledError:
                    return
                except Exception:
                    if not devices:
                        scan_status.set_text("Bluetooth unavailable")
                    await asyncio.sleep(2)
                    continue
                for address, name, rssi in discovered:
                    known[address] = (address, name, rssi)
                if known:
                    devices = list(known.values())
                    rebuilding_devices = True
                    try:
                        model.clear()
                        selected_path = None
                        for row, (_address, name, rssi) in enumerate(devices):
                            device_id = device_id_for_name(name)
                            model.append((name, device_id, rssi))
                            if name == selected_name:
                                selected_path = row
                        if selected_path is not None:
                            selection.select_path(selected_path)
                    finally:
                        rebuilding_devices = False
                    if selected_path is not None:
                        select_current_device()
                    else:
                        scan_status.set_text(f"{len(devices)} found")
                else:
                    scan_status.set_text("Scanning")
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return
        finally:
            scan_task = None

    def update_accessibility() -> bool:
        missing = integration_missing()
        ready = not missing
        access_status.set_text(
            "Desktop access is allowed."
            if ready
            else "Desktop access is not allowed yet.\n\nMissing: " + ", ".join(missing)
        )
        access_button.set_visible(not ready)
        next_button.set_sensitive(ready)
        if ready:
            set_status("")
        return ready

    def clear_content() -> None:
        for child in content.get_children():
            content.remove(child)

    def add_summary_line(label: str, value: str) -> None:
        content.pack_start(_label(Gtk, f"{label}: {value}"), False, False, 0)

    def render() -> None:
        nonlocal scan_task
        clear_content()
        set_status("")
        for index, label in enumerate(step_labels):
            context = label.get_style_context()
            context.remove_class("xc-step-reached")
            context.remove_class("xc-step-current")
            if index <= step:
                context.add_class("xc-step-reached")
            if index == step:
                context.add_class("xc-step-current")
        back_button.set_sensitive(step > 0)
        next_button.set_label("Finish" if step == 3 else "Continue")

        if step == 0:
            title.set_text("Pair XC")
            detail.set_text(
                "Choose a nearby XC-XXXX device. XC Buddy needs a paired device before the app can listen."
            )
            content.pack_start(scroll, False, False, 0)
            content.pack_start(scan_status, False, False, 0)
            next_button.set_sensitive(bool(config.paired_device_ids))
            if scan_task is None and not devices:
                scan_task = asyncio.create_task(scan())
        elif step == 1:
            title.set_text("Configure transcription")
            detail.set_text("Enter the API key used to transcribe audio.")
            content.pack_start(api_row, False, False, 0)
            next_button.set_sensitive(True)
            api_key.grab_focus()
        elif step == 2:
            title.set_text("Allow text insertion")
            detail.set_text(
                "XC Buddy pastes recognized text at your cursor, so Linux desktop access is required."
            )
            content.pack_start(access_status, False, False, 0)
            content.pack_start(access_button, False, False, 0)
            update_accessibility()
        else:
            title.set_text("XC Buddy is ready")
            detail.set_text(
                "The device and ASR settings are configured. Finish setup to start scanning and connecting."
            )
            device = (
                f"XC-{config.paired_device_ids[0]}"
                if config.paired_device_ids
                else "Not paired"
            )
            add_summary_line("Device", device)
            add_summary_line("Transcription", "Configured")
            add_summary_line(
                "Desktop Access",
                "Allowed" if not integration_missing() else "Not allowed yet",
            )
            next_button.set_sensitive(True)
        window.show_all()
        if step == 2:
            access_button.set_visible(bool(integration_missing()))

    def go_back(_widget=None) -> None:
        nonlocal step
        if step <= 0:
            return
        if step == 1:
            config.openai_api_key = api_key.get_text().strip()
        step -= 1
        render()

    def go_next(_widget=None) -> None:
        nonlocal step
        set_status("")
        if step == 1:
            config.openai_api_key = api_key.get_text().strip()
        if step == 0 and not select_current_device():
            set_status("Select an XC device first.")
            return
        if step == 1 and not config.openai_api_key:
            set_status("Enter the API key used to transcribe audio.")
            return
        if step == 2 and not update_accessibility():
            set_status("Allow desktop access before continuing.")
            return
        if step == 3:
            try:
                save_config()
            except OSError as error:
                set_status(f"Save failed: {error}")
                return
            complete(True)
            return
        step += 1
        render()

    def close_window(*_args) -> bool:
        complete(False)
        return True

    selection.connect("changed", lambda _selection: select_current_device())
    table.connect("row-activated", lambda *_args: go_next())
    back_button.connect("clicked", go_back)
    next_button.connect("clicked", go_next)
    access_button.connect("clicked", lambda _button: update_accessibility())
    api_key.connect("activate", go_next)
    window.connect("delete-event", close_window)

    render()
    window.present()
    while not result.done():
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        await asyncio.sleep(0.02)
    return result.result()
