# ruff: noqa: F722, F821
# mypy: disable-error-code="name-defined,valid-type"
"""Native StatusNotifierItem and DBusMenu integration for Linux desktops.

dbus-fast uses quoted D-Bus signatures as runtime annotations; they are not
Python forward references and are therefore intentionally exempt from static
annotation-name checks.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType, PropertyAccess, RequestNameReply
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property, dbus_signal


logger = logging.getLogger(__name__)

WATCHER_BUS_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_OBJECT_PATH = "/StatusNotifierWatcher"
DBUS_BUS_NAME = "org.freedesktop.DBus"
DBUS_OBJECT_PATH = "/org/freedesktop/DBus"
SNI_OBJECT_PATH = "/StatusNotifierItem"
MENU_OBJECT_PATH = "/MenuBar"


class MenuItem:
    def __init__(
        self,
        text: str | Callable[[Any], str],
        action: Any,
        *,
        checked: bool | Callable[[Any], bool] = False,
        radio: bool = False,
        enabled: bool | Callable[[Any], bool] = True,
    ) -> None:
        self.text = text
        self.action = action
        self.checked = checked
        self.radio = radio
        self.enabled = enabled


class Menu:
    SEPARATOR = object()

    def __init__(self, *items: MenuItem | object) -> None:
        self.items = items


def _value(value: Any, item: MenuItem) -> Any:
    return value(item) if callable(value) else value


def icon_path(icon_name: str) -> str:
    return str(Path(__file__).with_name("icons") / f"xc-buddy-{icon_name}-symbolic.svg")


def theme_icon_name(icon_name: str) -> str:
    return f"xc-buddy-{icon_name}-symbolic"


@dataclass(frozen=True)
class _MenuNode:
    item: MenuItem | None
    properties: dict[str, Variant]
    children: tuple[int, ...]
    action: Callable | None = None


class _DBusMenu(ServiceInterface):
    """Export one-level choice menus for the Shell-owned StatusNotifier popup."""

    def __init__(self, activate: Callable[[_MenuNode, int], None]) -> None:
        super().__init__("com.canonical.dbusmenu")
        self._activate = activate
        self._nodes: dict[int, _MenuNode] = {0: _MenuNode(None, {}, ())}
        self._node_ids: dict[str, int] = {}
        self._next_node_id = 1
        self._revision = 1

    def _node_id(self, key: str) -> int:
        node_id = self._node_ids.get(key)
        if node_id is None:
            node_id = self._next_node_id
            self._node_ids[key] = node_id
            self._next_node_id += 1
        return node_id

    def set_menu(self, menu: Menu) -> None:
        previous_nodes = self._nodes
        nodes: dict[int, _MenuNode] = {}
        root_children = self._menu_nodes(menu, "root", 0, nodes)
        nodes[0] = _MenuNode(None, {}, root_children)
        updated: list[tuple[int, dict[str, Variant]]] = []
        removed: list[tuple[int, list[str]]] = []
        for node_id, node in nodes.items():
            previous = previous_nodes.get(node_id)
            if previous is None:
                continue
            changed_properties = {
                name: value
                for name, value in node.properties.items()
                if previous.properties.get(name) != value
            }
            if changed_properties:
                updated.append((node_id, changed_properties))
            removed_properties = [
                name for name in previous.properties if name not in node.properties
            ]
            if removed_properties:
                removed.append((node_id, removed_properties))
        self._nodes = nodes
        self._revision = 1 if self._revision == 2**32 - 1 else self._revision + 1
        if updated or removed:
            self.ItemsPropertiesUpdated(updated, removed)
        self.LayoutUpdated(self._revision, 0)

    def _menu_nodes(
        self,
        menu: Menu,
        parent_key: str,
        depth: int,
        nodes: dict[int, _MenuNode],
    ) -> tuple[int, ...]:
        children: list[int] = []
        labels: dict[tuple[str, str], int] = {}
        for position, item in enumerate(menu.items):
            if item is Menu.SEPARATOR:
                node_id = self._node_id(f"{parent_key}/separator:{position}")
                nodes[node_id] = _MenuNode(
                    None,
                    {
                        "type": Variant("s", "separator"),
                        "visible": Variant("b", True),
                    },
                    (),
                )
                children.append(node_id)
                continue
            if not isinstance(item, MenuItem):
                continue

            label = str(_value(item.text, item))
            kind = (
                "submenu"
                if isinstance(item.action, Menu)
                else "action"
                if callable(item.action)
                else "state"
            )
            if kind == "state":
                key = f"{parent_key}/state:{position}"
            else:
                label_key = (kind, label)
                occurrence = labels.get(label_key, 0)
                labels[label_key] = occurrence + 1
                key = f"{parent_key}/{kind}:{label}:{occurrence}"
            node_id = self._node_id(key)
            properties = {
                "label": Variant("s", label),
                "enabled": Variant("b", bool(_value(item.enabled, item))),
                "visible": Variant("b", True),
            }
            child_nodes: tuple[int, ...] = ()
            if isinstance(item.action, Menu):
                if depth >= 1:
                    raise ValueError(
                        "StatusNotifier menus support at most one submenu level"
                    )
                properties["children-display"] = Variant("s", "submenu")
                child_nodes = self._menu_nodes(item.action, key, depth + 1, nodes)
            if item.checked is not False or item.radio:
                properties["toggle-type"] = Variant(
                    "s", "radio" if item.radio else "checkmark"
                )
                properties["toggle-state"] = Variant(
                    "i", int(bool(_value(item.checked, item)))
                )
            nodes[node_id] = _MenuNode(
                item,
                properties,
                child_nodes,
                item.action if callable(item.action) else None,
            )
            children.append(node_id)
        return tuple(children)

    def _node(self, node_id: int) -> _MenuNode:
        try:
            return self._nodes[node_id]
        except KeyError as error:
            raise DBusError(
                "com.canonical.dbusmenu.Error.UnknownId",
                f"Unknown menu item {node_id}",
            ) from error

    @staticmethod
    def _properties(node: _MenuNode, names: list[str]) -> dict[str, Variant]:
        if not names:
            return dict(node.properties)
        return {
            name: node.properties[name] for name in names if name in node.properties
        }

    def _layout(
        self, node_id: int, recursion_depth: int, property_names: list[str]
    ) -> tuple[int, dict[str, Variant], list[Variant]]:
        node = self._node(node_id)
        children: list[Variant] = []
        if recursion_depth != 0:
            child_depth = (
                recursion_depth if recursion_depth < 0 else recursion_depth - 1
            )
            children = [
                Variant("(ia{sv}av)", self._layout(child, child_depth, property_names))
                for child in node.children
            ]
        return node_id, self._properties(node, property_names), children

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 4

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @dbus_method()
    def GetLayout(
        self, parent_id: "i", recursion_depth: "i", property_names: "as"
    ) -> "u(ia{sv}av)":
        return self._revision, self._layout(parent_id, recursion_depth, property_names)

    @dbus_method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        return [
            (node_id, self._properties(self._node(node_id), property_names))
            for node_id in ids
            if node_id in self._nodes
        ]

    @dbus_method()
    def GetProperty(self, node_id: "i", name: "s") -> "v":
        node = self._node(node_id)
        try:
            return node.properties[name]
        except KeyError as error:
            raise DBusError(
                "com.canonical.dbusmenu.Error.UnknownProperty",
                f"Menu item {node_id} has no {name!r} property",
            ) from error

    def _handle_event(self, node_id: int, event_id: str, timestamp: int) -> None:
        node = self._node(node_id)
        if (
            event_id == "clicked"
            and node.action is not None
            and node.properties["enabled"].value
        ):
            self._activate(node, timestamp)

    @dbus_method()
    def Event(self, node_id: "i", event_id: "s", _data: "v", timestamp: "u"):
        self._handle_event(node_id, event_id, timestamp)

    @dbus_method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        failures: list[int] = []
        for node_id, event_id, _data, timestamp in events:
            try:
                self._handle_event(node_id, event_id, timestamp)
            except DBusError:
                failures.append(node_id)
        return failures

    @dbus_method()
    def AboutToShow(self, node_id: "i") -> "b":
        self._node(node_id)
        return False

    @dbus_method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [], [node_id for node_id in ids if node_id not in self._nodes]

    @dbus_signal()
    def LayoutUpdated(self, revision: "u", parent: "i") -> "ui":
        return revision, parent

    @dbus_signal()
    def ItemsPropertiesUpdated(
        self, updated: "a(ia{sv})", removed: "a(ias)"
    ) -> "a(ia{sv})a(ias)":
        return updated, removed

    @dbus_signal()
    def ItemActivationRequested(self, node_id: "i", timestamp: "u") -> "iu":
        return node_id, timestamp


class _StatusNotifierItem(ServiceInterface):
    def __init__(
        self, identifier: str, title: str, icon_name: str, icon_theme_path: str
    ) -> None:
        super().__init__("org.kde.StatusNotifierItem")
        self._identifier = identifier
        self._title = title
        self._icon_name = icon_name
        self._icon_theme_path = icon_theme_path
        self._status = "Active"

    def update(self, title: str, icon_name: str, status: str) -> None:
        changed: dict[str, Any] = {}
        title_changed = title != self._title
        icon_changed = icon_name != self._icon_name
        status_changed = status != self._status
        self._title = title
        self._icon_name = icon_name
        self._status = status
        if title_changed:
            changed["Title"] = title
        if icon_changed:
            changed["IconName"] = icon_name
        if status_changed:
            changed["Status"] = status
        if changed:
            self.emit_properties_changed(changed)
        if title_changed:
            self.NewTitle()
        if icon_changed:
            self.NewIcon()
        if status_changed:
            self.NewStatus(status)

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return self._identifier

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return self._title

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return self._status

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        return self._icon_theme_path

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_OBJECT_PATH

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return self._icon_name

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> "s":
        return ""

    @dbus_method()
    def Activate(self, _x: "i", _y: "i"):
        return None

    @dbus_method()
    def ContextMenu(self, _x: "i", _y: "i"):
        return None

    @dbus_method()
    def SecondaryActivate(self, _x: "i", _y: "i"):
        return None

    @dbus_method()
    def XAyatanaSecondaryActivate(self, _timestamp: "u"):
        return None

    @dbus_method()
    def Scroll(self, _delta: "i", _orientation: "s"):
        return None

    @dbus_signal()
    def NewTitle(self):
        return None

    @dbus_signal()
    def NewIcon(self):
        return None

    @dbus_signal()
    def NewAttentionIcon(self):
        return None

    @dbus_signal()
    def NewOverlayIcon(self):
        return None

    @dbus_signal()
    def NewStatus(self, status: "s") -> "s":
        return status

    @dbus_signal()
    def NewIconThemePath(self, icon_theme_path: "s") -> "s":
        return icon_theme_path

    @dbus_signal()
    def NewMenu(self):
        return None


class NativeIndicator:
    """App-owned SNI state with a Shell-owned recursive menu surface."""

    def __init__(self, identifier: str, title: str, menu: Menu) -> None:
        self._identifier = identifier
        self._title = title
        self._menu = menu
        self._icon_name = "pair"
        self._icon: Any = None
        self._bus: MessageBus | None = None
        self._menu_service: _DBusMenu | None = None
        self._status_item: _StatusNotifierItem | None = None
        self._watcher: Any = None
        self._session_bus: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._registration_task: asyncio.Task | None = None
        self._registered = False
        self.registration_error: str | None = None
        self.on_connection_changed: Callable[[bool], None] = lambda _connected: None
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.activation_time = 0

    async def start(self) -> bool:
        if self._bus is not None:
            return self._registered
        self._loop = asyncio.get_running_loop()
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        menu_service = _DBusMenu(self._activate)
        status_item = _StatusNotifierItem(
            self._identifier,
            self._title,
            theme_icon_name(self._icon_name),
            str(Path(__file__).with_name("icons")),
        )
        bus.export(MENU_OBJECT_PATH, menu_service)
        bus.export(SNI_OBJECT_PATH, status_item)
        try:
            reply = await bus.request_name(self._bus_name)
            if reply not in (
                RequestNameReply.PRIMARY_OWNER,
                RequestNameReply.ALREADY_OWNER,
            ):
                raise RuntimeError(f"Could not own StatusNotifier name: {reply.name}")
            self._bus = bus
            self._menu_service = menu_service
            self._status_item = status_item
            self.update_menu()
            node = await bus.introspect(DBUS_BUS_NAME, DBUS_OBJECT_PATH)
            self._session_bus = bus.get_proxy_object(
                DBUS_BUS_NAME, DBUS_OBJECT_PATH, node
            ).get_interface(DBUS_BUS_NAME)
            self._session_bus.on_name_owner_changed(self._name_owner_changed)
        except Exception:
            bus.unexport(MENU_OBJECT_PATH)
            bus.unexport(SNI_OBJECT_PATH)
            bus.disconnect()
            self._bus = None
            self._menu_service = None
            self._status_item = None
            raise
        task = self._schedule_registration()
        if task is not None:
            await task
        return self._registered

    async def _register_with_watcher(self) -> None:
        if self._bus is None:
            return
        try:
            node = await self._bus.introspect(WATCHER_BUS_NAME, WATCHER_OBJECT_PATH)
            watcher: Any = self._bus.get_proxy_object(
                WATCHER_BUS_NAME, WATCHER_OBJECT_PATH, node
            ).get_interface(WATCHER_BUS_NAME)
            await watcher.call_register_status_notifier_item(self._bus_name)
        except (DBusError, OSError, asyncio.TimeoutError) as error:
            self._set_registered(False)
            message = f"StatusNotifier watcher unavailable: {error}"
            self.registration_error = message
            raise RuntimeError(message) from error
        self._watcher = watcher
        self.registration_error = None
        self._set_registered(True)

    def _name_owner_changed(self, name: str, _old_owner: str, new_owner: str) -> None:
        if name != WATCHER_BUS_NAME:
            return
        self._watcher = None
        self._set_registered(False)
        if new_owner:
            self._schedule_registration()

    def _schedule_registration(self) -> asyncio.Task | None:
        if self._loop is None or self._bus is None:
            return None
        if self._registration_task is not None and not self._registration_task.done():
            return self._registration_task
        self._registration_task = self._loop.create_task(
            self._register_after_watcher_appears()
        )
        return self._registration_task

    async def _register_after_watcher_appears(self) -> None:
        try:
            await self._register_with_watcher()
        except RuntimeError as error:
            logger.debug("Could not register XC Buddy with StatusNotifier: %s", error)

    def _set_registered(self, registered: bool) -> None:
        if registered == self._registered:
            return
        self._registered = registered
        try:
            self.on_connection_changed(registered)
        except Exception:
            logger.exception("StatusNotifier connection callback failed")

    def _activate(self, node: _MenuNode, timestamp: int) -> None:
        if node.action is None or node.item is None:
            return
        self.activation_time = timestamp
        try:
            node.action(self, node.item)
        except Exception:
            logger.exception("Status menu action failed")

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value
        self._publish_state()

    @property
    def icon(self) -> Any:
        return self._icon

    @icon.setter
    def icon(self, value: Any) -> None:
        self._icon = value

    @property
    def menu(self) -> Menu:
        return self._menu

    @menu.setter
    def menu(self, value: Menu) -> None:
        self._menu = value

    def set_state(
        self, icon_name: str, accessibility_description: str, _visible_title: str
    ) -> None:
        self._icon_name = icon_name
        self._title = f"XC Buddy — {accessibility_description}"
        self._publish_state()

    def _publish_state(self) -> None:
        if self._status_item is None:
            return
        self._status_item.update(
            self._title,
            theme_icon_name(self._icon_name),
            "NeedsAttention" if self._icon_name == "error" else "Active",
        )

    def update_menu(self) -> None:
        if self._menu_service is None:
            return
        self._menu_service.set_menu(self._menu)
        if self._status_item is not None:
            self._status_item.NewMenu()

    def pump(self) -> None:
        return

    def stop(self) -> None:
        bus = self._bus
        if bus is None:
            return
        self._bus = None
        self._menu_service = None
        self._status_item = None
        self._watcher = None
        self._session_bus = None
        self._loop = None
        self._registered = False
        if self._registration_task is not None:
            self._registration_task.cancel()
            self._registration_task = None
        bus.unexport(MENU_OBJECT_PATH)
        bus.unexport(SNI_OBJECT_PATH)
        bus.disconnect()
