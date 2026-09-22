from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
from collections.abc import Callable
from typing import Any

from . import protocol
from .config import normalize_device_id


LOG = logging.getLogger(__name__)
SLEEP_MATCH_RULE = (
    "type='signal',sender='org.freedesktop.login1',"
    "interface='org.freedesktop.login1.Manager',member='PrepareForSleep',"
    "path='/org/freedesktop/login1'"
)


@dataclasses.dataclass(frozen=True)
class ConnectedXCDevice:
    name: str
    device_id: str


class BluetoothManager:
    def __init__(
        self,
        paired_ids: list[str],
        on_audio: Callable,
        on_event: Callable,
        on_connections: Callable,
    ) -> None:
        self.paired_ids = set(paired_ids)
        self.on_audio, self.on_event, self.on_connections = (
            on_audio,
            on_event,
            on_connections,
        )
        self.clients: dict[str, Any] = {}
        self.devices: dict[str, str] = {}
        self.device_names: dict[str, str] = {}
        self._control_state = protocol.ui_state("ready")
        self.control_states: dict[str, bytes] = {}
        self.task: asyncio.Task | None = None
        self._running = False
        self._sleeping = False
        self._sleep_bus: Any = None
        self._sleep_message_handler = self._handle_sleep_message
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_tasks: set[asyncio.Task] = set()
        self.ota_waiters: dict[
            str, tuple[asyncio.Future, int, Callable[[int, int, bool], None]]
        ] = {}

    @staticmethod
    def device_id(name: str | None) -> str:
        advertised_name = (name or "").strip().upper()
        if not advertised_name.startswith(("XC-", "VS-")):
            return ""
        return normalize_device_id(advertised_name)

    @staticmethod
    async def discover(timeout: float = 8) -> list[tuple[str, str, int]]:
        from bleak import BleakScanner

        found = await BleakScanner.discover(
            timeout=timeout, return_adv=True, service_uuids=[protocol.SERVICE_UUID]
        )
        result = []
        for address, (device, adv) in found.items():
            name = adv.local_name or device.name or ""
            if BluetoothManager.device_id(name):
                result.append((address, name, adv.rssi))
        return sorted(result, key=lambda item: item[2], reverse=True)

    async def start(self) -> None:
        self._running = True
        await self._subscribe_to_sleep()
        self._ensure_scan_loop()

    async def stop(self) -> None:
        self._running = False
        for task in tuple(self._lifecycle_tasks):
            task.cancel()
        if self._lifecycle_tasks:
            await asyncio.gather(*self._lifecycle_tasks, return_exceptions=True)
        await self._cancel_scan_loop()
        await self.send(protocol.disconnect())
        await self._disconnect_clients()
        self._unsubscribe_from_sleep()
        self._sleeping = False

    def _ensure_scan_loop(self) -> None:
        if self._running and not self._sleeping and self.task is None:
            self.task = asyncio.create_task(self._scan_loop())

    async def _cancel_scan_loop(self) -> None:
        task, self.task = self.task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _disconnect_clients(self) -> None:
        for client in list(self.clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self.clients.clear()
        self.devices.clear()
        self.device_names.clear()
        self.on_connections({})

    @property
    def connected_devices(self) -> dict[str, ConnectedXCDevice]:
        return {
            address: ConnectedXCDevice(
                self.device_names.get(address, f"XC-{device_id}"), device_id
            )
            for address, device_id in self.devices.items()
        }

    async def _subscribe_to_sleep(self) -> None:
        if self._sleep_bus is not None:
            return
        bus = None
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast.constants import BusType, MessageType
            from dbus_fast.message import Message

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            reply = await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[SLEEP_MATCH_RULE],
                )
            )
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError(reply.error_name or "D-Bus AddMatch failed")
            bus.add_message_handler(self._sleep_message_handler)
            self._sleep_bus = bus
        except Exception:
            LOG.debug("Unable to monitor system sleep state", exc_info=True)
            if bus is not None:
                bus.disconnect()

    def _unsubscribe_from_sleep(self) -> None:
        if self._sleep_bus is not None:
            self._sleep_bus.remove_message_handler(self._sleep_message_handler)
            self._sleep_bus.disconnect()
        self._sleep_bus = None

    def _handle_sleep_message(self, message: Any) -> None:
        from dbus_fast.constants import MessageType

        if (
            message.message_type == MessageType.SIGNAL
            and message.path == "/org/freedesktop/login1"
            and message.interface == "org.freedesktop.login1.Manager"
            and message.member == "PrepareForSleep"
            and message.body
        ):
            self._prepare_for_sleep(bool(message.body[0]))

    def _prepare_for_sleep(self, sleeping: bool) -> None:
        if not self._running:
            return
        task = asyncio.create_task(self._handle_sleep(bool(sleeping)))
        self._lifecycle_tasks.add(task)
        task.add_done_callback(self._lifecycle_tasks.discard)

    async def _handle_sleep(self, sleeping: bool) -> None:
        async with self._lifecycle_lock:
            if sleeping == self._sleeping or not self._running:
                return
            self._sleeping = sleeping
            if sleeping:
                await self._cancel_scan_loop()
                await self.send(protocol.ui_state("ready"), remember=True)
                await self._disconnect_clients()
            else:
                self._ensure_scan_loop()

    async def _scan_loop(self) -> None:
        from bleak import BleakClient, BleakScanner

        while True:
            try:
                if self.clients:
                    await asyncio.sleep(2)
                    continue
                found = await BleakScanner.discover(
                    timeout=5, return_adv=True, service_uuids=[protocol.SERVICE_UUID]
                )
                for address, (device, adv) in found.items():
                    if self.clients:
                        break
                    advertised_name = adv.local_name or device.name or ""
                    device_id = self.device_id(advertised_name)
                    if (
                        not device_id
                        or device_id not in self.paired_ids
                        or address in self.clients
                    ):
                        continue
                    try:
                        await self._connect(
                            BleakClient,
                            device,
                            address,
                            device_id,
                            advertised_name,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOG.debug(
                            "BLE connection setup failed for XC-%s",
                            device_id,
                            exc_info=True,
                        )
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.debug("BLE scan failed", exc_info=True)
                await asyncio.sleep(3)

    async def _connect(
        self,
        client_type,
        device,
        address: str,
        device_id: str,
        advertised_name: str,
    ) -> None:
        if self.clients:
            return
        client = client_type(
            device,
            disconnected_callback=lambda _client, a=address: self._disconnected(a),
        )
        try:
            await client.connect()
            self.clients[address] = client
            self.devices[address] = device_id
            self.device_names[address] = advertised_name or f"XC-{device_id}"
            await client.start_notify(
                protocol.AUDIO_UUID,
                lambda _characteristic, data, a=address: self._audio(a, bytes(data)),
            )
            await client.start_notify(
                protocol.STATE_UUID,
                lambda _characteristic, data, a=address: self._state(a, bytes(data)),
            )
            await client.start_notify(
                protocol.OTA_STATE_UUID,
                lambda _characteristic, data, a=address: self._ota(a, bytes(data)),
            )
            await self._initialize(client, address)
        except BaseException:
            self.clients.pop(address, None)
            self.devices.pop(address, None)
            self.device_names.pop(address, None)
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        self.on_connections(self.connected_devices)

    def _disconnected(self, address: str) -> None:
        waiter = self.ota_waiters.pop(address, None)
        if waiter and not waiter[0].done():
            waiter[0].set_exception(RuntimeError("No XC device is connected."))
        self.clients.pop(address, None)
        self.devices.pop(address, None)
        self.device_names.pop(address, None)
        self.on_connections(self.connected_devices)

    def _audio(self, address: str, data: bytes) -> None:
        if frame := protocol.parse_audio_frame(data):
            self.on_audio(address, self.devices.get(address, ""), frame)

    def _state(self, address: str, data: bytes) -> None:
        if event := protocol.parse_event(data):
            self.on_event(address, self.devices.get(address, ""), event)

    def _ota(self, address: str, data: bytes) -> None:
        event = protocol.parse_event(data, 0x30)
        if not event:
            return
        waiter = self.ota_waiters.get(address)
        if waiter and int(event.get("transfer_id") or waiter[1]) == waiter[1]:
            future, _transfer_id, progress = waiter
            if event["event"] == "progress":
                progress(
                    int(event.get("written") or 0),
                    int(event.get("size") or 1),
                    True,
                )
            elif event["event"] == "done" and not future.done():
                future.set_result(None)
            elif event["event"] == "error" and not future.done():
                future.set_exception(
                    RuntimeError(f"Device rejected OTA: {event.get('code', 'unknown')}")
                )
        self.on_event(address, self.devices.get(address, ""), event)

    @property
    def control_state(self) -> bytes:
        return self._control_state

    @control_state.setter
    def control_state(self, data: bytes) -> None:
        self._control_state = data
        for address in set(self.control_states) | set(self.clients):
            self.control_states[address] = data

    async def _initialize(self, client, address: str) -> None:
        await client.write_gatt_char(
            protocol.CONTROL_UUID,
            self.control_states.get(address, self.control_state),
            response=False,
        )

    async def send(
        self, data: bytes, address: str | None = None, remember: bool = False
    ) -> None:
        if remember:
            if address is None:
                self.control_state = data
            else:
                self.control_states[address] = data
        for key, client in list(self.clients.items()):
            if address is None or address == key:
                try:
                    await client.write_gatt_char(
                        protocol.CONTROL_UUID, data, response=False
                    )
                except Exception:
                    pass

    async def update_firmware(
        self,
        device_id: str,
        image: bytes,
        progress: Callable[[int, int, bool], None],
    ) -> None:
        if len(image) > 3 * 1024 * 1024:
            raise ValueError("Firmware image is larger than the OTA partition.")
        pair = next(
            (
                (a, c)
                for a, c in self.clients.items()
                if self.devices.get(a) == device_id
            ),
            None,
        )
        if not pair:
            raise RuntimeError("No XC device is connected.")
        address, client = pair
        transfer_id = random.randint(1, 0xFFFFFFFF)
        done = asyncio.get_running_loop().create_future()
        self.ota_waiters[address] = (done, transfer_id, progress)
        try:
            progress(0, len(image), True)
            await client.write_gatt_char(
                protocol.OTA_RX_UUID,
                protocol.ota_begin(len(image), transfer_id),
                response=True,
            )
            mtu = getattr(client, "mtu_size", 247)
            chunk_size = max(20, min(mtu - 15, 244))
            last_progress = 0
            for offset in range(0, len(image), chunk_size):
                chunk = image[offset : offset + chunk_size]
                await client.write_gatt_char(
                    protocol.OTA_RX_UUID,
                    protocol.ota_data(transfer_id, offset, chunk),
                    response=False,
                )
                written = offset + len(chunk)
                if written - last_progress >= 65536 or written == len(image):
                    last_progress = written
                    progress(written, len(image), False)
            await client.write_gatt_char(
                protocol.OTA_RX_UUID,
                protocol.ota_end(transfer_id, len(image)),
                response=True,
            )
            await asyncio.wait_for(done, timeout=45)
            progress(len(image), len(image), True)
        except BaseException as error:
            try:
                await client.write_gatt_char(
                    protocol.OTA_RX_UUID,
                    protocol.ota_abort(transfer_id),
                    response=False,
                )
            except Exception:
                pass
            if isinstance(error, (asyncio.CancelledError, RuntimeError)):
                raise
            raise RuntimeError(f"BLE write failed: {error}") from error
        finally:
            self.ota_waiters.pop(address, None)
