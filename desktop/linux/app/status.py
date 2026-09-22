from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrayPresentation:
    state: str
    icon_name: str
    accessibility_description: str
    visible_title: str


def tray_presentation(text: str, has_connected_devices: bool) -> TrayPresentation:
    """Return the same status-bar presentation selected by the macOS app."""
    normalized = text.lower()
    if "pair" in normalized:
        state = "pairing"
    elif "listen" in normalized:
        state = "listening"
    elif "error" in normalized or "failed" in normalized:
        state = "error"
    elif any(word in normalized for word in ("process", "final", "transcrib")):
        state = "processing"
    elif any(
        word in normalized
        for word in ("ready", "connect", "scan", "match", "pause", "no speech")
    ):
        state = "ready"
    else:
        state = "processing"

    if state == "pairing":
        return TrayPresentation(state, "pair", "Pair XC", "Pair")
    if state == "listening":
        return TrayPresentation(state, "listening", "Listening", "")
    if state == "error":
        return TrayPresentation(state, "error", "Error", "Error")
    if state == "processing":
        return TrayPresentation(state, "processing", "Processing", "Processing")
    icon_name = "ready" if has_connected_devices else "pair"
    return TrayPresentation(state, icon_name, "Ready", "")
