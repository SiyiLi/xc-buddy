from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .gtk_onboarding import _gtk_module, _label
from .gtk_layout import apply_typography, present_from_tray, scale_window


async def choose_device(
    discover: Callable[[], Awaitable[list[tuple[str, str, int]]]],
    device_id_for_name: Callable[[str], str],
    existing_device_ids: list[str],
    activation_time: int = 0,
    window_created: Callable[[object], None] | None = None,
) -> str | None:
    """Port of PairDeviceWindowController.swift using native GTK widgets."""
    Gtk = _gtk_module()
    window = Gtk.Window(title="Pair XC")
    window.set_default_size(420, 280)
    window.set_size_request(420, 280)
    scale_window(window, 420, 280)
    apply_typography(Gtk, window)
    window.set_resizable(False)
    window.set_position(Gtk.WindowPosition.CENTER)
    if window_created is not None:
        window_created(window)

    result: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    devices: list[tuple[str, str, int]] = []
    scan_task: asyncio.Task | None = None

    stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    stack.set_margin_top(16)
    stack.set_margin_bottom(16)
    stack.set_margin_start(16)
    stack.set_margin_end(16)
    window.add(stack)

    model = Gtk.ListStore(str, str, int, str)
    table = Gtk.TreeView(model=model)
    for heading, model_index, width in (
        ("Device", 0, 170),
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
    scroll.set_size_request(-1, 200)
    scroll.add(table)
    stack.pack_start(scroll, True, True, 0)

    footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    status = _label(Gtk, "Scanning", secondary=True)
    pair_button = Gtk.Button.new_with_label("Pair")
    pair_button.get_style_context().add_class("suggested-action")
    cancel_button = Gtk.Button.new_with_label("Cancel")
    footer.pack_start(status, True, True, 0)
    footer.pack_start(pair_button, False, False, 0)
    footer.pack_start(cancel_button, False, False, 0)
    stack.pack_end(footer, False, False, 0)

    def finish(value: str | None) -> None:
        nonlocal scan_task
        if scan_task is not None:
            scan_task.cancel()
            scan_task = None
        if not result.done():
            result.set_result(value)
        window.destroy()

    def pair(_widget=None) -> None:
        tree_model, tree_iter = selection.get_selected()
        if tree_iter is None:
            status.set_text("Select a device")
            return
        finish(tree_model[tree_iter][3])

    async def scan() -> None:
        nonlocal devices
        try:
            while not result.done():
                previous_name = ""
                tree_model, tree_iter = selection.get_selected()
                if tree_iter is not None:
                    previous_name = tree_model[tree_iter][3]
                devices = await discover()
                model.clear()
                selected_path = None
                for row, (_address, name, rssi) in enumerate(devices):
                    device_id = device_id_for_name(name)
                    display_name = (
                        f"{name} (paired)" if device_id in existing_device_ids else name
                    )
                    model.append((display_name, device_id, rssi, name))
                    if name == previous_name:
                        selected_path = row
                if selected_path is not None:
                    selection.select_path(selected_path)
                status.set_text("Scanning" if not devices else f"{len(devices)} found")
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return
        except Exception:
            status.set_text("Bluetooth unavailable")

    def close_window(*_args) -> bool:
        finish(None)
        return True

    pair_button.connect("clicked", pair)
    cancel_button.connect("clicked", lambda _button: finish(None))
    table.connect("row-activated", lambda *_args: pair())
    window.connect("delete-event", close_window)
    window.show_all()
    present_from_tray(window, activation_time)
    scan_task = asyncio.create_task(scan())

    while not result.done():
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        await asyncio.sleep(0.02)
    return result.result()
