from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime as dt
import logging
import time

from . import protocol
from .bluetooth import BluetoothManager, ConnectedXCDevice
from .bridge import CodexBridge, RelayClient
from .config import AppConfig
from .input import InputInjector
from .ogg import OggOpusMuxer
from .services import TranscriptionClient, TranslationClient
from .services import (
    FirmwareRelease,
    download_firmware,
    latest_firmware,
    version_is_older,
)

LOG = logging.getLogger(__name__)

MINIMUM_RECORDING_DURATION = 0.5
AUDIO_END_TIMEOUT = 1.0
APPROVAL_TIMEOUT = 60.0
FIRMWARE_RELEASE_CACHE_DURATION = 60 * 60
HEARTBEAT_INTERVAL = 30.0


@dataclasses.dataclass
class Cycle:
    address: str
    device_id: str
    session_id: int
    started: float = dataclasses.field(default_factory=time.monotonic)
    debug_started: dt.datetime = dataclasses.field(default_factory=dt.datetime.now)
    muxer: OggOpusMuxer = dataclasses.field(default_factory=OggOpusMuxer)
    audio: bytearray = dataclasses.field(default_factory=bytearray)
    frames: int = 0
    finalizing: bool = False
    waiting_for_end: bool = False
    eos: bool = False
    final_task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        self.audio += self.muxer.reset()


class Coordinator:
    """Platform-neutral state machine mirroring the macOS coordinator."""

    def __init__(self, config: AppConfig, ui) -> None:
        self.config, self.ui = copy.deepcopy(config), ui
        self.ble = BluetoothManager(
            self.config.paired_device_ids,
            self.on_audio,
            self.on_event,
            self.on_connections,
        )
        self.bridge = CodexBridge(
            self.config.codex_bridge_port,
            self.config.codex_bridge_token,
            self.on_codex_event,
            ui.codex_status,
        )
        self.relay = RelayClient(self.on_codex_event, ui.relay_status)
        self.injector = InputInjector()
        self.main_cycle: Cycle | None = None
        self.subtitle_cycles: dict[tuple[str, int], Cycle] = {}
        self.active_subtitles: dict[str, int] = {}
        self.pending_text: str | None = None
        self.pending_paused = False
        self.last_text: str | None = None
        self.last_address: str | None = None
        self.last_device_id: str = ""
        self.codex_state = "idle"
        self.applied_relay_mode = "disabled"
        self._bridge_settings = (
            self.config.codex_bridge_port,
            self.config.codex_bridge_token,
        )
        self._relay_settings = (
            self.config.relay_mode,
            self.config.relay_url,
            self.config.relay_sender_token,
            self.config.relay_receiver_token,
        )
        self.approval_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.firmware_task: asyncio.Task | None = None
        self.device_firmware: dict[str, dict] = {}
        self.cached_release: FirmwareRelease | None = None
        self.active_firmware_task: asyncio.Task | None = None
        self.running = False

    async def start(self) -> None:
        self.running = True
        self.ui.recoverable_input(bool(self.last_text))
        self.ui.status("Pair XC" if not self.config.paired_device_ids else "Ready")
        await self.apply_relay_mode()
        self.heartbeat_task = asyncio.create_task(self._heartbeat())
        self.firmware_task = asyncio.create_task(self._firmware_checks())

    async def stop(self) -> None:
        self.running = False
        background = [
            task
            for task in (self.heartbeat_task, self.approval_task, self.firmware_task)
            if task
        ]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        recognition = [
            cycle.final_task
            for cycle in ([self.main_cycle] if self.main_cycle else [])
            + list(self.subtitle_cycles.values())
            if cycle.final_task
        ]
        for task in recognition:
            task.cancel()
        if recognition:
            await asyncio.gather(*recognition, return_exceptions=True)
        self.main_cycle = None
        self.subtitle_cycles.clear()
        self.active_subtitles.clear()
        self.pending_text = None
        self.pending_paused = False
        update_task = self.active_firmware_task
        if update_task:
            update_task.cancel()
            await asyncio.gather(update_task, return_exceptions=True)
        await self.bridge.stop()
        await self.relay.stop()
        await self.ble.stop()
        self.heartbeat_task = None
        self.approval_task = None
        self.firmware_task = None
        self.active_firmware_task = None
        self.ui.hide_overlay()
        self.ui.hide_subtitles()
        self.ui.connections({})

    async def update_config(self, config: AppConfig) -> None:
        """Apply settings with the same cancellation and subsystem boundaries as macOS."""
        was_recognizing = await self._cancel_recognition_for_config_change()
        updated = copy.deepcopy(config)
        bridge_settings = (updated.codex_bridge_port, updated.codex_bridge_token)
        relay_settings = (
            updated.relay_mode,
            updated.relay_url,
            updated.relay_sender_token,
            updated.relay_receiver_token,
        )
        bridge_changed = bridge_settings != self._bridge_settings
        relay_changed = relay_settings != self._relay_settings
        paired_changed = set(updated.paired_device_ids) != self.ble.paired_ids
        if paired_changed:
            paired = set(updated.paired_device_ids)
            self.device_firmware = {
                device_id: info
                for device_id, info in self.device_firmware.items()
                if device_id in paired
            }
            if self.active_firmware_task:
                self.active_firmware_task.cancel()
                await asyncio.gather(self.active_firmware_task, return_exceptions=True)
            await self.ble.stop()
            self.ble = BluetoothManager(
                updated.paired_device_ids,
                self.on_audio,
                self.on_event,
                self.on_connections,
            )
        self.config = updated
        if bridge_changed:
            await self.bridge.stop()
            self.bridge = CodexBridge(
                updated.codex_bridge_port,
                updated.codex_bridge_token,
                self.on_codex_event,
                self.ui.codex_status,
            )
            self._bridge_settings = bridge_settings
        if relay_changed and self.running:
            await self.apply_relay_mode()
        else:
            if paired_changed and self.running and updated.relay_mode != "sender":
                await self.ble.start()
            if bridge_changed and self.running and updated.relay_mode != "receiver":
                await self._start_bridge()
        if updated.relay_mode != "sender":
            for address in list(self.ble.clients):
                await self._initialize_device(address)
        if paired_changed:
            self._publish_firmware_availability()
        if was_recognizing:
            self.ui.status("Ready")

    def update_device_theme_colors(self, colors: dict[str, str]) -> None:
        self.config.device_theme_colors = dict(colors)

    async def _cancel_recognition_for_config_change(self) -> bool:
        cycles = ([self.main_cycle] if self.main_cycle else []) + list(
            self.subtitle_cycles.values()
        )
        if not cycles and self.pending_text is None:
            return False
        tasks = [cycle.final_task for cycle in cycles if cycle.final_task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        addresses = {cycle.address for cycle in cycles}
        self.main_cycle = None
        self.subtitle_cycles.clear()
        self.active_subtitles.clear()
        self.pending_text = None
        self.pending_paused = False
        self.ui.hide_overlay()
        self.ui.hide_subtitles()
        if self.applied_relay_mode != "sender":
            for address in addresses:
                await self._send_ready(address)
        return True

    async def update_firmware(self, device_id: str) -> None:
        if self.active_firmware_task:
            return
        self.active_firmware_task = asyncio.current_task()
        self.ui.firmware_started(f"XC-{device_id}")
        try:
            release = self.cached_release
            if release is None:
                release = await asyncio.to_thread(latest_firmware)
            info = self.device_firmware.get(device_id, {})
            if info.get("hardware") != "stick_s3":
                raise RuntimeError("The connected firmware does not expose BLE OTA.")
            current = info.get("firmware_version", "")
            if not version_is_older(current, release.version):
                raise RuntimeError(
                    f"firmware {current or 'unknown'} is already current"
                )
            image = await asyncio.to_thread(download_firmware, release)
            await self.ble.update_firmware(
                device_id,
                image,
                lambda written, total, confirmed: self.ui.firmware_progress(
                    device_id, written, total, confirmed
                ),
            )
            self.ui.firmware_succeeded()
        except asyncio.CancelledError:
            self.ui.firmware_failed("Firmware update cancelled.")
        except Exception as error:
            self.ui.firmware_failed(str(error))
        finally:
            self.active_firmware_task = None
            self.ui.firmware_finished()

    def cancel_firmware_update(self) -> None:
        if self.active_firmware_task:
            self.active_firmware_task.cancel()

    async def apply_relay_mode(self) -> None:
        previous_mode = self.applied_relay_mode
        self.applied_relay_mode = self.config.relay_mode
        self._relay_settings = (
            self.config.relay_mode,
            self.config.relay_url,
            self.config.relay_sender_token,
            self.config.relay_receiver_token,
        )
        await self.bridge.stop()
        await self.relay.stop()
        if self.config.relay_mode == "sender":
            if self.approval_task:
                self.approval_task.cancel()
                self.approval_task = None
            await self.ble.stop()
            await self._start_bridge()
            if previous_mode == "receiver":
                self.codex_state = "idle"
            self.relay.retain_sender_state(self.codex_state)
            await self.relay.start(
                "sender", self.config.relay_url, self.config.relay_sender_token
            )
        elif self.config.relay_mode == "receiver":
            await self.ble.start()
            await self.relay.start(
                "receiver", self.config.relay_url, self.config.relay_receiver_token
            )
            self.on_codex_event("idle", "")
        else:
            await self.ble.start()
            await self._start_bridge()
            self.ui.relay_status("Disabled")
            if previous_mode == "receiver":
                self.codex_state = "idle"
            self.on_codex_event(self.codex_state, "")

    async def _start_bridge(self) -> None:
        try:
            await self.bridge.start()
        except OSError as error:
            self.ui.codex_status(f"Unavailable: {error}")
            self.ui.status(f"Codex bridge unavailable: {error}")

    def on_connections(self, devices: dict[str, ConnectedXCDevice]) -> None:
        if not self.running:
            self.subtitle_cycles.clear()
            self.active_subtitles.clear()
            self.main_cycle = None
            self.pending_text = None
            self.pending_paused = False
            return
        connected = set(devices)
        for cycle in list(self.subtitle_cycles.values()):
            if cycle.address not in connected:
                self._cancel_cycle(cycle, restore_activity=True)
        if self.main_cycle and self.main_cycle.address not in connected:
            cycle = self.main_cycle
            if cycle.waiting_for_end and cycle.frames:
                if cycle.final_task:
                    cycle.final_task.cancel()
                cycle.final_task = asyncio.create_task(self._transcribe(cycle))
            else:
                self.ui.hide_subtitles()
                self._cancel_cycle(cycle, restore_activity=True)
        self.ui.connections(devices)
        self.ui.status(
            "Ready" if devices or self.config.paired_device_ids else "Pair XC"
        )
        for address in devices:
            asyncio.create_task(self._initialize_device(address))

    async def _initialize_device(self, address: str) -> None:
        await self.ble.send(
            protocol.interaction_mode(self.config.interaction_mode), address
        )
        await self.ble.send(
            protocol.codex_success_chime(self.config.codex_success_chime), address
        )
        timers = self.config.power_timers
        await self.ble.send(
            protocol.power_timers(
                timers.display_dim_seconds,
                timers.display_off_seconds,
                timers.idle_deep_sleep_seconds,
                timers.codex_deep_sleep_seconds,
            ),
            address,
        )

    def on_event(self, address: str, device_id: str, event: dict) -> None:
        kind, button = event.get("event"), event.get("button")
        if kind == "device_info":
            self.device_firmware[device_id] = event
            self.ui.device_info(device_id, event)
            self._publish_firmware_availability()
        elif kind == "button_down" and button == "primary":
            self._primary_down(address, device_id, int(event.get("session_id") or 0))
        elif kind == "button_up":
            if button == "primary":
                self._primary_up(address)
            elif button == "secondary":
                self._secondary(address)
        elif kind == "button_click":
            if button == "primary":
                self._primary_click(
                    address, device_id, int(event.get("session_id") or 0)
                )
            elif button == "secondary":
                self._secondary(address)

    def _primary_click(self, address: str, device_id: str, session_id: int) -> None:
        if self._pending_primary(address):
            return
        if self.config.interaction_mode != "click_to_talk":
            asyncio.create_task(self._ready(address))
            return
        active = self._active_for(address)
        if active:
            if active.finalizing:
                asyncio.create_task(
                    self.ble.send(protocol.ui_state("thinking"), address, True)
                )
                return
            self._primary_up(address)
        else:
            self._primary_down(address, device_id, session_id)

    def _primary_down(self, address: str, device_id: str, session_id: int) -> None:
        if self._pending_primary(address):
            return
        if not session_id:
            asyncio.create_task(self._ready(address))
            return
        if self.config.output.target == "subtitle":
            previous = self.active_subtitles.get(address)
            if previous == session_id or (address, session_id) in self.subtitle_cycles:
                return
            if previous:
                self.active_subtitles.pop(address, None)
            cycle = Cycle(address, device_id, session_id)
            self.subtitle_cycles[(address, session_id)] = cycle
            self.active_subtitles[address] = session_id
        else:
            if self.main_cycle:
                if self.main_cycle.address == address:
                    if self.main_cycle.finalizing:
                        asyncio.create_task(
                            self.ble.send(protocol.ui_state("thinking"), address, True)
                        )
                else:
                    asyncio.create_task(self._send_ready(address))
                return
            cycle = self.main_cycle = Cycle(address, device_id, session_id)
        self.ui.listening(device_id)
        asyncio.create_task(
            self.ble.send(protocol.ui_state("recording"), address, True)
        )

    def _primary_up(self, address: str) -> None:
        cycle = self._active_for(address)
        if not cycle:
            return
        if time.monotonic() - cycle.started < MINIMUM_RECORDING_DURATION:
            self._cancel_cycle(cycle)
            return
        if (
            self.config.output.target == "subtitle"
            and not cycle.frames
            and self.subtitle_cycles.get((address, cycle.session_id)) is cycle
        ):
            cycle.finalizing = True
            cycle.final_task = asyncio.create_task(
                self._error(cycle, "No audio frames from device")
            )
            return
        cycle.finalizing = True
        cycle.waiting_for_end = True
        # Match macOS: retain the listening overlay until the final audio
        # frame arrives, then show the transcription stage at ASR start.
        self.ui.status("Processing")
        asyncio.create_task(self.ble.send(protocol.ui_state("thinking"), address, True))
        if not cycle.final_task:
            cycle.final_task = asyncio.create_task(self._finish_after_grace(cycle))
        if (
            self.active_subtitles.get(address) == cycle.session_id
            and self.config.interaction_mode == "hold_to_talk"
        ):
            self.active_subtitles.pop(address, None)
            asyncio.create_task(self._send_ready(address))

    def on_audio(
        self, address: str, device_id: str, frame: protocol.AudioFrame
    ) -> None:
        cycle = self.subtitle_cycles.get((address, frame.session_id))
        if (
            not cycle
            and self.main_cycle
            and self.main_cycle.address == address
            and self.main_cycle.session_id == frame.session_id
        ):
            cycle = self.main_cycle
        if not cycle:
            return
        if cycle.eos:
            return
        if frame.payload:
            cycle.frames += 1
            cycle.audio += cycle.muxer.packet(frame.payload, frame.is_end)
            cycle.eos = frame.is_end
        if frame.is_end:
            cycle.finalizing = True
            cycle.waiting_for_end = False
            if cycle.final_task:
                cycle.final_task.cancel()
            cycle.final_task = asyncio.create_task(self._transcribe(cycle))

    async def _finish_after_grace(self, cycle: Cycle) -> None:
        await asyncio.sleep(AUDIO_END_TIMEOUT)
        cycle.waiting_for_end = False
        await self._transcribe(cycle)

    async def _transcribe(self, cycle: Cycle) -> None:
        if not self._cycle_exists(cycle):
            return
        cycle.waiting_for_end = False
        if time.monotonic() - cycle.started < MINIMUM_RECORDING_DURATION:
            self._cancel_cycle(cycle)
            return
        if not cycle.frames:
            await self._error(cycle, "No audio frames from device")
            return
        if not cycle.eos:
            cycle.audio += cycle.muxer.finish()
            cycle.eos = True
        self.ui.processing(cycle.device_id, "Transcribing...")
        self._save_debug(cycle)
        try:
            text = await asyncio.to_thread(
                TranscriptionClient(self.config).transcribe, bytes(cycle.audio)
            )
        except Exception as error:
            await self._error(cycle, str(error))
            return
        profile = self.config.profile_for(cycle.device_id)
        if profile.transform == "translate" and text:
            try:
                self.ui.processing(cycle.device_id, "Translating...")
                text = await asyncio.to_thread(
                    TranslationClient(self.config).translate,
                    text,
                    profile.translation_target,
                )
            except Exception as error:
                if profile.target == "subtitle":
                    self.ui.error(cycle.device_id, str(error))
                    await self._finish_subtitle_cycle(cycle, "")
                else:
                    await self._error(cycle, str(error))
                return
        if profile.target == "subtitle":
            await self._finish_subtitle_cycle(cycle, text)
        else:
            self.pending_text = self.last_text = text
            self.last_address = cycle.address
            self.last_device_id = cycle.device_id
            self.ui.recoverable_input(True)
            self.pending_paused = False
            self._remove_cycle(cycle, keep_main=True)
            self.ui.final(
                cycle.device_id,
                text,
                lambda: asyncio.create_task(self.commit_pending()),
            )
            await self.ble.send(
                protocol.ui_state("pending_confirmation", text), cycle.address, True
            )

    async def _finish_subtitle_cycle(self, cycle: Cycle, text: str) -> None:
        is_active = self.active_subtitles.get(cycle.address) == cycle.session_id
        has_active = cycle.address in self.active_subtitles
        if text:
            self.ui.subtitle(
                cycle.device_id,
                text,
                self.config.device_theme_colors.get(cycle.device_id, "white"),
            )
        if is_active or not has_active:
            self.ui.hide_overlay(cycle.device_id)
        self._remove_cycle(cycle)
        if cycle.address not in self.active_subtitles:
            await self._ready(cycle.address)

    async def commit_pending(self) -> None:
        text = self.pending_text
        if text is None:
            return
        self.pending_text = None
        self.pending_paused = False
        self.main_cycle = None
        try:
            submitted_to_codex = self.config.auto_enter and await asyncio.to_thread(
                self.injector.frontmost_application_is_codex
            )
            await asyncio.to_thread(self.injector.paste, text, self.config.auto_enter)
            self.on_codex_event("working" if submitted_to_codex else "idle", "")
        except Exception as error:
            self.ui.error(
                self.last_device_id,
                str(error),
                lambda: asyncio.create_task(self._restore_codex(self.last_address))
                if self.last_address
                else None,
            )
            await self.ble.send(
                protocol.ui_state("error", str(error)), self.last_address
            )

    def _pending_primary(self, address: str) -> bool:
        if self.pending_text is None:
            return False
        if address != self.last_address:
            return True
        if not self.pending_paused:
            self.pending_paused = True
            self.ui.paused(self.last_device_id, self.pending_text)
        else:
            self.ui.hide_overlay(self.last_device_id)
            asyncio.create_task(self.commit_pending())
        return True

    def _secondary(self, address: str) -> None:
        if session := self.active_subtitles.get(address):
            self._cancel_subtitle(address, session)
            return
        address_cycles = [
            cycle for cycle in self.subtitle_cycles.values() if cycle.address == address
        ]
        if address_cycles:
            for cycle in address_cycles:
                self._cancel_cycle(cycle)
            return
        if (
            self.main_cycle
            and self.main_cycle.address == address
            and self.pending_text is None
        ):
            self._cancel_cycle(self.main_cycle)
            return
        if self.pending_text is not None and address == self.last_address:
            self.pending_text = None
            self.pending_paused = False
            self.main_cycle = None
            self.ui.hide_overlay(self.last_device_id)
            asyncio.create_task(self._restore_codex(address))
            return
        if self.last_text and not self.main_cycle:
            self.pending_text = self.last_text
            self.pending_paused = True
            self.last_address = address
            self.last_device_id = getattr(self.ble, "devices", {}).get(
                address, self.last_device_id
            )
            self.ui.paused(self.last_device_id, self.last_text)
            asyncio.create_task(
                self.ble.send(
                    protocol.ui_state("pending_confirmation", self.last_text),
                    address,
                    True,
                )
            )
            return
        asyncio.create_task(self._restore_codex(address))

    def restore_last(self) -> bool:
        if not self.last_text or self.pending_text is not None or self.main_cycle:
            return False
        self.pending_text = self.last_text
        self.pending_paused = True
        self.ui.paused(self.last_device_id, self.last_text)
        if self.last_address:
            asyncio.create_task(
                self.ble.send(
                    protocol.ui_state("pending_confirmation", self.last_text),
                    self.last_address,
                    True,
                )
            )
        return True

    def on_codex_event(self, event: str, message: str = "") -> None:
        if self.config.relay_mode == "sender":
            self.relay.publish(event)
            self.codex_state = (
                "idle"
                if event in ("done", "error", "idle")
                else ("working" if event == "tool_call_started" else event)
            )
            if event == "error":
                self.ui.codex_status("Error")
                self.ui.status("Codex error")
                return
            status = {
                "idle": "Idle",
                "working": "Working",
                "approval_needed": "Approval needed",
            }[self.codex_state]
            self.ui.codex_status(status)
            self.ui.status(
                {
                    "idle": "Ready",
                    "working": "Codex working",
                    "approval_needed": "Approval needed",
                }[self.codex_state]
            )
            return
        if self.approval_task:
            self.approval_task.cancel()
            self.approval_task = None
        if event in ("working", "tool_call_started"):
            if event == "tool_call_started" and self.codex_state == "working":
                return
            self.codex_state = "working"
            self.ui.status("Codex working")
            self.ui.codex_status("Working")
            data = protocol.ui_state("codex_working", "Codex is working")
        elif event == "approval_needed":
            self.codex_state = "approval_needed"
            self.ui.status("Approval needed")
            self.ui.codex_status("Approval needed")
            data = protocol.ui_state("approval_needed", "Approval needed")
            self.approval_task = asyncio.create_task(self._approval_chime())
        elif event == "done":
            self.codex_state = "idle"
            self.ui.status("Ready")
            self.ui.codex_status("Idle")
            data = protocol.ui_state("codex_done", "Turn complete")
            self.ble.control_state = protocol.ui_state("ready")
        elif event == "error":
            self.codex_state = "idle"
            self.ui.status("Codex error")
            self.ui.codex_status("Error")
            data = protocol.ui_state("error", message or "Codex error")
            self.ble.control_state = protocol.ui_state("ready")
        else:
            self.codex_state = "idle"
            self.ui.status("Ready")
            self.ui.codex_status("Idle")
            data = protocol.ui_state("ready", "", "ready")
        asyncio.create_task(
            self.ble.send(data, remember=event not in ("done", "error"))
        )

    async def _approval_chime(self) -> None:
        await asyncio.sleep(APPROVAL_TIMEOUT)
        if self.codex_state == "approval_needed" and self.config.codex_success_chime:
            await self.ble.send(protocol.codex_notification_chime())

    async def _restore_codex(self, address: str) -> None:
        if self.config.relay_mode == "sender":
            return
        self.ui.status(
            {
                "idle": "Ready",
                "working": "Codex working",
                "approval_needed": "Approval needed",
            }[self.codex_state]
        )
        states = {
            "idle": protocol.ui_state("ready", "", "ready"),
            "working": protocol.ui_state("codex_working", "Codex is working"),
            "approval_needed": protocol.ui_state("approval_needed", "Approval needed"),
        }
        await self.ble.send(states[self.codex_state], address, True)

    async def _send_ready(self, address: str) -> None:
        background = {
            "idle": "ready",
            "working": "codex_working",
            "approval_needed": "approval_needed",
        }[self.codex_state]
        await self.ble.send(protocol.ui_state("ready", "", background), address, True)

    async def _ready(self, address: str) -> None:
        await self._send_ready(address)
        self.ui.status("Ready")

    async def _error(self, cycle: Cycle, message: str) -> None:
        is_subtitle = (
            self.subtitle_cycles.get((cycle.address, cycle.session_id)) is cycle
        )
        self._remove_cycle(cycle)
        if is_subtitle:
            if cycle.address in self.active_subtitles:
                return
            self.ui.error(
                cycle.device_id,
                message,
                lambda: asyncio.create_task(self._send_ready(cycle.address)),
            )
            return
        self.ui.error(
            cycle.device_id,
            message,
            lambda: asyncio.create_task(self._ready(cycle.address)),
        )
        await self.ble.send(
            protocol.ui_state("error", message), cycle.address, remember=True
        )

    def _active_for(self, address: str) -> Cycle | None:
        session = self.active_subtitles.get(address)
        return (
            self.subtitle_cycles.get((address, session))
            if session
            else self.main_cycle
            if self.main_cycle and self.main_cycle.address == address
            else None
        )

    def _cycle_exists(self, cycle: Cycle) -> bool:
        return (
            cycle is self.main_cycle
            or self.subtitle_cycles.get((cycle.address, cycle.session_id)) is cycle
        )

    def _remove_cycle(self, cycle: Cycle, keep_main: bool = False) -> None:
        if cycle is self.main_cycle and not keep_main:
            self.main_cycle = None
        self.subtitle_cycles.pop((cycle.address, cycle.session_id), None)
        if self.active_subtitles.get(cycle.address) == cycle.session_id:
            self.active_subtitles.pop(cycle.address, None)

    def _cancel_cycle(self, cycle: Cycle, restore_activity: bool = False) -> None:
        was_main = cycle is self.main_cycle
        if cycle.final_task and cycle.final_task is not asyncio.current_task():
            cycle.final_task.cancel()
        self._remove_cycle(cycle)
        if was_main:
            self.pending_text = None
            self.pending_paused = False
        self.ui.hide_overlay(None if was_main else cycle.device_id)
        asyncio.create_task(
            self._restore_codex(cycle.address)
            if restore_activity
            else self._ready(cycle.address)
        )

    def _cancel_subtitle(self, address: str, session: int) -> None:
        if cycle := self.subtitle_cycles.get((address, session)):
            self._cancel_cycle(cycle)

    def _save_debug(self, cycle: Cycle) -> None:
        if not self.config.debug_audio_cache:
            return
        try:
            self.config.debug_audio_dir.mkdir(parents=True, exist_ok=True)
            stamp = cycle.debug_started.strftime("%Y%m%d-%H%M%S")
            device = f"XC-{cycle.device_id}" if cycle.device_id else "unknown-device"
            target = (
                self.config.debug_audio_dir
                / f"{stamp}-{device}-session-{cycle.session_id}.ogg"
            )
            temporary = target.with_suffix(".ogg.tmp")
            temporary.write_bytes(cycle.audio)
            temporary.replace(target)
        except OSError:
            # Debug capture is best-effort. A bad cache path must never break
            # transcription or leave a device stuck in its recording state.
            LOG.warning("failed to save debug audio", exc_info=True)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await self.ble.send(protocol.heartbeat())

    async def _firmware_checks(self) -> None:
        while True:
            self.ui.firmware_checking(True)
            try:
                self.cached_release = await asyncio.to_thread(latest_firmware)
                self._publish_firmware_availability()
            except Exception:
                LOG.debug("firmware release check failed", exc_info=True)
            finally:
                self.ui.firmware_checking(False)
            await asyncio.sleep(FIRMWARE_RELEASE_CACHE_DURATION)

    def _publish_firmware_availability(self) -> None:
        if not self.cached_release:
            return
        availability = {
            device_id: info.get("hardware") == "stick_s3"
            and version_is_older(
                info.get("firmware_version", ""), self.cached_release.version
            )
            for device_id, info in self.device_firmware.items()
        }
        self.ui.firmware_release(self.cached_release.version, availability)
