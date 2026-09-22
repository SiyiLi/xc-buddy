from __future__ import annotations

import os


MINIMUM_FONT_PT = 11
BODY_FONT_PT = 13


def ui_scale() -> float:
    try:
        scale = float(os.environ.get("XC_BUDDY_UI_SCALE", "1"))
    except ValueError:
        return 1.0
    return max(1.0, min(scale, 1.25))


def apply_typography(Gtk, window) -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(
        f"""
        .xc-app {{ font-size: {BODY_FONT_PT}pt; }}
        .xc-app notebook tab label,
        .xc-app .xc-caption {{ font-size: {MINIMUM_FONT_PT}pt; }}
        .xc-app .heading {{ font-size: {BODY_FONT_PT}pt; font-weight: 600; }}
        """.encode()
    )
    Gtk.StyleContext.add_provider_for_screen(
        window.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    window.get_style_context().add_class("xc-app")


def scale_window(window, width: int, height: int) -> None:
    """Apply the small physical-DPI correction selected before GTK starts."""
    scale = ui_scale()
    if scale <= 1:
        return
    scaled_width = round(width * scale)
    scaled_height = round(height * scale)
    window.set_default_size(scaled_width, scaled_height)
    window.set_size_request(scaled_width, scaled_height)


def _x11_server_time(window) -> int:
    """Get an X11 timestamp when a tray host omitted the triggering event."""
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkX11", "3.0")
        from gi.repository import Gdk, GdkX11

        surface = window.get_window()
        if surface is None:
            return 0
        surface.set_events(surface.get_events() | Gdk.EventMask.PROPERTY_CHANGE_MASK)
        return int(GdkX11.x11_get_server_time(surface))
    except (AttributeError, ImportError, TypeError, ValueError):
        return 0


def present_from_tray(window, activation_time: int = 0) -> None:
    """Present a window from a direct tray action without permanent stacking."""
    timestamp = activation_time or _x11_server_time(window)
    if timestamp:
        window.present_with_time(timestamp)
    else:
        window.present()
