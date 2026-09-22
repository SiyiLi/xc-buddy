from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from .gtk_layout import ui_scale
from .gtk_onboarding import _gtk_module


THEME_BACKGROUND = {
    "white": "rgba(255,255,255,0.86)",
    "pink": "rgba(255,214,230,0.86)",
    "green": "rgba(214,242,214,0.86)",
    "yellow": "rgba(255,240,184,0.86)",
    "blue": "rgba(209,232,255,0.86)",
    "purple": "rgba(230,214,255,0.86)",
}

THEME_ACCENT = {
    "white": "#ffffff",
    "pink": "#ff6b9e",
    "green": "#4fd68c",
    "yellow": "#ffc742",
    "blue": "#61adff",
    "purple": "#b88cff",
}


def _modules():
    Gtk = _gtk_module()
    from gi.repository import Gdk, GLib, Pango

    return Gtk, Gdk, GLib, Pango


def _workarea(Gdk):
    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    return monitor.get_workarea()


class RecognitionIndicator:
    def __init__(self, scale: float = 1.0) -> None:
        Gtk, _Gdk, GLib, _Pango = _modules()
        self.scale = scale
        self.widget = Gtk.DrawingArea()
        size = round(34 * scale)
        self.widget.set_size_request(size, size)
        self.mode = "listening"
        self.started = time.monotonic()
        self.duration = 1.2
        self.widget.connect("draw", self._draw)
        self._timer = GLib.timeout_add(30, self._tick)

    def set_mode(self, mode: str, duration: float = 1.2) -> None:
        self.mode = mode
        self.started = time.monotonic()
        self.duration = duration
        self.widget.queue_draw()

    def _tick(self) -> bool:
        self.widget.queue_draw()
        return True

    def _draw(self, widget, context) -> bool:
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        center_x, center_y = width / 2, height / 2
        context.set_source_rgba(0, 0, 0, 0.34)
        context.set_line_width(3 * self.scale)
        context.set_line_cap(1)
        if self.mode == "listening":
            elapsed = time.monotonic() - self.started
            for index, base_height in enumerate((12, 20, 15)):
                phase = max(0.0, elapsed - index * 0.14)
                scale = 0.8 + 0.35 * math.sin(phase * math.pi / 0.52)
                bar_height = base_height * scale * self.scale
                x = center_x + (-10 + index * 8) * self.scale
                context.set_line_width(4 * self.scale)
                context.move_to(x, center_y - bar_height / 2)
                context.line_to(x, center_y + bar_height / 2)
                context.stroke()
            return False
        if self.mode == "countdown":
            elapsed = time.monotonic() - self.started
            fraction = max(0.0, 1.0 - elapsed / max(self.duration, 0.01))
            context.arc(
                center_x,
                center_y,
                12 * self.scale,
                -math.pi / 2,
                -math.pi / 2 + math.tau * fraction,
            )
        else:
            context.arc(center_x, center_y, 12 * self.scale, 0, math.tau)
        context.stroke()
        if self.mode == "error":
            arm = 5 * self.scale
            context.move_to(center_x - arm, center_y - arm)
            context.line_to(center_x + arm, center_y + arm)
            context.move_to(center_x + arm, center_y - arm)
            context.line_to(center_x - arm, center_y + arm)
            context.stroke()
        return False


class OverlayWindow:
    def __init__(self, loop: asyncio.AbstractEventLoop, on_hide: Callable[[], None]):
        Gtk, Gdk, _GLib, Pango = _modules()
        self.Gdk, self.Pango = Gdk, Pango
        self.loop, self.on_hide = loop, on_hide
        self.scale = ui_scale()
        self.window = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.window.set_decorated(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_accept_focus(False)
        self.window.set_app_paintable(True)
        self.window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        visual = self.window.get_screen().get_rgba_visual()
        if visual is not None:
            self.window.set_visual(visual)
        self.window.connect("draw", self._clear)

        self.container = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=round(16 * self.scale),
        )
        self.container.set_name(f"xc-overlay-{id(self)}")
        self.container.set_margin_start(round(32 * self.scale))
        self.container.set_margin_end(round(32 * self.scale))
        self.container.set_margin_top(round(24 * self.scale))
        self.container.set_margin_bottom(round(24 * self.scale))
        self.window.add(self.container)
        self.indicator = RecognitionIndicator(self.scale)
        self.container.pack_start(self.indicator.widget, False, False, 0)

        labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=round(8 * self.scale),
        )
        self.text = Gtk.Label(xalign=0.5)
        self.text.set_justify(Gtk.Justification.CENTER)
        self.text.set_line_wrap(True)
        self.text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.text.get_style_context().add_class("xc-overlay-text")
        self.hint = Gtk.Label(xalign=0.5)
        self.hint.get_style_context().add_class("xc-overlay-hint")
        labels.pack_start(self.text, True, True, 0)
        labels.pack_start(self.hint, False, False, 0)
        self.container.pack_start(labels, True, True, 0)
        self.hide_handle: asyncio.TimerHandle | None = None
        self.position = "center"
        self._apply_style("white")

    @staticmethod
    def _clear(_widget, context) -> bool:
        context.set_operator(0)
        context.paint()
        context.set_operator(2)
        return False

    def _apply_style(self, theme: str) -> None:
        Gtk, _Gdk, _GLib, _Pango = _modules()
        if previous := getattr(self, "_provider", None):
            for widget in (self.container, self.text, self.hint):
                widget.get_style_context().remove_provider(previous)
        provider = Gtk.CssProvider()
        name = self.container.get_name()
        provider.load_from_data(
            f"""
            #{name} {{
                background-color: {THEME_BACKGROUND.get(theme, THEME_BACKGROUND['white'])};
                border-radius: {round(24 * self.scale)}px;
            }}
            #{name} .xc-overlay-text {{
                color: rgba(0,0,0,0.68);
                font-size: 30pt;
                font-weight: 400;
            }}
            #{name} .xc-overlay-hint {{
                color: rgba(0,0,0,0.42);
                font-size: 13pt;
                font-weight: 500;
            }}
            """.encode()
        )
        for widget in (self.container, self.text, self.hint):
            widget.get_style_context().add_provider(
                provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self._provider = provider

    def set_theme(self, theme: str) -> None:
        self._apply_style(theme)

    def show(
        self,
        mode: str,
        text: str,
        hint: str,
        seconds: float,
        hidden: Callable | None,
    ) -> None:
        if self.hide_handle:
            self.hide_handle.cancel()
        self.text.set_text(text or ("No speech" if mode == "countdown" else " "))
        self.hint.set_text(hint)
        self.hint.set_visible(bool(hint))
        self.indicator.set_mode(mode, seconds or 1.2)

        layout = self.text.create_pango_layout(self.text.get_text())
        layout.set_font_description(self.Pango.FontDescription("Sans 30"))
        measured_width, _height = layout.get_pixel_size()
        workarea = _workarea(self.Gdk)
        max_width = min(round(645 * self.scale), workarea.width - 48)
        side_chrome = round((32 + 34 + 16 + 32) * self.scale)
        width = min(
            max(round(300 * self.scale), measured_width + side_chrome),
            max_width,
        )
        self.text.set_size_request(max(1, width - side_chrome), -1)
        self.window.set_size_request(width, round(112 * self.scale))
        self.window.show_all()
        if not hint:
            self.hint.hide()
        self.window.set_opacity(1)
        self._raise()
        if seconds:
            self.hide_handle = self.loop.call_later(seconds, lambda: self.hide(hidden))

    def move(self, position: str, index: int) -> None:
        workarea = _workarea(self.Gdk)
        width, height = self.window.get_size()
        margin, gap = 28, 14
        step = height + gap
        if position == "center":
            if index == 0:
                offset = 0
            else:
                magnitude = (index + 1) // 2
                offset = (-magnitude if index % 2 else magnitude) * step
            x = workarea.x + (workarea.width - width) // 2
            y = workarea.y + (workarea.height - height) // 2 + offset
            y = max(
                workarea.y + margin,
                min(y, workarea.y + workarea.height - margin - height),
            )
        elif position.startswith("top"):
            y = workarea.y + margin + index * step
            x = (
                workarea.x + margin
                if position.endswith("left")
                else workarea.x + workarea.width - margin - width
            )
        else:
            y = workarea.y + workarea.height - margin - height - index * step
            x = (
                workarea.x + margin
                if position.endswith("left")
                else workarea.x + workarea.width - margin - width
            )
        self.window.move(x, y)
        self._raise()

    def _raise(self) -> None:
        if surface := self.window.get_window():
            surface.raise_()

    def hide(self, hidden: Callable | None = None) -> None:
        if self.hide_handle:
            self.hide_handle.cancel()
            self.hide_handle = None
        self.window.hide()
        self.on_hide()
        if hidden:
            hidden()

    def destroy(self) -> None:
        if self.hide_handle:
            self.hide_handle.cancel()
        self.window.destroy()


@dataclass
class SubtitleLane:
    text: str
    color: str
    generation: int
    timer: asyncio.TimerHandle


class SubtitleWindow:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        Gtk, Gdk, _GLib, Pango = _modules()
        self.Gtk, self.Gdk, self.Pango = Gtk, Gdk, Pango
        self.loop = loop
        self.window = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.window.set_decorated(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_accept_focus(False)
        self.window.set_app_paintable(True)
        self.window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        visual = self.window.get_screen().get_rgba_visual()
        if visual is not None:
            self.window.set_visual(visual)
        self.window.connect("draw", OverlayWindow._clear)
        self.stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.viewport = Gtk.ScrolledWindow()
        self.viewport.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self.viewport.add(self.stack)
        self.window.add(self.viewport)
        self.lanes: dict[str, SubtitleLane] = {}
        self.generation = 0
        self._providers: list[Any] = []

    def show(self, device_id: str, text: str, color: str) -> None:
        text = text.strip()
        if not text:
            return
        self.generation += 1
        if old := self.lanes.pop(device_id, None):
            old.timer.cancel()
        generation = self.generation
        timer = self.loop.call_later(7, self._hide_lane, device_id, generation)
        self.lanes[device_id] = SubtitleLane(text, color, generation, timer)
        self.render()

    def _hide_lane(self, device_id: str, generation: int) -> None:
        lane = self.lanes.get(device_id)
        if lane is None or lane.generation != generation:
            return
        self.lanes.pop(device_id).timer.cancel()
        self.render()

    def set_colors(self, colors: dict[str, str]) -> None:
        for device_id, lane in self.lanes.items():
            lane.color = colors.get(device_id, "white")
        self.render()

    def render(self) -> None:
        for child in self.stack.get_children():
            self.stack.remove(child)
        self._providers.clear()
        if not self.lanes:
            self.window.hide()
            return
        workarea = _workarea(self.Gdk)
        window_width = 520
        for device_id in sorted(self.lanes):
            lane = self.lanes[device_id]
            layout = self.window.create_pango_layout(lane.text)
            layout.set_font_description(self.Pango.FontDescription("Sans Semi-Bold 46"))
            measured, _height = layout.get_pixel_size()
            lane_width = min(
                min(1400, int(workarea.width * 0.86)), max(520, measured + 148)
            )
            window_width = max(window_width, lane_width)
            box = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=12)
            name = f"xc-subtitle-{id(box)}"
            box.set_name(name)
            box.set_margin_top(0)
            box.set_margin_bottom(0)
            box.set_size_request(lane_width, 76)
            bar = self.Gtk.Box()
            bar.set_name(f"{name}-bar")
            bar.set_size_request(6, -1)
            bar.set_margin_start(18)
            bar.set_margin_top(14)
            bar.set_margin_bottom(14)
            device = self.Gtk.Label(label=device_id)
            device.set_size_request(42, -1)
            device.get_style_context().add_class("xc-subtitle-device")
            label = self.Gtk.Label(label=lane.text)
            label.set_justify(self.Gtk.Justification.CENTER)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(self.Pango.WrapMode.WORD_CHAR)
            label.get_style_context().add_class("xc-subtitle-text")
            label.set_margin_end(26)
            label.set_margin_top(12)
            label.set_margin_bottom(14)
            box.pack_start(bar, False, False, 0)
            box.pack_start(device, False, False, 0)
            box.pack_start(label, True, True, 0)
            provider = self.Gtk.CssProvider()
            accent = THEME_ACCENT.get(lane.color, THEME_ACCENT["white"])
            provider.load_from_data(
                f"""
                #{name} {{ background-color: rgba(0,0,0,0.62); border-radius: 14px; }}
                #{name}-bar {{ background-color: {accent}; border-radius: 3px; }}
                #{name} .xc-subtitle-device {{ color: {accent}; font-family: monospace; font-size: 14pt; font-weight: 600; }}
                #{name} .xc-subtitle-text {{ color: white; font-size: 46px; font-weight: 600; text-shadow: 0 1px 3px rgba(0,0,0,0.8); }}
                """.encode()
            )
            for widget in (box, bar, device, label):
                widget.get_style_context().add_provider(
                    provider, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            self._providers.append(provider)
            self.stack.pack_start(box, False, False, 0)
        self.window.set_size_request(window_width, -1)
        self.window.show_all()
        self.window.resize(window_width, 1)
        while self.Gtk.events_pending():
            self.Gtk.main_iteration_do(False)
        _minimum_height, natural_height = self.stack.get_preferred_height()
        height = min(natural_height, int(workarea.height * 0.36))
        self.window.resize(window_width, height)
        while self.Gtk.events_pending():
            self.Gtk.main_iteration_do(False)
        width, height = self.window.get_size()
        x = workarea.x + (workarea.width - width) // 2
        y = (
            workarea.y
            + workarea.height
            - height
            - max(18, int(workarea.height * 0.035))
        )
        self.window.move(x, y)

    def hide_all(self) -> None:
        for lane in self.lanes.values():
            lane.timer.cancel()
        self.lanes.clear()
        self.window.hide()

    def destroy(self) -> None:
        self.hide_all()
        self.window.destroy()


class RuntimePresenter:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.overlays: dict[str, OverlayWindow] = {}
        self.visible: set[str] = set()
        self.colors: dict[str, str] = {}
        self.positions: dict[str, str] = {}
        self.subtitles = SubtitleWindow(loop)

    def show_overlay(
        self,
        device_id: str,
        mode: str,
        text: str,
        hint: str = "",
        seconds: float = 0,
        hidden: Callable | None = None,
    ) -> None:
        key = device_id or "__default__"
        overlay = self.overlays.get(key)
        if overlay is None:
            overlay = self.overlays[key] = OverlayWindow(
                self.loop, partial(self._mark_hidden, key)
            )
        overlay.set_theme(self.colors.get(device_id, "white"))
        overlay.position = self.positions.get(device_id, "center")
        self.visible.add(key)
        overlay.show(mode, text, hint, seconds, hidden)
        self.reposition()

    def _mark_hidden(self, key: str) -> None:
        self.visible.discard(key)
        self.reposition()

    def hide_overlay(self, device_id: str | None = None) -> None:
        keys = (
            list(self.overlays) if device_id is None else [device_id or "__default__"]
        )
        for key in keys:
            if overlay := self.overlays.get(key):
                overlay.hide()

    def reposition(self) -> None:
        grouped: dict[str, list[str]] = {}
        for key in self.visible:
            device_id = "" if key == "__default__" else key
            grouped.setdefault(self.positions.get(device_id, "center"), []).append(key)
        for position, keys in grouped.items():
            for index, key in enumerate(sorted(keys)):
                self.overlays[key].move(position, index)

    def preferences(self, colors: dict[str, str], positions: dict[str, str]) -> None:
        self.colors = dict(colors)
        self.positions = dict(positions)
        for key, overlay in self.overlays.items():
            device_id = "" if key == "__default__" else key
            overlay.set_theme(self.colors.get(device_id, "white"))
            overlay.position = self.positions.get(device_id, "center")
        self.subtitles.set_colors(colors)
        self.reposition()

    def destroy(self) -> None:
        for overlay in self.overlays.values():
            overlay.destroy()
        self.overlays.clear()
        self.subtitles.destroy()
