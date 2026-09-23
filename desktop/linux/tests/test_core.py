import asyncio
import dataclasses
import hashlib
import io
import json
import os
import runpy
import ssl
import struct
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import build_identity, config, protocol, services, updater
from app.app import (
    _configure_gtk_backend,
    _correct_gtk_scale,
    _create_event_root,
    _desktop_integration_missing,
    _start_core_before_desktop_ui,
)
from app.bridge import (
    CodexBridge,
    RelayClient,
    classify_event,
    decode_relay_message,
)
from app.bluetooth import BluetoothManager, ConnectedXCDevice
from app.coordinator import Coordinator, Cycle
from app.input import InputInjector
from app.gtk_layout import present_from_tray, ui_scale
from app import native_indicator
from app.native_indicator import Menu as NativeMenu
from app.native_indicator import MenuItem as NativeMenuItem
from app.native_indicator import (
    _DBusMenu,
    _StatusNotifierItem,
    icon_path,
    theme_icon_name,
)
from app.ogg import OggOpusMuxer, ogg_crc
from app.services import (
    AppRelease,
    ServiceError,
    TranscriptionClient,
    TranslationClient,
    download_app_release,
    latest_app_release,
    latest_firmware,
    linux_release_asset_name,
    version_is_older,
)
from app.status import tray_presentation
from app.ui import (
    POSITION_NAMES,
    THEME_NAMES,
    TRANSLATION_TARGETS,
    DesktopUI,
)


class ProtocolTests(unittest.TestCase):
    def test_audio_frame(self):
        payload = b"opus"
        raw = (
            struct.pack("<BBHIIBBH", 1, 1, 16, 0x1234, 7, 3, 0, len(payload)) + payload
        )
        frame = protocol.parse_audio_frame(raw)
        self.assertEqual(
            (frame.session_id, frame.sequence, frame.payload), (0x1234, 7, payload)
        )
        self.assertTrue(frame.is_start and frame.is_end)
        self.assertIsNone(protocol.parse_audio_frame(raw[:-1]))

    def test_state_and_control_json(self):
        body = b'{"event":"button_down","button":"primary","session_id":4}'
        self.assertEqual(
            protocol.parse_event(struct.pack("<BBH", 1, 0x10, len(body)) + body)[
                "session_id"
            ],
            4,
        )
        self.assertEqual(
            json.loads(protocol.ui_state("ready")),
            {"event": "ui_state", "state": "ready", "text": ""},
        )
        invalid = b'{"event":"button_down","session_id":"4"}'
        self.assertIsNone(
            protocol.parse_event(struct.pack("<BBH", 1, 0x10, len(invalid)) + invalid)
        )

    def test_build_identity_uses_only_a_valid_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build-info.json"
            path.write_text(
                json.dumps(
                    {
                        "build_id": "0.2.1-abc123",
                        "source_revision": "deadbeef",
                        "wheel_sha256": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                build_identity, "package_version", return_value="0.2.1"
            ):
                self.assertEqual(
                    build_identity.version_text(path), "XC Buddy 0.2.1 (0.2.1-abc123)"
                )
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(build_identity.load_build_info(path), {})

    def test_ota_frames(self):
        self.assertEqual(
            protocol.ota_begin(100, 9), struct.pack("<BBHII", 1, 0x20, 12, 100, 9)
        )
        self.assertEqual(protocol.ota_data(9, 3, b"ab")[-2:], b"ab")
        self.assertEqual(len(protocol.ota_abort(9)), 8)


class ArtifactInstallTests(unittest.TestCase):
    def test_linux_icons_are_extracted_from_the_macos_asset(self):
        builder = runpy.run_path(
            str(Path(__file__).parents[1] / "scripts" / "build-user.py")
        )
        mac_icon = Path(__file__).parents[2] / "macos" / "Resources" / "AppIcon.icns"
        with tempfile.TemporaryDirectory() as directory:
            for size in (256, 512):
                destination = Path(directory) / f"AppIcon-{size}.png"
                builder["extract_linux_icon"](mac_icon, destination, (size, size))
                image = destination.read_bytes()

                self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(builder["png_dimensions"](image), (size, size))

    def test_launcher_registers_the_macos_icon_in_the_hicolor_theme(self):
        installer = runpy.run_path(
            str(Path(__file__).parents[1] / "scripts" / "install-user.py")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "AppIcon.png").write_bytes(b"\x89PNG\r\n\x1a\n512")
            (artifact / "AppIcon-256.png").write_bytes(b"\x89PNG\r\n\x1a\n256")
            (artifact / "ai.xc.buddy.desktop").write_text(
                "Exec=xc-buddy\nIcon=__XC_BUDDY_APP_ICON__\n", encoding="utf-8"
            )
            data_home = root / "data"
            data_root = data_home / "xc-buddy"
            icon_name = installer["install_app_icon"](artifact, data_home)
            installer["install_launchers"](
                artifact,
                data_root,
                root / "bin",
                root / "config",
                icon_name,
            )

            entry = (data_home / "applications" / "ai.xc.buddy.desktop").read_text(
                encoding="utf-8"
            )
            self.assertEqual(icon_name, "ai.xc.buddy")
            self.assertEqual(
                (data_home / "icons/hicolor/512x512/apps/ai.xc.buddy.png").read_bytes(),
                b"\x89PNG\r\n\x1a\n512",
            )
            self.assertEqual(
                (data_home / "icons/hicolor/256x256/apps/ai.xc.buddy.png").read_bytes(),
                b"\x89PNG\r\n\x1a\n256",
            )
            self.assertIn("Icon=ai.xc.buddy", entry)
            self.assertIn(
                'export XC_BUDDY_LAUNCHER="$0"',
                (root / "bin/xc-buddy").read_text(),
            )

    def test_release_archive_contains_one_verified_artifact_root(self):
        builder = runpy.run_path(
            str(Path(__file__).parents[1] / "scripts" / "build-user.py")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "0.2.1-build"
            artifact.mkdir()
            (artifact / "build-info.json").write_text("{}", encoding="utf-8")
            (artifact / "install-user.py").write_text("# installer\n", encoding="utf-8")
            archive = builder["write_release_archive"](artifact, root, "0.2.1")
            self.assertEqual(archive.name, linux_release_asset_name("0.2.1"))
            with tarfile.open(archive, "r:gz") as bundle:
                self.assertIn("0.2.1-build/build-info.json", bundle.getnames())
            extracted = updater._safe_extract(archive, root / "extracted")
            self.assertEqual(extracted.name, artifact.name)
            self.assertTrue((extracted / "install-user.py").is_file())
            self.assertTrue(Path(f"{archive}.sha256").is_file())

    def test_updater_rejects_unsafe_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                payload = b"bad"
                member = tarfile.TarInfo("../outside")
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(RuntimeError, "unsafe path"):
                updater._safe_extract(archive, root / "output")

    def test_updater_is_available_only_from_the_managed_launcher(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(updater.standard_update_context())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "data/xc-buddy/current"
            current.mkdir(parents=True)
            build_info = current / "build-info.json"
            build_info.write_text("{}", encoding="utf-8")
            launcher = root / "bin/xc-buddy"
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            environment = {
                build_identity.BUILD_INFO_ENV: str(build_info),
                updater.LAUNCHER_ENV: str(launcher),
                "XDG_CONFIG_HOME": str(root / "config"),
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                context = updater.standard_update_context()

            self.assertIsNotNone(context)
            self.assertEqual(context.data_home, root / "data")
            self.assertEqual(context.config_home, root / "config")
            self.assertEqual(context.launcher, launcher)

    def test_icon_cache_refresh_is_optional(self):
        installer = runpy.run_path(
            str(Path(__file__).parents[1] / "scripts" / "install-user.py")
        )
        with (
            mock.patch.object(
                installer["shutil"],
                "which",
                return_value="/usr/bin/gtk-update-icon-cache",
            ),
            mock.patch.object(installer["subprocess"], "run") as run,
        ):
            installer["refresh_icon_cache"](Path("/tmp/data"))

        run.assert_called_once_with(
            [
                "/usr/bin/gtk-update-icon-cache",
                "--force",
                "--ignore-theme-index",
                "/tmp/data/icons/hicolor",
            ],
            check=False,
            stdout=installer["subprocess"].DEVNULL,
            stderr=installer["subprocess"].DEVNULL,
        )

    def test_desktop_database_refresh_is_optional(self):
        installer = runpy.run_path(
            str(Path(__file__).parents[1] / "scripts" / "install-user.py")
        )
        with (
            mock.patch.object(
                installer["shutil"],
                "which",
                return_value="/usr/bin/update-desktop-database",
            ),
            mock.patch.object(installer["subprocess"], "run") as run,
        ):
            installer["refresh_desktop_database"](Path("/tmp/data"))

        run.assert_called_once_with(
            ["/usr/bin/update-desktop-database", "/tmp/data/applications"],
            check=False,
            stdout=installer["subprocess"].DEVNULL,
            stderr=installer["subprocess"].DEVNULL,
        )

    def test_integrity_requires_checksum_coverage_for_every_artifact_file(self):
        installer = runpy.run_path(
            str(Path(__file__).parents[1] / "scripts" / "install-user.py")
        )
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            payload = artifact / "payload"
            payload.mkdir()
            wheel = payload / "xc_buddy-0.2.1-py3-none-any.whl"
            wheel.write_bytes(b"not a real wheel; verification does not install it")
            lock = artifact / "runtime-requirements.lock"
            lock.write_text("example==1.0\n", encoding="utf-8")
            digest = installer["content_digest"](artifact)
            manifest = {
                "artifact_content_sha256": digest,
                "build_id": f"0.2.1-{digest[:12]}",
                "runtime_requirements_sha256": installer["sha256"](lock),
                "version": "0.2.1",
                "wheel_filename": wheel.name,
                "wheel_sha256": installer["sha256"](wheel),
            }
            (artifact / "build-info.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            checksums = sorted(path for path in artifact.rglob("*") if path.is_file())
            (artifact / "SHA256SUMS").write_text(
                "".join(
                    f"{installer['sha256'](path)}  "
                    f"{path.relative_to(artifact).as_posix()}\n"
                    for path in checksums
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                installer["verify_artifact"](artifact)["build_id"],
                manifest["build_id"],
            )
            (artifact / "wheelhouse").mkdir()
            (artifact / "wheelhouse" / "unexpected.whl").write_bytes(b"extra")
            with self.assertRaisesRegex(RuntimeError, "checksum coverage"):
                installer["verify_artifact"](artifact)


class OggTests(unittest.TestCase):
    def test_crc_known_vector(self):
        self.assertEqual(ogg_crc(b"123456789"), 0x89A1897F)

    def test_muxer_pages_and_checksums(self):
        muxer = OggOpusMuxer()
        stream = muxer.reset() + muxer.packet(b"\x11\x22") + muxer.finish()
        pages = []
        offset = 0
        while offset < len(stream):
            self.assertEqual(stream[offset : offset + 4], b"OggS")
            segments = stream[offset + 26]
            size = 27 + segments + sum(stream[offset + 27 : offset + 27 + segments])
            page = bytearray(stream[offset : offset + size])
            expected = struct.unpack_from("<I", page, 22)[0]
            page[22:26] = b"\0\0\0\0"
            self.assertEqual(ogg_crc(page), expected)
            pages.append(stream[offset : offset + size])
            offset += size
        self.assertEqual(len(pages), 4)
        self.assertIn(b"OpusHead", pages[0])
        self.assertIn(b"XC Buddy", pages[1])
        self.assertEqual(struct.unpack_from("<Q", pages[2], 6)[0], 2880)

    def test_v1_packet_limits_match_macos(self):
        muxer = OggOpusMuxer()
        with self.assertRaisesRegex(ValueError, "empty Opus"):
            muxer.packet(b"")
        with self.assertRaisesRegex(ValueError, "one lacing segment"):
            muxer.packet(b"x" * 256)


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = config.AppConfig(
                openai_api_key='a"b',
                openai_prompt="first line\nsecond\tline",
                paired_device_ids=["ABCD"],
                device_outputs={
                    "ABCD": config.OutputProfile("focused_app", "translate", "zh-CN")
                },
            )
            config.save(original, path)
            loaded = config.load(path)
            self.assertEqual(loaded.openai_api_key, 'a"b')
            self.assertEqual(loaded.openai_prompt, "first line\nsecond\tline")
            self.assertEqual(loaded.paired_device_ids, ["ABCD"])
            self.assertEqual(loaded.profile_for("ABCD").translation_target, "zh-CN")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_normalize_device_id(self):
        self.assertEqual(config.normalize_device_id("xc-a1b2 extra"), "A1B2")
        self.assertEqual(config.normalize_device_id("VS-a1b2"), "A1B2")

    def test_toml_comments_hotwords_and_save_filtering_match_macos(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'asr_hotwords = "CUDA\\nCodex,CUDA" # inline comment\n'
                'paired_device_ids = "ABCD"\n'
                'device_theme_colors = "ABCD:white,BCDE:pink"\n'
                'device_overlay_positions = "ABCD:center,BCDE:top_left"\n'
                '[output]\ntranslation_target = "  fr  "\n'
            )
            loaded = config.load(path)
            self.assertEqual(loaded.asr_hotwords, ["CUDA", "Codex"])
            self.assertEqual(loaded.output.translation_target, "fr")
            config.save(loaded, path)
            text = path.read_text()
        self.assertIn('device_theme_colors = ""', text)
        self.assertIn('device_overlay_positions = ""', text)

    def test_device_profile_inherits_changed_default_target(self):
        settings = config.AppConfig(
            device_outputs={
                "ABCD": config.OutputProfile("focused_app", "translate", "fr")
            }
        )
        settings.output.target = "subtitle"
        self.assertEqual(settings.profile_for("ABCD").target, "subtitle")

    def test_legacy_booleans_and_invalid_device_style_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'auto_enter = "yes"\n'
                'debug_audio_cache = "1"\n'
                'device_theme_colors = "ABCD:orange,BCDE:pink"\n'
                'device_overlay_positions = "ABCD:middle,BCDE:top_left"\n'
            )
            loaded = config.load(path)
        self.assertTrue(loaded.auto_enter and loaded.debug_audio_cache)
        self.assertEqual(loaded.device_theme_colors, {"BCDE": "pink"})
        self.assertEqual(loaded.device_overlay_positions, {"BCDE": "top_left"})


class StatusTests(unittest.TestCase):
    @mock.patch("app.app._correct_gtk_scale")
    def test_tk_dpi_probe_is_destroyed_before_desktop_runtime(self, correct_scale):
        tk_module = mock.Mock()
        scale_probe = tk_module.Tk.return_value

        root = _create_event_root(tk_module)

        scale_probe.withdraw.assert_called_once_with()
        correct_scale.assert_called_once_with(scale_probe)
        scale_probe.destroy.assert_called_once_with()
        root.update()
        root.destroy()

    @mock.patch.object(InputInjector, "atspi_available", return_value=True)
    @mock.patch("app.app.shutil.which")
    def test_gnome_wayland_uses_atspi_instead_of_unsupported_wtype(
        self, which, _atspi_available
    ):
        which.side_effect = lambda command: (
            f"/usr/bin/{command}"
            if command in ("wl-copy", "wl-paste", "xmodmap")
            else None
        )
        with mock.patch.dict(
            os.environ,
            {
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            },
            clear=True,
        ):
            self.assertEqual(_desktop_integration_missing(), [])

    @mock.patch.object(InputInjector, "gtk_clipboard_available", return_value=True)
    @mock.patch.object(InputInjector, "atspi_available", return_value=True)
    @mock.patch("app.app.shutil.which")
    def test_gnome_wayland_accepts_the_gtk_clipboard_fallback(
        self, which, _atspi_available, _gtk_clipboard_available
    ):
        which.side_effect = lambda command: (
            f"/usr/bin/{command}" if command == "xmodmap" else None
        )
        with mock.patch.dict(
            os.environ,
            {
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            },
            clear=True,
        ):
            self.assertEqual(_desktop_integration_missing(), [])

    def test_native_tray_state_icons_are_bundled(self):
        for name in ("pair", "ready", "listening", "processing", "error"):
            path = Path(icon_path(name))
            self.assertTrue(path.is_file(), path)
            self.assertEqual(path.suffix, ".svg")
            # Ubuntu's AppIndicators extension asks for an optional
            # ``-panel`` variant before using the normal StatusNotifier name.
            # Keep an explicit equivalent asset so the indicator is visible
            # rather than relying on an icon-theme fallback.
            panel_path = path.with_name(f"{path.stem}-panel{path.suffix}")
            self.assertTrue(panel_path.is_file(), panel_path)
            self.assertEqual(theme_icon_name(name), f"xc-buddy-{name}-symbolic")

    def test_tray_states_match_macos_status_controller(self):
        cases = (
            ("Pair XC", False, ("pairing", "pair", "Pair XC", "Pair")),
            ("Listening", True, ("listening", "listening", "Listening", "")),
            ("ASR error: bad", True, ("error", "error", "Error", "Error")),
            (
                "Transcribing",
                True,
                ("processing", "processing", "Processing", "Processing"),
            ),
            ("Ready", False, ("ready", "pair", "Ready", "")),
            ("Ready", True, ("ready", "ready", "Ready", "")),
        )
        for text, connected, expected in cases:
            with self.subTest(text=text, connected=connected):
                value = tray_presentation(text, connected)
                self.assertEqual(
                    (
                        value.state,
                        value.icon_name,
                        value.accessibility_description,
                        value.visible_title,
                    ),
                    expected,
                )

    @mock.patch("app.app.subprocess.run")
    def test_standard_dpi_screen_corrects_accidental_gnome_2x_scale(self, run):
        root = mock.Mock()
        root.winfo_screenwidth.return_value = 3440
        root.winfo_screenmmwidth.return_value = 802
        run.return_value.stdout = "Xft.dpi:\t192\n"
        with mock.patch.dict(os.environ, {}, clear=True):
            _correct_gtk_scale(root)
            self.assertEqual(os.environ["GDK_SCALE"], "1")
            self.assertEqual(os.environ["GDK_DPI_SCALE"], "0.624")
            self.assertEqual(os.environ["XC_BUDDY_UI_SCALE"], "1.135")

    def test_gnome_wayland_uses_positionable_x11_gtk_windows(self):
        with mock.patch.dict(
            os.environ,
            {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":1"},
            clear=True,
        ):
            _configure_gtk_backend()
            self.assertEqual(os.environ["GDK_BACKEND"], "x11")

    def test_explicit_gtk_backend_is_preserved(self):
        with mock.patch.dict(
            os.environ,
            {
                "WAYLAND_DISPLAY": "wayland-0",
                "DISPLAY": ":1",
                "GDK_BACKEND": "wayland",
            },
            clear=True,
        ):
            _configure_gtk_backend()
            self.assertEqual(os.environ["GDK_BACKEND"], "wayland")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _tls_contexts():
        fixtures = Path(__file__).with_name("fixtures")
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(
            fixtures / "localhost-cert.pem", fixtures / "localhost-key.pem"
        )
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        return server, client

    @staticmethod
    def _request_headers(socket):
        request = getattr(socket, "request", None)
        return getattr(request, "headers", getattr(socket, "request_headers", {}))

    async def test_authenticated_http_event(self):
        events = []
        bridge = CodexBridge(
            0, "secret", lambda event, message: events.append((event, message))
        )
        await bridge.start()
        port = bridge.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = b'{"type":"agent-turn-complete"}'
        writer.write(
            b"POST / HTTP/1.1\r\nAuthorization: Bearer secret\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        await bridge.stop()
        self.assertIn(b"204 No Content", response)
        self.assertEqual(events, [("done", "")])

    def test_classification(self):
        self.assertEqual(
            classify_event({"type": "tool-call-start"})[0], "tool_call_started"
        )
        self.assertEqual(
            classify_event({"type": "TOOL-CALL-START"})[0], "tool_call_started"
        )
        self.assertEqual(
            classify_event({"event": "approval-requested"})[0], "approval_needed"
        )
        self.assertEqual(
            classify_event({"event": "turn-failed", "message": "bad"}), ("error", "bad")
        )
        self.assertEqual(classify_event({"type": 42}), ("working", ""))

    def test_relay_messages_are_strict_and_malformed_messages_are_ignored(self):
        self.assertEqual(
            decode_relay_message('{"kind":"state","state":"working","snapshot":true}'),
            ("working", ""),
        )
        self.assertEqual(
            decode_relay_message('{"kind":"notice","notice":"error"}'),
            ("error", "Codex error"),
        )
        self.assertIsNone(decode_relay_message("not json"))
        self.assertIsNone(
            decode_relay_message('{"kind":"state","state":"working","snapshot":"true"}')
        )
        self.assertIsNone(
            decode_relay_message('{"kind":"notice","notice":"done","unexpected":1}')
        )

    async def test_sender_detects_idle_socket_closure(self):
        class Socket:
            def __init__(self):
                self.closed = asyncio.Event()

            async def wait_closed(self):
                await self.closed.wait()

            async def send(self, _message):
                pass

        client = RelayClient(lambda *_: None, lambda *_: None)
        socket = Socket()
        task = asyncio.create_task(client._send_messages(socket))
        socket.closed.set()
        with self.assertRaisesRegex(ConnectionError, "relay connection closed"):
            await asyncio.wait_for(task, 1)

    async def test_missing_relay_token_names_the_role_without_repeating_relay(self):
        statuses: list[str] = []
        client = RelayClient(lambda *_: None, statuses.append)

        await client.start("receiver", "wss://relay.example", "")

        self.assertEqual(statuses, ["Receiver token is not set"])
        self.assertIsNone(client.task)

    async def test_sender_over_real_local_tls(self):
        import websockets

        received = []
        authorization = []
        connected = asyncio.Event()
        complete = asyncio.Event()

        async def handler(socket):
            authorization.append(self._request_headers(socket).get("Authorization"))
            received.append(json.loads(await socket.recv()))
            received.append(json.loads(await socket.recv()))
            complete.set()
            await socket.wait_closed()

        server_context, client_context = self._tls_contexts()
        server = await websockets.serve(handler, "127.0.0.1", 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        client = RelayClient(
            lambda *_: None,
            lambda status: connected.set() if status == "Connected" else None,
            ssl_context=client_context,
        )
        client.retain_sender_state("working")
        try:
            with mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1"}):
                await client.start("sender", f"wss://127.0.0.1:{port}", "secret")
                await asyncio.wait_for(connected.wait(), 2)
                client.publish("approval_needed")
                await asyncio.wait_for(complete.wait(), 2)
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()
        self.assertEqual(authorization, ["Bearer secret"])
        self.assertEqual(
            received,
            [
                {"kind": "state", "state": "working"},
                {"kind": "state", "state": "approval_needed"},
            ],
        )

    async def test_receiver_over_real_local_tls(self):
        import websockets

        events = []
        authorization = []
        complete = asyncio.Event()

        async def handler(socket):
            authorization.append(self._request_headers(socket).get("Authorization"))
            await socket.send(
                json.dumps(
                    {
                        "kind": "state",
                        "state": "approval_needed",
                        "snapshot": True,
                    }
                )
            )
            await socket.send(json.dumps({"kind": "notice", "notice": "done"}))
            await socket.wait_closed()

        def receive(event, message):
            events.append((event, message))
            if len(events) == 2:
                complete.set()

        server_context, client_context = self._tls_contexts()
        server = await websockets.serve(handler, "127.0.0.1", 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        client = RelayClient(receive, lambda *_: None, ssl_context=client_context)
        try:
            with mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1"}):
                await client.start("receiver", f"wss://127.0.0.1:{port}", "secret")
                await asyncio.wait_for(complete.wait(), 2)
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()
        self.assertEqual(authorization, ["Bearer secret"])
        self.assertEqual(events, [("approval_needed", ""), ("done", "")])


class ServiceTests(unittest.TestCase):
    @mock.patch("app.services.urllib.request.urlopen")
    def test_latest_app_release_uses_the_app_release_channel(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            [
                {"tag_name": "v0.1.7", "draft": False, "prerelease": False},
                {"tag_name": "app-v0.2.2", "draft": False, "prerelease": True},
            ]
        ).encode()
        urlopen.return_value.__enter__.return_value = response

        self.assertEqual(services._latest_github_release_version(), "0.2.2")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, services.GITHUB_RELEASES_API_URL)

    @mock.patch("app.services.time.sleep")
    @mock.patch("app.services.urllib.request.urlopen")
    def test_latest_app_release_retries_a_transient_github_denial(self, urlopen, sleep):
        response = mock.MagicMock()
        response.read.return_value = b'[{"tag_name":"app-v0.2.2","draft":false}]'
        response.__enter__.return_value = response
        urlopen.side_effect = [
            services.urllib.error.HTTPError(
                services.GITHUB_RELEASES_API_URL,
                403,
                "Forbidden",
                {},
                None,
            ),
            response,
        ]

        self.assertEqual(services._latest_github_release_version(), "0.2.2")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(services.GITHUB_LOOKUP_RETRY_SECONDS)

    @mock.patch("app.services.time.sleep")
    @mock.patch("app.services.urllib.request.urlopen")
    def test_latest_app_release_uses_feed_when_api_limit_is_exhausted(
        self, urlopen, _sleep
    ):
        denied = services.urllib.error.HTTPError(
            services.GITHUB_RELEASES_API_URL, 403, "Forbidden", {}, None
        )
        feed = mock.MagicMock()
        feed.read.return_value = (
            b'<feed xmlns="http://www.w3.org/2005/Atom">'
            b'<entry><link rel="alternate" href="https://github.com/'
            b'SiyiLi/xc-buddy/releases/tag/v0.1.7"/></entry>'
            b'<entry><link rel="alternate" href="https://github.com/'
            b'SiyiLi/xc-buddy/releases/tag/app-v0.2.2"/></entry></feed>'
        )
        feed.__enter__.return_value = feed
        urlopen.side_effect = [denied, denied, denied, feed]

        self.assertEqual(services._latest_github_release_version(), "0.2.2")
        self.assertEqual(
            urlopen.call_args.args[0].full_url, services.GITHUB_RELEASES_FEED_URL
        )

    def test_firmware_version_order_matches_macos(self):
        self.assertTrue(version_is_older("v0.2.0", "0.2.1"))
        self.assertTrue(version_is_older("1.0-beta2", "1.0"))
        self.assertTrue(version_is_older("1.0-beta2", "1.0-beta10"))
        self.assertFalse(version_is_older("1.0", "1.0.0"))
        self.assertFalse(version_is_older("unknown", "1.0"))

    @mock.patch("app.services._request")
    def test_transcription_matches_macos_chat_audio_contract(self, request):
        request.return_value = b'{"choices":[{"message":{"content":" hello "}}]}'
        settings = config.AppConfig(openai_api_key="key", asr_hotwords=["CUDA"])
        self.assertEqual(TranscriptionClient(settings).transcribe(b"ogg"), "hello")
        url, body, headers = request.call_args.args
        payload = json.loads(body)
        self.assertEqual(url, "https://inference-api.nvidia.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer key")
        audio = payload["messages"][0]["content"][0]
        self.assertEqual(audio["input_audio"], {"data": "b2dn", "format": "ogg"})
        self.assertIn("CUDA", payload["messages"][0]["content"][1]["text"])

    @mock.patch("app.services._request")
    def test_empty_transcription_is_an_error(self, request):
        request.return_value = b'{"choices":[{"message":{"content":"  "}}]}'
        with self.assertRaisesRegex(
            ServiceError, "Transcription response contained no text"
        ):
            TranscriptionClient(config.AppConfig(openai_api_key="key")).transcribe(
                b"ogg"
            )

    @mock.patch("app.services._request")
    def test_transcription_runtime_errors_match_macos(self, request):
        settings = config.AppConfig(openai_api_key="key")
        request.side_effect = ServiceError("HTTP 503: unavailable")
        with self.assertRaisesRegex(
            ServiceError, "Transcription HTTP 503: unavailable"
        ):
            TranscriptionClient(settings).transcribe(b"ogg")

        request.side_effect = None
        request.return_value = b"not json"
        with self.assertRaisesRegex(ServiceError, "Invalid transcription response"):
            TranscriptionClient(settings).transcribe(b"ogg")

    def test_transcription_configuration_errors_are_provider_neutral(self):
        cases = (
            (config.AppConfig(), "Transcription API key is not set"),
            (
                config.AppConfig(openai_api_key="key", openai_model=""),
                "Transcription model is not set",
            ),
            (
                config.AppConfig(openai_api_key="key", openai_base_url="invalid"),
                "Transcription base URL is invalid",
            ),
        )
        for settings, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ServiceError, message
            ):
                TranscriptionClient(settings).transcribe(b"ogg")

    @mock.patch("app.services._request")
    def test_gpt_56_translation_parameters(self, request):
        request.return_value = b'{"choices":[{"message":{"content":" bonjour "}}]}'
        settings = config.AppConfig(
            llm_api_key="key", llm_model="gpt-5.6", asr_hotwords=["Codex"]
        )
        self.assertEqual(
            TranslationClient(settings).translate("hello", "French"), "bonjour"
        )
        payload = json.loads(request.call_args.args[1])
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertNotIn("temperature", payload)
        self.assertIn("Codex", payload["messages"][0]["content"])

    def test_translation_configuration_errors_match_macos(self):
        with self.assertRaisesRegex(ServiceError, "Missing LLM API key"):
            TranslationClient(config.AppConfig()).translate("hello", "French")
        with self.assertRaisesRegex(ServiceError, "Invalid LLM base URL"):
            TranslationClient(
                config.AppConfig(llm_api_key="key", llm_base_url="invalid")
            ).translate("hello", "French")

    @mock.patch("app.services._request")
    def test_invalid_translation_response_matches_macos(self, request):
        request.return_value = b"{}"
        with self.assertRaisesRegex(ServiceError, "Invalid LLM translation response"):
            TranslationClient(config.AppConfig(llm_api_key="key")).translate(
                "hello", "French"
            )

    @mock.patch("app.services._request")
    def test_firmware_release_metadata_errors_match_macos(self, request):
        request.side_effect = ServiceError("HTTP 404: not found")
        with self.assertRaisesRegex(
            ServiceError, "No published firmware release is available yet"
        ):
            latest_firmware()

        request.side_effect = None
        request.return_value = b"not json"
        with self.assertRaisesRegex(
            ServiceError, "GitHub returned an invalid firmware release response"
        ):
            latest_firmware()

    @mock.patch("app.services._request")
    def test_firmware_release_uses_manifest_version_not_app_tag(self, request):
        manifest = json.dumps(
            {
                "hardware": "stick_s3",
                "version": "0.1.7",
                "ota_asset": "xc-buddy-sticks3-ota.bin",
            }
        ).encode()
        firmware = b"firmware image"
        request.side_effect = [
            json.dumps(
                {
                    "tag_name": "v0.2.1",
                    "assets": [
                        {
                            "name": "xc-buddy-sticks3-firmware.json",
                            "browser_download_url": "https://example.test/manifest.json",
                            "digest": f"sha256:{hashlib.sha256(manifest).hexdigest()}",
                            "size": len(manifest),
                        },
                        {
                            "name": "xc-buddy-sticks3-ota.bin",
                            "browser_download_url": "https://example.test/ota.bin",
                            "digest": f"sha256:{hashlib.sha256(firmware).hexdigest()}",
                            "size": len(firmware),
                        },
                    ],
                }
            ).encode(),
            manifest,
        ]

        release = latest_firmware()

        self.assertEqual(release.version, "0.1.7")
        self.assertEqual(release.url, "https://example.test/ota.bin")
        self.assertEqual(request.call_count, 2)

    @mock.patch("app.services._request")
    def test_firmware_manifest_checksum_is_verified(self, request):
        manifest = b'{"hardware":"stick_s3","version":"0.1.7"}'
        request.side_effect = [
            json.dumps(
                {
                    "tag_name": "v0.2.1",
                    "assets": [
                        {
                            "name": "xc-buddy-sticks3-firmware.json",
                            "browser_download_url": "https://example.test/manifest.json",
                            "digest": "sha256:" + "0" * 64,
                            "size": len(manifest),
                        },
                        {
                            "name": "xc-buddy-sticks3-ota.bin",
                            "browser_download_url": "https://example.test/ota.bin",
                            "digest": "sha256:" + "1" * 64,
                            "size": 1,
                        },
                    ],
                }
            ).encode(),
            manifest,
        ]
        with self.assertRaisesRegex(
            ServiceError,
            "The downloaded firmware manifest checksum does not match GitHub",
        ):
            latest_firmware()

    @mock.patch("app.services._latest_github_release_version", return_value="0.2.2")
    @mock.patch("app.services._request")
    def test_app_release_selects_and_verifies_this_linux_runtime(
        self, request, _latest_version
    ):
        archive = b"release archive"
        name = linux_release_asset_name("0.2.2")
        digest = hashlib.sha256(archive).hexdigest()
        request.side_effect = [f"{digest}  {name}\n".encode(), archive]
        release = latest_app_release()
        self.assertEqual(
            release,
            AppRelease(
                "0.2.2",
                f"https://github.com/SiyiLi/xc-buddy/releases/download/app-v0.2.2/{name}",
                digest,
                0,
                name,
            ),
        )
        checksum_url = request.call_args_list[0].args[0]
        self.assertEqual(checksum_url, f"{release.url}.sha256")
        self.assertNotIn("api.github.com", checksum_url)
        self.assertEqual(download_app_release(release), archive)

    @mock.patch("app.services._latest_github_release_version", return_value="0.2.1")
    @mock.patch("app.services._request")
    def test_current_app_version_does_not_require_a_release_asset(
        self, request, _latest_version
    ):
        release = latest_app_release("0.2.1")
        self.assertEqual(release.version, "0.2.1")
        self.assertEqual(release.url, "")
        request.assert_not_called()


class InputTests(unittest.TestCase):
    def test_x_keycodes_follow_the_active_keyboard_map(self):
        mapping = """
             36         0xff0d (Return)  0x0000 (NoSymbol)
             37         0xffe3 (Control_L)  0xffe4 (Control_R)
             50         0xffe1 (Shift_L)  0xffe2 (Shift_R)
             55         0x0076 (v)  0x0056 (V)
        """
        self.assertEqual(InputInjector._x_keycode(mapping, "Control_L"), 29)
        self.assertEqual(InputInjector._x_keycode(mapping, "v", "V"), 47)
        self.assertEqual(InputInjector._x_keycode(mapping, "Return"), 28)

    def test_terminal_focus_uses_terminal_paste_shortcut(self):
        self.assertTrue(
            InputInjector._requires_terminal_paste(
                "gnome-terminal-server Terminal role=terminal"
            )
        )
        self.assertFalse(
            InputInjector._requires_terminal_paste("org.gnome.TextEditor role=text")
        )

    def test_atspi_keyboard_event_failure_is_not_silently_ignored(self):
        atspi = mock.Mock()
        atspi.generate_keyboard_event.return_value = False
        with self.assertRaisesRegex(RuntimeError, "rejected keyboard event"):
            InputInjector._atspi_event(atspi, 55, object())

    @mock.patch("app.input.shutil.which")
    def test_wayland_compositor_wins_over_xwayland_for_codex_detection(self, which):
        which.side_effect = lambda command: (
            f"/usr/bin/{command}" if command in ("swaymsg", "xdotool") else None
        )
        injector = InputInjector()
        tree = {
            "nodes": [
                {
                    "focused": True,
                    "app_id": "com.openai.codex",
                    "name": "Codex",
                }
            ]
        }
        with mock.patch.dict(
            os.environ,
            {
                "WAYLAND_DISPLAY": "wayland-1",
                "DISPLAY": ":0",
                "SWAYSOCK": "/tmp/sway.sock",
            },
            clear=True,
        ), mock.patch.object(
            injector, "_output", return_value=json.dumps(tree)
        ) as output:
            self.assertTrue(injector.frontmost_application_is_codex())
        self.assertEqual(output.call_args.args[0][0], "swaymsg")

    @mock.patch("app.input.subprocess.run")
    def test_clipboard_restore_guard_only_matches_unchanged_temporary_text(self, run):
        run.return_value.stdout = b"temporary"
        self.assertTrue(InputInjector._clipboard_matches(["clipboard"], "temporary"))
        self.assertFalse(InputInjector._clipboard_matches(["clipboard"], "different"))
        run.return_value.stdout = b"temporary\n"
        self.assertFalse(InputInjector._clipboard_matches(["clipboard"], "temporary"))

    @mock.patch("app.input.shutil.which", return_value=None)
    def test_wayland_uses_gtk_clipboard_when_wl_clipboard_is_unavailable(self, _which):
        injector = InputInjector()
        restore = mock.Mock()
        with mock.patch.object(
            injector, "_gtk_clipboard_text", return_value=restore
        ) as gtk_clipboard:
            result = injector._wayland_clipboard("recognized text")

        self.assertIs(result, restore)
        gtk_clipboard.assert_called_once_with("recognized text")

    def test_gtk_clipboard_restores_unchanged_temporary_text(self):
        clipboard = mock.Mock()
        clipboard.wait_for_text.side_effect = ["before", "temporary"]
        with mock.patch.object(
            InputInjector, "_gtk_main", side_effect=lambda callback: callback()
        ), mock.patch.object(InputInjector, "_gtk_clipboard", return_value=clipboard):
            restore = InputInjector._gtk_clipboard_text("temporary")
            restore()

        self.assertEqual(
            clipboard.set_text.call_args_list,
            [mock.call("temporary", -1), mock.call("before", -1)],
        )

    def test_gnome_introspection_selects_only_the_focused_window(self):
        response = """({uint64 1: {'title': <'Other'>, 'has-focus': <false>},
                     uint64 2: {'title': <'Codex'>, 'wm-class': <'codex'>,
                                'has-focus': <true>}},)"""
        focused = InputInjector._focused_gnome_window(response)
        self.assertIn("Codex", focused)
        self.assertNotIn("Other", focused)


class DesktopStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_starts_before_ui_and_survives_ui_failure(self):
        events: list[str] = []

        class Core:
            async def start(self):
                events.append("core")

        class UI:
            async def start_tray(self):
                events.append("ui")
                raise RuntimeError("desktop unavailable")

        with self.assertLogs("app.app", "ERROR"):
            await _start_core_before_desktop_ui(Core(), UI())

        self.assertEqual(events, ["core", "ui"])


class DesktopUITests(unittest.TestCase):
    def test_start_tray_uses_status_notifier(self):
        ui = DesktopUI.__new__(DesktopUI)
        menu = object()
        tray = mock.Mock()
        tray.start = mock.AsyncMock(return_value=True)
        ui._menu = mock.Mock(return_value=menu)
        ui._refresh = mock.Mock()
        ui._show_control_window = mock.Mock()

        with mock.patch(
            "app.native_indicator.NativeIndicator", return_value=tray
        ) as native:
            asyncio.run(ui.start_tray())

        native.assert_called_once_with("xc-buddy", "XC Buddy", menu)
        tray.start.assert_awaited_once_with()
        self.assertIs(ui.tray, tray)
        ui._refresh.assert_called_once_with()
        ui._show_control_window.assert_not_called()

    def test_start_tray_keeps_indicator_alive_while_showing_fallback(self):
        ui = DesktopUI.__new__(DesktopUI)
        menu = object()
        tray = mock.Mock()
        tray.start = mock.AsyncMock(return_value=False)
        tray.registration_error = "no watcher"
        ui._menu = mock.Mock(return_value=menu)
        ui._refresh = mock.Mock()
        ui._show_control_window = mock.Mock()

        with self.assertLogs("app.ui", "WARNING"):
            with mock.patch("app.native_indicator.NativeIndicator", return_value=tray):
                asyncio.run(ui.start_tray())

        self.assertIs(ui.tray, tray)
        ui._refresh.assert_called_once_with()
        ui._show_control_window.assert_called_once_with()

    def test_tray_connection_changes_switch_the_fallback_window(self):
        ui = DesktopUI.__new__(DesktopUI)
        window = ui._control_window = mock.Mock()
        ui._show_control_window = mock.Mock()

        ui._tray_connection_changed(True)

        window.destroy.assert_called_once_with()
        self.assertIsNone(ui._control_window)
        ui._tray_connection_changed(False)
        ui._show_control_window.assert_called_once_with()

    def test_overlay_scale_uses_the_same_bounded_ui_scale_as_app_windows(self):
        with mock.patch.dict(os.environ, {"XC_BUDDY_UI_SCALE": "1.135"}, clear=True):
            self.assertEqual(ui_scale(), 1.135)
        with mock.patch.dict(os.environ, {"XC_BUDDY_UI_SCALE": "9"}, clear=True):
            self.assertEqual(ui_scale(), 1.25)

    @mock.patch("app.gtk_layout._x11_server_time", return_value=9876)
    def test_tray_window_uses_server_time_when_host_omits_activation_time(
        self, _server_time
    ):
        window = mock.Mock()

        present_from_tray(window)

        window.present_with_time.assert_called_once_with(9876)
        window.present.assert_not_called()

    @mock.patch("app.gtk_layout._x11_server_time")
    def test_tray_window_preserves_a_host_activation_time(self, server_time):
        window = mock.Mock()

        present_from_tray(window, 1234)

        server_time.assert_not_called()
        window.present_with_time.assert_called_once_with(1234)
        window.present.assert_not_called()

    def test_pump_services_tray_and_gtk_windows(self):
        ui = DesktopUI.__new__(DesktopUI)
        ui.tray = mock.Mock()
        ui._runtime_presenter = None
        ui._gtk_windows = [object()]
        Gtk = mock.Mock()
        Gtk.events_pending.side_effect = [True, False]

        with mock.patch("app.gtk_onboarding._gtk_module", return_value=Gtk):
            ui.pump()

        ui.tray.pump.assert_called_once_with()
        Gtk.main_iteration_do.assert_called_once_with(False)

    @mock.patch("app.ui.present_from_tray")
    def test_open_settings_reuses_and_raises_existing_window(self, present):
        ui = DesktopUI.__new__(DesktopUI)
        ui._settings_window = mock.Mock()

        ui.edit_settings(object(), mock.Mock(), activation_time=1234)

        present.assert_called_once_with(ui._settings_window, 1234)

    @mock.patch("app.ui.present_from_tray")
    def test_pairing_click_reuses_and_raises_existing_window(self, present):
        async def check():
            ui = DesktopUI.__new__(DesktopUI)
            ui._pairing_window = mock.Mock()
            result = await ui.choose_device(mock.AsyncMock(), [], activation_time=4321)
            self.assertIsNone(result)
            present.assert_called_once_with(ui._pairing_window, 4321)

        asyncio.run(check())

    def test_firmware_update_retains_tray_activation_time(self):
        ui = DesktopUI.__new__(DesktopUI)
        ui._pending_window_activation_time = 0
        ui.on_update = mock.Mock()

        ui._request_firmware_update("ABCD", 9876)

        self.assertEqual(ui._pending_window_activation_time, 9876)
        ui.on_update.assert_called_once_with("ABCD")

    def test_tray_menu_preserves_global_controls_and_opens_device_window(self):
        ui = DesktopUI.__new__(DesktopUI)
        ui._paired_ids = {"ABCD"}
        ui._connections = {
            "address": ConnectedXCDevice(name="XC-ABCD", device_id="ABCD")
        }
        ui._device_info = {
            "ABCD": {"firmware_version": "0.2.0", "update_available": True}
        }
        ui._latest_firmware = "0.2.1"
        ui._firmware_checking = False
        ui._updating_device = None
        ui._has_recoverable_input = True
        ui._theme_colors = {}
        ui._overlay_positions = {}
        ui._config = config.AppConfig(paired_device_ids=["ABCD"])
        ui.on_check_updates = None
        ui._codex = "Idle"
        ui._relay = "Connected"
        for callback in (
            "on_set_relay",
            "on_set_output",
            "on_set_auto_enter",
            "on_set_interaction",
            "on_pair",
            "on_restore",
            "on_settings",
            "on_website",
            "on_quit",
        ):
            setattr(ui, callback, lambda *_: None)
        ui._show_device_window = mock.Mock()
        ui.call = mock.Mock()
        menu = ui._menu(native_indicator)
        items = [item for item in menu.items if isinstance(item, NativeMenuItem)]
        labels = [
            item.text(item) if callable(item.text) else item.text for item in items
        ]
        self.assertEqual(
            labels,
            [
                "Device: XC-ABCD connected",
                "Target: Focused App",
                "Codex: Idle",
                "Relay: Connected",
                "XC-ABCD",
                "Relay Mode",
                "Interaction",
                "Output",
                "Press Return After Paste",
                "Restore Last Input",
                "Pair Device...",
                "Settings...",
                "Quit",
            ],
        )
        for label in ("Relay Mode", "Output", "Interaction"):
            item = next(item for item in items if item.text == label)
            self.assertIsInstance(item.action, NativeMenu)
        device_item = next(item for item in items if item.text == "XC-ABCD")
        self.assertNotIsInstance(device_item.action, NativeMenu)
        device_index = menu.items.index(device_item)
        self.assertIs(menu.items[device_index + 1], NativeMenu.SEPARATOR)
        self.assertEqual(menu.items[device_index + 2].text, "Relay Mode")
        indicator = mock.Mock(activation_time=1234)
        device_item.action(indicator, device_item)
        ui.call.assert_called_once_with(ui._show_device_window, "ABCD", 1234)

    def test_connected_device_name_and_paired_order_follow_macos(self):
        ui = DesktopUI.__new__(DesktopUI)
        ui._config = config.AppConfig(
            paired_device_ids=["BCDE", "ABCD"], relay_mode="disabled"
        )
        ui._paired_ids = {"BCDE", "ABCD"}
        ui._device_info = {}
        ui._connections = {
            "first": ConnectedXCDevice(name="XC Custom", device_id="ABCD"),
        }

        self.assertEqual(ui._device_ids(), ("ABCD", "BCDE"))
        self.assertEqual(ui._device_name("ABCD"), "XC Custom")
        self.assertEqual(ui._device_name("CDEF"), "XC-CDEF")
        self.assertEqual(ui._device_summary(), "Device: XC Custom connected")

    def test_status_notifier_title_exposes_accessible_state(self):
        indicator = native_indicator.NativeIndicator(
            "xc-buddy", "XC Buddy", NativeMenu()
        )
        indicator._status_item = mock.Mock()

        indicator.set_state("processing", "Processing", "Processing")

        self.assertEqual(indicator.title, "XC Buddy — Processing")
        indicator._status_item.update.assert_called_once_with(
            "XC Buddy — Processing",
            "xc-buddy-processing-symbolic",
            "Active",
        )

    def test_sender_mode_marks_bluetooth_paused_in_all_device_surfaces(self):
        ui = DesktopUI.__new__(DesktopUI)
        ui._config = config.AppConfig(paired_device_ids=["ABCD"], relay_mode="sender")
        # Sender mode stops BLE.  Keep a stale connection here to make sure
        # the display does not briefly claim that a scan is still active.
        ui._connections = {
            "address": ConnectedXCDevice(name="XC-ABCD", device_id="ABCD")
        }
        ui._paired_ids = {"ABCD"}
        ui._relay = "Connected"

        self.assertEqual(ui._device_summary(), "Device: Bluetooth paused")
        self.assertEqual(ui._device_state("ABCD"), "Bluetooth paused")
        self.assertEqual(ui._relay_summary(), "Relay: Connected as a sender")

    def test_receiver_mode_identifies_the_relay_role(self):
        ui = DesktopUI.__new__(DesktopUI)
        ui._config = config.AppConfig(relay_mode="receiver")
        ui._relay = "Connected"

        self.assertEqual(ui._relay_summary(), "Relay: Connected as a receiver")

    def test_relay_error_is_not_given_a_redundant_role_suffix(self):
        ui = DesktopUI.__new__(DesktopUI)
        ui._config = config.AppConfig(relay_mode="receiver")
        ui._relay = "Receiver token is not set"

        self.assertEqual(ui._relay_summary(), "Relay: Receiver token is not set")

    def test_device_window_is_reused_for_the_same_device(self):
        ui = DesktopUI.__new__(DesktopUI)
        ui._paired_ids = {"ABCD"}
        ui._connections = {}
        ui._device_info = {}
        ui._device_windows = {}
        ui._gtk_windows = []
        device_window = mock.Mock()
        device_window.window = mock.Mock()

        with mock.patch(
            "app.gtk_device.DeviceWindow", return_value=device_window
        ) as window_class:
            ui._show_device_window("ABCD", 1234)
            ui._show_device_window("ABCD", 5678)

        window_class.assert_called_once_with(
            ui,
            "ABCD",
            THEME_NAMES,
            POSITION_NAMES,
            TRANSLATION_TARGETS,
        )
        device_window.window.show_all.assert_called_once_with()
        self.assertEqual(
            device_window.present.call_args_list,
            [mock.call(1234), mock.call(5678)],
        )

    def test_status_notifier_rejects_a_third_menu_level(self):
        menu = NativeMenu(
            NativeMenuItem(
                "XC-ABCD",
                NativeMenu(
                    NativeMenuItem(
                        "Overlay Color",
                        NativeMenu(NativeMenuItem("White", None)),
                    )
                ),
            )
        )
        service = _DBusMenu(lambda node, timestamp: node.action(None, node.item))
        with self.assertRaisesRegex(ValueError, "at most one submenu level"):
            service.set_menu(menu)

    def test_status_notifier_keeps_menu_action_ids_stable_across_refreshes(self):
        selected: list[str] = []
        service = _DBusMenu(lambda node, _timestamp: node.action(None, node.item))
        service.set_menu(
            NativeMenu(
                NativeMenuItem("Device: XC-ABCD connected", None, enabled=False),
                NativeMenuItem(
                    "Relay Mode",
                    NativeMenu(
                        NativeMenuItem(
                            "Sender", lambda _icon, _item: selected.append("old relay")
                        )
                    ),
                ),
                NativeMenuItem(
                    "XC-ABCD", lambda _icon, _item: selected.append("old device")
                ),
            )
        )
        initial = service._layout(0, -1, [])
        initial_nodes = [child.value for child in initial[2]]
        relay = next(
            node for node in initial_nodes if node[1]["label"].value == "Relay Mode"
        )
        sender_id = relay[2][0].value[0]
        device_id = next(
            node[0] for node in initial_nodes if node[1]["label"].value == "XC-ABCD"
        )

        service.set_menu(
            NativeMenu(
                NativeMenuItem("Device: XC-ABCD scanning", None, enabled=False),
                NativeMenuItem(
                    "Relay Mode",
                    NativeMenu(
                        NativeMenuItem(
                            "Sender", lambda _icon, _item: selected.append("new relay")
                        )
                    ),
                ),
                NativeMenuItem("Restore Last Input", None),
                NativeMenuItem(
                    "XC-ABCD", lambda _icon, _item: selected.append("new device")
                ),
            )
        )

        service._handle_event(sender_id, "clicked", 1234)
        service._handle_event(device_id, "clicked", 1234)
        self.assertEqual(selected, ["new relay", "new device"])

    def test_status_notifier_item_advertises_a_shell_owned_menu(self):
        item = _StatusNotifierItem(
            "xc-buddy", "XC Buddy", "xc-buddy-pair-symbolic", "/tmp/icons"
        )
        interface = item.introspect()

        self.assertEqual(interface.name, "org.kde.StatusNotifierItem")
        properties = {
            property.name: property.signature for property in interface.properties
        }
        self.assertEqual(properties["Menu"], "o")
        self.assertEqual(properties["ItemIsMenu"], "b")
        self.assertEqual(properties["IconThemePath"], "s")
        self.assertEqual(properties["IconName"], "s")
        self.assertTrue(
            {"Activate", "ContextMenu", "SecondaryActivate", "Scroll"}
            <= {method.name for method in interface.methods}
        )
        self.assertTrue(
            {"NewIcon", "NewStatus", "NewMenu"}
            <= {signal.name for signal in interface.signals}
        )


class NativeIndicatorStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_is_one_shot_and_reports_connection(self):
        indicator = native_indicator.NativeIndicator(
            "xc-buddy", "XC Buddy", NativeMenu()
        )
        watcher = mock.Mock()
        watcher.call_register_status_notifier_item = mock.AsyncMock()
        indicator.on_connection_changed = mock.Mock()
        proxy = mock.Mock()
        proxy.get_interface.return_value = watcher
        bus = mock.Mock()
        bus.introspect = mock.AsyncMock(return_value=object())
        bus.get_proxy_object.return_value = proxy
        indicator._bus = bus

        await indicator._register_with_watcher()

        bus.introspect.assert_awaited_once_with(
            native_indicator.WATCHER_BUS_NAME,
            native_indicator.WATCHER_OBJECT_PATH,
        )
        watcher.call_register_status_notifier_item.assert_awaited_once_with(
            indicator._bus_name
        )
        self.assertIs(indicator._watcher, watcher)
        self.assertTrue(indicator._registered)
        indicator.on_connection_changed.assert_called_once_with(True)

    async def test_registration_failure_does_not_poll(self):
        indicator = native_indicator.NativeIndicator(
            "xc-buddy", "XC Buddy", NativeMenu()
        )
        bus = mock.Mock()
        bus.introspect = mock.AsyncMock(side_effect=OSError("watcher unavailable"))
        indicator._bus = bus

        with self.assertRaisesRegex(
            RuntimeError, "StatusNotifier watcher unavailable"
        ) as caught:
            await indicator._register_with_watcher()

        self.assertEqual(bus.introspect.await_count, 1)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertIsNone(indicator._watcher)
        self.assertFalse(indicator._registered)
        self.assertEqual(indicator.registration_error, str(caught.exception))

    async def test_watcher_owner_changes_drive_disconnect_and_registration(self):
        indicator = native_indicator.NativeIndicator(
            "xc-buddy", "XC Buddy", NativeMenu()
        )
        indicator._bus = mock.Mock()
        indicator._loop = asyncio.get_running_loop()
        indicator._registered = True
        indicator.on_connection_changed = mock.Mock()
        indicator._register_with_watcher = mock.AsyncMock()

        indicator._name_owner_changed(native_indicator.WATCHER_BUS_NAME, ":1.20", "")
        self.assertFalse(indicator._registered)
        indicator.on_connection_changed.assert_called_once_with(False)

        indicator._name_owner_changed(native_indicator.WATCHER_BUS_NAME, "", ":1.21")
        await indicator._registration_task

        indicator._register_with_watcher.assert_awaited_once_with()


@unittest.skipUnless(
    os.environ.get("DBUS_SESSION_BUS_ADDRESS"), "requires a D-Bus session"
)
class StatusNotifierProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real D-Bus serialization without registering a panel icon."""

    async def asyncSetUp(self):
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType, RequestNameReply

        self._server = await MessageBus(bus_type=BusType.SESSION).connect()
        self._client = await MessageBus(bus_type=BusType.SESSION).connect()
        self._service_name = f"io.github.xcbuddy.ProtocolTest{os.getpid()}"
        reply = await self._server.request_name(self._service_name)
        self.assertEqual(reply, RequestNameReply.PRIMARY_OWNER)
        self._selected: list[str] = []
        self._menu_service = _DBusMenu(
            lambda node, _timestamp: node.action(None, node.item)
        )
        self._status_item = _StatusNotifierItem(
            "xc-buddy",
            "XC Buddy",
            "xc-buddy-pair-symbolic",
            "/tmp/xc-buddy-icons",
        )
        self._server.export("/MenuBar", self._menu_service)
        self._server.export("/StatusNotifierItem", self._status_item)

    async def asyncTearDown(self):
        self._server.unexport("/MenuBar")
        self._server.unexport("/StatusNotifierItem")
        self._client.disconnect()
        self._server.disconnect()

    async def test_one_level_menu_and_action_round_trip_over_dbus(self):
        from dbus_fast import Variant

        self._menu_service.set_menu(
            NativeMenu(
                NativeMenuItem(
                    "Relay Mode",
                    NativeMenu(
                        NativeMenuItem(
                            "Sender",
                            lambda _icon, _item: self._selected.append("sender"),
                        )
                    ),
                ),
                NativeMenuItem(
                    "XC-ABCD", lambda _icon, _item: self._selected.append("device")
                ),
            )
        )
        node = await self._client.introspect(self._service_name, "/MenuBar")
        proxy = self._client.get_proxy_object(self._service_name, "/MenuBar", node)
        menu = proxy.get_interface("com.canonical.dbusmenu")

        _revision, root = await menu.call_get_layout(0, -1, [])
        relay, device = (child.value for child in root[2])
        self.assertEqual(relay[1]["label"].value, "Relay Mode")
        self.assertEqual(relay[1]["children-display"].value, "submenu")
        sender = relay[2][0].value
        self.assertEqual(sender[1]["label"].value, "Sender")
        self.assertEqual(sender[2], [])
        self.assertEqual(device[1]["label"].value, "XC-ABCD")
        self.assertEqual(device[2], [])

        await menu.call_event(sender[0], "clicked", Variant("s", ""), 4321)
        await menu.call_event(device[0], "clicked", Variant("s", ""), 4321)
        self.assertEqual(self._selected, ["sender", "device"])

    async def test_radio_selection_update_is_signaled_over_dbus(self):
        self._menu_service.set_menu(
            NativeMenu(
                NativeMenuItem(
                    "Relay Mode",
                    NativeMenu(
                        NativeMenuItem("Disabled", None, checked=False, radio=True),
                        NativeMenuItem("Sender", None, checked=True, radio=True),
                    ),
                )
            )
        )
        node = await self._client.introspect(self._service_name, "/MenuBar")
        proxy = self._client.get_proxy_object(self._service_name, "/MenuBar", node)
        menu = proxy.get_interface("com.canonical.dbusmenu")
        _revision, root = await menu.call_get_layout(0, -1, [])
        relay = root[2][0].value
        disabled, sender = (child.value for child in relay[2])
        updates: list[tuple] = []
        updated = asyncio.Event()

        def properties_updated(changed, removed) -> None:
            updates.append((changed, removed))
            updated.set()

        menu.on_items_properties_updated(properties_updated)
        self._menu_service.set_menu(
            NativeMenu(
                NativeMenuItem(
                    "Relay Mode",
                    NativeMenu(
                        NativeMenuItem("Disabled", None, checked=True, radio=True),
                        NativeMenuItem("Sender", None, checked=False, radio=True),
                    ),
                )
            )
        )
        await asyncio.wait_for(updated.wait(), 1)

        changed, removed = updates[0]
        state_changes = {
            node_id: properties["toggle-state"].value
            for node_id, properties in changed
            if "toggle-state" in properties
        }
        self.assertEqual(state_changes, {disabled[0]: 1, sender[0]: 0})
        self.assertEqual(removed, [])

    async def test_status_item_properties_round_trip_over_dbus(self):
        node = await self._client.introspect(self._service_name, "/StatusNotifierItem")
        proxy = self._client.get_proxy_object(
            self._service_name, "/StatusNotifierItem", node
        )
        properties = proxy.get_interface("org.freedesktop.DBus.Properties")

        menu_path = await properties.call_get("org.kde.StatusNotifierItem", "Menu")
        icon_name = await properties.call_get("org.kde.StatusNotifierItem", "IconName")
        is_menu = await properties.call_get("org.kde.StatusNotifierItem", "ItemIsMenu")
        self.assertEqual(menu_path.value, "/MenuBar")
        self.assertEqual(icon_name.value, "xc-buddy-pair-symbolic")
        self.assertTrue(is_menu.value)


class FakeUI:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.events.append((name, args))


class FakeBLE:
    def __init__(self):
        self.sent = []
        self.control_state = protocol.ui_state("ready")
        self.paired_ids = set()
        self.clients = {}
        self.devices = {}

    async def send(self, data, address=None, remember=False):
        self.sent.append((json.loads(data), address, remember))
        if remember:
            self.control_state = data

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeRelay:
    def __init__(self):
        self.sender_state = None
        self.started = []
        self.published = []

    async def stop(self):
        pass

    def retain_sender_state(self, state):
        self.sender_state = state

    def publish(self, event):
        self.published.append(event)

    async def start(self, mode, endpoint, token):
        self.started.append((mode, endpoint, token))


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def test_debug_audio_write_failure_does_not_break_recognition(self):
        settings = config.AppConfig(debug_audio_cache=True)
        coordinator = Coordinator(settings, FakeUI())
        cycle = Cycle("a", "ABCD", 7)
        cycle.audio.extend(b"ogg")
        with self.assertLogs("app.coordinator", "WARNING"), mock.patch.object(
            Path, "mkdir", side_effect=PermissionError
        ):
            coordinator._save_debug(cycle)

    async def test_transcription_overlay_begins_with_the_asr_request(self):
        ui = FakeUI()
        coordinator = Coordinator(config.AppConfig(), ui)
        coordinator.ble = FakeBLE()
        cycle = Cycle("a", "ABCD", 7)
        cycle.started -= 1
        cycle.frames = 1
        cycle.eos = True
        cycle.audio.extend(b"audio")
        coordinator.main_cycle = cycle

        with mock.patch(
            "app.coordinator.asyncio.to_thread",
            new=mock.AsyncMock(return_value="hello"),
        ):
            await coordinator._transcribe(cycle)

        events = [event for event, _args in ui.events]
        self.assertLess(events.index("processing"), events.index("final"))
        self.assertIn(("processing", ("ABCD", "Transcribing...")), ui.events)

    async def test_sender_retains_local_state_before_connecting(self):
        settings = config.AppConfig(
            relay_mode="sender",
            relay_url="wss://relay.example/ws",
            relay_sender_token="secret",
        )
        coordinator = Coordinator(settings, FakeUI())
        coordinator.ble = FakeBLE()
        coordinator.bridge.start = mock.AsyncMock()
        coordinator.bridge.stop = mock.AsyncMock()
        coordinator.relay = FakeRelay()
        coordinator.codex_state = "working"
        await coordinator.apply_relay_mode()
        self.assertEqual(coordinator.relay.sender_state, "working")
        self.assertEqual(
            coordinator.relay.started,
            [("sender", "wss://relay.example/ws", "secret")],
        )

    async def test_concurrent_subtitle_cycles_and_short_press_cancel(self):
        settings = config.AppConfig()
        settings.output.target = "subtitle"
        ui = FakeUI()
        coordinator = Coordinator(settings, ui)
        coordinator.ble = FakeBLE()
        coordinator._primary_down("a", "AAAA", 1)
        coordinator._primary_down("b", "BBBB", 2)
        await asyncio.sleep(0)
        self.assertEqual(set(coordinator.active_subtitles), {"a", "b"})
        coordinator._primary_up("a")
        await asyncio.sleep(0)
        self.assertNotIn("a", coordinator.active_subtitles)
        self.assertIn("b", coordinator.active_subtitles)
        coordinator._cancel_subtitle("b", 2)
        await asyncio.sleep(0)

    async def test_confirmation_first_press_pauses_second_commits(self):
        coordinator = Coordinator(config.AppConfig(), FakeUI())
        coordinator.ble = FakeBLE()
        coordinator.pending_text = coordinator.last_text = "hello"
        coordinator.last_address = "a"
        coordinator.main_cycle = mock.Mock(address="a")
        coordinator.commit_pending = mock.AsyncMock()
        self.assertTrue(coordinator._pending_primary("a"))
        self.assertTrue(coordinator.pending_paused)
        self.assertTrue(coordinator._pending_primary("a"))
        await asyncio.sleep(0)
        coordinator.commit_pending.assert_awaited_once()

    async def test_codex_done_is_live_only_and_reconnects_ready(self):
        coordinator = Coordinator(config.AppConfig(), FakeUI())
        coordinator.ble = FakeBLE()
        coordinator.ble.control_state = protocol.ui_state(
            "codex_working", "Codex is working"
        )
        coordinator.on_codex_event("done")
        await asyncio.sleep(0)
        self.assertEqual(coordinator.ble.sent[-1][0]["state"], "codex_done")
        self.assertFalse(coordinator.ble.sent[-1][2])
        self.assertEqual(json.loads(coordinator.ble.control_state)["state"], "ready")
        self.assertNotIn("background_state", json.loads(coordinator.ble.control_state))

    async def test_ready_retains_codex_as_background_state(self):
        coordinator = Coordinator(config.AppConfig(), FakeUI())
        coordinator.ble = FakeBLE()
        coordinator.codex_state = "working"
        await coordinator._send_ready("a")
        self.assertEqual(
            coordinator.ble.sent[-1],
            (
                {
                    "event": "ui_state",
                    "state": "ready",
                    "text": "",
                    "background_state": "codex_working",
                },
                "a",
                True,
            ),
        )

    async def test_sender_error_status_matches_local_sender_state(self):
        settings = config.AppConfig(relay_mode="sender")
        ui = FakeUI()
        coordinator = Coordinator(settings, ui)
        coordinator.relay = FakeRelay()
        coordinator.on_codex_event("error", "bad")
        self.assertEqual(coordinator.codex_state, "idle")
        self.assertIn(("codex_status", ("Error",)), ui.events)
        self.assertIn(("status", ("Codex error",)), ui.events)

    async def test_subtitle_duplicate_and_preempt_keep_existing_cycles(self):
        settings = config.AppConfig()
        settings.output.target = "subtitle"
        coordinator = Coordinator(settings, FakeUI())
        coordinator.ble = FakeBLE()
        coordinator._primary_down("a", "AAAA", 1)
        first = coordinator.subtitle_cycles[("a", 1)]
        coordinator._primary_down("a", "AAAA", 1)
        self.assertIs(coordinator.subtitle_cycles[("a", 1)], first)
        coordinator._primary_down("a", "AAAA", 2)
        self.assertIs(coordinator.subtitle_cycles[("a", 1)], first)
        self.assertEqual(coordinator.active_subtitles["a"], 2)
        await asyncio.sleep(0)

    async def test_older_subtitle_completion_does_not_hide_new_recording(self):
        settings = config.AppConfig()
        settings.output.target = "subtitle"
        ui = FakeUI()
        coordinator = Coordinator(settings, ui)
        coordinator.ble = FakeBLE()
        coordinator._primary_down("a", "AAAA", 1)
        first = coordinator.subtitle_cycles[("a", 1)]
        coordinator._primary_down("a", "AAAA", 2)
        await asyncio.sleep(0)
        ui.events.clear()
        coordinator.ble.sent.clear()
        await coordinator._finish_subtitle_cycle(first, "older text")
        self.assertNotIn(("hide_overlay", ("AAAA",)), ui.events)
        self.assertFalse(coordinator.ble.sent)
        self.assertEqual(coordinator.active_subtitles["a"], 2)

    async def test_stop_cancels_active_recognition_and_hides_ui(self):
        ui = FakeUI()
        coordinator = Coordinator(config.AppConfig(), ui)
        coordinator.ble = FakeBLE()
        cycle = coordinator.main_cycle = mock.Mock()
        cycle.final_task = asyncio.create_task(asyncio.sleep(60))
        coordinator.subtitle_cycles = {}
        coordinator.bridge.stop = mock.AsyncMock()
        coordinator.relay.stop = mock.AsyncMock()
        await coordinator.stop()
        self.assertTrue(cycle.final_task.cancelled())
        self.assertIsNone(coordinator.main_cycle)
        self.assertIn(("hide_overlay", ()), ui.events)
        self.assertIn(("hide_subtitles", ()), ui.events)

    async def test_config_update_cancels_recognition_without_restarting_subsystems(
        self,
    ):
        settings = config.AppConfig(paired_device_ids=["ABCD"])
        ui = FakeUI()
        coordinator = Coordinator(settings, ui)
        ble = FakeBLE()
        ble.paired_ids = {"ABCD"}
        ble.start = mock.AsyncMock()
        ble.stop = mock.AsyncMock()
        coordinator.ble = ble
        coordinator.relay = FakeRelay()
        old_bridge = mock.Mock()
        old_bridge.stop = mock.AsyncMock()
        coordinator.bridge = old_bridge
        cycle = coordinator.main_cycle = mock.Mock(address="a", device_id="ABCD")
        cycle.final_task = asyncio.create_task(asyncio.sleep(60))
        with mock.patch("app.coordinator.CodexBridge") as bridge_type:
            await coordinator.update_config(settings)
        await asyncio.sleep(0)
        self.assertTrue(cycle.final_task.cancelled())
        ble.stop.assert_not_awaited()
        ble.start.assert_not_awaited()
        old_bridge.stop.assert_not_awaited()
        bridge_type.assert_not_called()

    async def test_config_update_restarts_only_a_changed_bridge(self):
        settings = config.AppConfig(paired_device_ids=["ABCD"])
        coordinator = Coordinator(settings, FakeUI())
        coordinator.running = True
        ble = FakeBLE()
        ble.paired_ids = {"ABCD"}
        ble.stop = mock.AsyncMock()
        coordinator.ble = ble
        coordinator.relay = FakeRelay()
        old_bridge = coordinator.bridge = mock.Mock()
        old_bridge.stop = mock.AsyncMock()
        changed = dataclasses.replace(settings, codex_bridge_port=17322)
        new_bridge = mock.Mock()
        new_bridge.start = mock.AsyncMock()
        new_bridge.stop = mock.AsyncMock()
        with mock.patch("app.coordinator.CodexBridge", return_value=new_bridge):
            await coordinator.update_config(changed)
        old_bridge.stop.assert_awaited_once()
        new_bridge.start.assert_awaited_once()
        ble.stop.assert_not_awaited()


class FakeGattClient:
    mtu_size = 247

    def __init__(self, manager, complete=True):
        self.manager = manager
        self.complete = complete
        self.writes = []

    async def write_gatt_char(self, _uuid, data, response=False):
        self.writes.append((bytes(data), response))
        if data[1] == 0x22 and self.complete:
            transfer_id = struct.unpack_from("<I", data, 4)[0]
            body = json.dumps({"event": "done", "transfer_id": transfer_id}).encode()
            self.manager._ota("a", struct.pack("<BBH", 1, 0x30, len(body)) + body)


class BluetoothTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        return BluetoothManager([], lambda *_: None, lambda *_: None, lambda *_: None)

    def test_advertised_device_name_requires_macos_prefix(self):
        self.assertEqual(BluetoothManager.device_id("XC-a1b2 extra"), "A1B2")
        self.assertEqual(BluetoothManager.device_id("VS-A1B2"), "A1B2")
        self.assertEqual(BluetoothManager.device_id("A1B2"), "")

    async def test_existing_connection_blocks_another_connect(self):
        manager = BluetoothManager(
            ["ABCD", "1234"], lambda *_: None, lambda *_: None, lambda *_: None
        )
        existing_client = mock.Mock()
        manager.clients = {"address": existing_client}
        manager.devices = {"address": "ABCD"}
        manager.device_names = {"address": "XC-ABCD"}
        client_type = mock.Mock(
            side_effect=AssertionError("a second BLE client must not be created")
        )

        await manager._connect(client_type, object(), "second", "1234", "XC-1234")

        client_type.assert_not_called()
        self.assertEqual(manager.clients, {"address": existing_client})
        self.assertEqual(manager.devices, {"address": "ABCD"})

    async def test_scan_pauses_while_a_device_is_connected(self):
        manager = BluetoothManager(
            ["ABCD", "1234"], lambda *_: None, lambda *_: None, lambda *_: None
        )
        manager.clients = {"address": mock.Mock()}
        manager.devices = {"address": "ABCD"}

        with mock.patch(
            "bleak.BleakScanner.discover", new_callable=mock.AsyncMock
        ) as discover:
            task = asyncio.create_task(manager._scan_loop())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        discover.assert_not_awaited()

    def test_connected_devices_preserve_advertised_names(self):
        manager = self.manager()
        manager.devices = {"address": "ABCD"}
        manager.device_names = {"address": "VS-ABCD"}

        self.assertEqual(
            manager.connected_devices,
            {"address": ConnectedXCDevice(name="VS-ABCD", device_id="ABCD")},
        )

    async def test_sleep_disconnects_and_wake_restarts_discovery(self):
        connections = []
        manager = BluetoothManager(
            ["ABCD"], lambda *_: None, lambda *_: None, connections.append
        )
        client = mock.Mock()
        client.write_gatt_char = mock.AsyncMock()
        client.disconnect = mock.AsyncMock()
        manager.clients = {"address": client}
        manager.devices = {"address": "ABCD"}
        manager._running = True

        scan_started = asyncio.Event()

        async def scan_until_cancelled():
            scan_started.set()
            await asyncio.Event().wait()

        manager._scan_loop = scan_until_cancelled
        manager._ensure_scan_loop()
        await scan_started.wait()
        await manager._handle_sleep(True)

        client.write_gatt_char.assert_awaited_once_with(
            protocol.CONTROL_UUID, protocol.ui_state("ready"), response=False
        )
        client.disconnect.assert_awaited_once()
        self.assertIsNone(manager.task)
        self.assertEqual(manager.clients, {})
        self.assertEqual(manager.devices, {})
        self.assertEqual(connections[-1], {})

        scan_started.clear()
        await manager._handle_sleep(False)
        await scan_started.wait()
        self.assertIsNotNone(manager.task)
        manager._running = False
        await manager._cancel_scan_loop()

    async def test_failed_notification_setup_is_removed_for_retry(self):
        manager = self.manager()

        class Client:
            def __init__(self, _device, disconnected_callback):
                self.disconnected_callback = disconnected_callback
                self.disconnected = False

            async def connect(self):
                return None

            async def start_notify(self, _uuid, _callback):
                raise RuntimeError("notification setup failed")

            async def disconnect(self):
                self.disconnected = True

        with self.assertRaisesRegex(RuntimeError, "notification setup failed"):
            await manager._connect(Client, object(), "a", "ABCD", "VS-ABCD")
        self.assertEqual(manager.clients, {})
        self.assertEqual(manager.devices, {})
        self.assertEqual(manager.device_names, {})

    async def test_ota_waits_for_device_done(self):
        manager = self.manager()
        client = FakeGattClient(manager)
        manager.clients["a"] = client
        manager.devices["a"] = "ABCD"
        progress = []
        await manager.update_firmware(
            "ABCD", b"firmware", lambda *value: progress.append(value)
        )
        self.assertEqual([write[0][1] for write in client.writes], [0x20, 0x21, 0x22])
        self.assertEqual(progress[-1], (8, 8, True))
        self.assertIn((8, 8, False), progress)

    async def test_ota_cancellation_sends_abort(self):
        manager = self.manager()
        client = FakeGattClient(manager, complete=False)
        manager.clients["a"] = client
        manager.devices["a"] = "ABCD"
        task = asyncio.create_task(
            manager.update_firmware("ABCD", b"firmware", lambda *_: None)
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client.writes[-1][0][1], 0x23)

    async def test_ota_user_visible_errors_match_macos(self):
        manager = self.manager()
        with self.assertRaisesRegex(
            ValueError, "Firmware image is larger than the OTA partition"
        ):
            await manager.update_firmware(
                "ABCD", b"x" * (3 * 1024 * 1024 + 1), mock.Mock()
            )
        with self.assertRaisesRegex(RuntimeError, "No XC device is connected"):
            await manager.update_firmware("ABCD", b"firmware", mock.Mock())

        future = asyncio.get_running_loop().create_future()
        manager.ota_waiters["a"] = (future, 7, mock.Mock())
        payload = json.dumps(
            {"event": "error", "transfer_id": 7, "code": "bad_digest"}
        ).encode()
        manager._ota("a", struct.pack("<BBH", 1, 0x30, len(payload)) + payload)
        with self.assertRaisesRegex(RuntimeError, "Device rejected OTA: bad_digest"):
            await future

    async def test_reconnect_replays_each_devices_authoritative_state(self):
        manager = self.manager()
        first_state = protocol.ui_state("recording")
        second_state = protocol.ui_state("approval_needed", "Approval needed")
        await manager.send(first_state, "a", remember=True)
        await manager.send(second_state, "b", remember=True)
        first, second = mock.Mock(), mock.Mock()
        first.write_gatt_char = mock.AsyncMock()
        second.write_gatt_char = mock.AsyncMock()
        await manager._initialize(first, "a")
        await manager._initialize(second, "b")
        self.assertEqual(first.write_gatt_char.call_args.args[1], first_state)
        self.assertEqual(second.write_gatt_char.call_args.args[1], second_state)

    async def test_broadcast_authoritative_state_updates_all_known_devices(self):
        manager = self.manager()
        manager.control_states = {
            "a": protocol.ui_state("recording"),
            "b": protocol.ui_state("thinking"),
        }
        ready = protocol.ui_state("ready", "", "ready")
        await manager.send(ready, remember=True)
        self.assertEqual(manager.control_states, {"a": ready, "b": ready})


if __name__ == "__main__":
    unittest.main()
