from __future__ import annotations

import json
import struct
from dataclasses import dataclass

SERVICE_UUID = "8f2f0b84-6e6f-4b23-88f7-3a3ceafc5100"
AUDIO_UUID = "8f2f0b84-6e6f-4b23-88f7-3a3ceafc5101"
STATE_UUID = "8f2f0b84-6e6f-4b23-88f7-3a3ceafc5102"
CONTROL_UUID = "8f2f0b84-6e6f-4b23-88f7-3a3ceafc5103"
OTA_RX_UUID = "8f2f0b84-6e6f-4b23-88f7-3a3ceafc5104"
OTA_STATE_UUID = "8f2f0b84-6e6f-4b23-88f7-3a3ceafc5105"


@dataclass(frozen=True)
class AudioFrame:
    session_id: int
    sequence: int
    flags: int
    payload: bytes

    @property
    def is_start(self) -> bool:
        return bool(self.flags & 1)

    @property
    def is_end(self) -> bool:
        return bool(self.flags & 2)


def parse_audio_frame(data: bytes) -> AudioFrame | None:
    if len(data) < 16:
        return None
    version, kind, header_len, session_id, sequence, flags, _reserved, payload_len = (
        struct.unpack_from("<BBHIIBBH", data)
    )
    if (
        version != 1
        or kind != 1
        or header_len != 16
        or len(data) < header_len + payload_len
    ):
        return None
    return AudioFrame(
        session_id, sequence, flags, data[header_len : header_len + payload_len]
    )


def parse_event(data: bytes, expected_type: int = 0x10) -> dict | None:
    if len(data) < 4:
        return None
    version, kind, payload_len = struct.unpack_from("<BBH", data)
    if version != 1 or kind != expected_type or len(data) < 4 + payload_len:
        return None
    try:
        event = json.loads(data[4 : 4 + payload_len].decode("utf-8"))
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            return None
        string_fields = (
            ("button", "hardware", "firmware_version")
            if expected_type == 0x10
            else ("code",)
        )
        uint_fields = (
            ("session_id", "duration_ms")
            if expected_type == 0x10
            else ("transfer_id", "written", "size")
        )
        int_fields = () if expected_type == 0x10 else ("esp_err", "reboot_ms")
        list_fields = ("buttons", "ui_states") if expected_type == 0x10 else ()
        for key in string_fields:
            if (
                key in event
                and event[key] is not None
                and not isinstance(event[key], str)
            ):
                return None
        for key in uint_fields:
            value = event.get(key)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 0xFFFFFFFF
            ):
                return None
        for key in int_fields:
            value = event.get(key)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                return None
        for key in list_fields:
            value = event.get(key)
            if value is not None and (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            ):
                return None
        return event
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _json_payload(event: str, **values: object) -> bytes:
    return json.dumps(
        {"event": event, **values}, separators=(",", ":"), ensure_ascii=False
    ).encode()


def ui_state(state: str, text: str = "", background_state: str | None = None) -> bytes:
    values: dict[str, object] = {"state": state, "text": text}
    if background_state is not None:
        values["background_state"] = background_state
    return _json_payload("ui_state", **values)


def interaction_mode(mode: str) -> bytes:
    return _json_payload("interaction_mode", mode=mode)


def codex_success_chime(enabled: bool) -> bytes:
    return _json_payload("codex_success_chime", enabled=enabled)


def codex_notification_chime() -> bytes:
    return _json_payload("codex_notification_chime")


def heartbeat() -> bytes:
    return _json_payload("heartbeat")


def disconnect() -> bytes:
    return _json_payload("disconnect")


def power_timers(dim: int, off: int, idle: int, codex: int) -> bytes:
    return _json_payload(
        "power_timers",
        dim_seconds=dim,
        screen_off_seconds=off,
        idle_sleep_seconds=idle,
        codex_sleep_seconds=codex,
    )


def ota_begin(image_size: int, transfer_id: int) -> bytes:
    return struct.pack("<BBHII", 1, 0x20, 12, image_size, transfer_id)


def ota_data(transfer_id: int, offset: int, chunk: bytes) -> bytes:
    return struct.pack("<BBHII", 1, 0x21, 12, transfer_id, offset) + chunk


def ota_end(transfer_id: int, image_size: int) -> bytes:
    return struct.pack("<BBHII", 1, 0x22, 12, transfer_id, image_size)


def ota_abort(transfer_id: int) -> bytes:
    return struct.pack("<BBHI", 1, 0x23, 8, transfer_id)
