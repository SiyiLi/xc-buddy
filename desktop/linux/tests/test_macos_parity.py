import re
import unittest
from pathlib import Path

from app import protocol
from app.config import AppConfig
from app.coordinator import (
    APPROVAL_TIMEOUT,
    AUDIO_END_TIMEOUT,
    FIRMWARE_RELEASE_CACHE_DURATION,
    MINIMUM_RECORDING_DURATION,
)
from app.ui import POSITION_NAMES, THEME_NAMES, TRANSLATION_TARGETS


MACOS = Path(__file__).resolve().parents[2] / "macos" / "Sources" / "XCBuddy"


@unittest.skipUnless(MACOS.exists(), "macOS source tree is not present")
class MacOSSourceParityTests(unittest.TestCase):
    def test_every_persisted_config_field_has_a_linux_equivalent(self):
        source = (MACOS / "AppConfig.swift").read_text()
        app_config_block = source.split("struct AppConfig {", 1)[1].split(
            "static var configDirectory", 1
        )[0]
        swift_fields = set(re.findall(r"^    var (\w+):", app_config_block, re.M))
        equivalents = {
            "openAIBaseURL": "openai_base_url",
            "openAIAPIKey": "openai_api_key",
            "openAIModel": "openai_model",
            "openAILanguage": "openai_language",
            "openAIPrompt": "openai_prompt",
            "llmBaseURL": "llm_base_url",
            "llmAPIKey": "llm_api_key",
            "llmModel": "llm_model",
            "interactionMode": "interaction_mode",
            "asrHotwords": "asr_hotwords",
            "pairedDeviceIDs": "paired_device_ids",
            "deviceThemeColors": "device_theme_colors",
            "deviceOverlayPositions": "device_overlay_positions",
            "defaultOutputProfile": "output",
            "deviceOutputProfiles": "device_outputs",
            "autoEnter": "auto_enter",
            "debugAudioCache": "debug_audio_cache",
            "debugAudioDirectory": "debug_audio_dir",
            "codexBridgePort": "codex_bridge_port",
            "codexBridgeToken": "codex_bridge_token",
            "codexSuccessChime": "codex_success_chime",
            "relayMode": "relay_mode",
            "relayURL": "relay_url",
            "relaySenderToken": "relay_sender_token",
            "relayReceiverToken": "relay_receiver_token",
            "devicePowerTimers": "power_timers",
        }
        self.assertEqual(swift_fields, set(equivalents))
        self.assertEqual(set(equivalents.values()), set(AppConfig.__dataclass_fields__))

    def test_protocol_uuids_match_the_macos_source(self):
        source = (MACOS / "BleProtocol.swift").read_text()
        swift_uuids = {
            name: value.lower()
            for name, value in re.findall(
                r"static let (\w+UUID) = \"([0-9A-F-]+)\"", source
            )
        }
        self.assertEqual(
            swift_uuids,
            {
                "serviceUUID": protocol.SERVICE_UUID,
                "audioUUID": protocol.AUDIO_UUID,
                "stateUUID": protocol.STATE_UUID,
                "controlUUID": protocol.CONTROL_UUID,
                "otaRXUUID": protocol.OTA_RX_UUID,
                "otaStateUUID": protocol.OTA_STATE_UUID,
            },
        )

    def test_user_visible_enum_choices_match(self):
        config_source = (MACOS / "AppConfig.swift").read_text()
        self.assertEqual(
            set(THEME_NAMES),
            set(
                re.findall(
                    r"case (white|pink|green|yellow|blue|purple)(?:\s|$)",
                    config_source,
                )
            ),
        )
        self.assertEqual(
            set(POSITION_NAMES),
            {"center", "top_left", "top_right", "bottom_left", "bottom_right"},
        )

        status_source = (MACOS / "StatusController.swift").read_text()
        translation_block = (
            status_source.split("private static let translationTargets", 1)[1]
            .split("= [", 1)[1]
            .split("\n    ]", 1)[0]
        )
        self.assertEqual(
            TRANSLATION_TARGETS,
            tuple(re.findall(r'\("([^"]+)", "([^"]+)"\)', translation_block)),
        )

    def test_defaults_and_state_machine_timing_match(self):
        settings = AppConfig()
        self.assertEqual(
            settings.openai_base_url, "https://inference-api.nvidia.com/v1"
        )
        self.assertEqual(settings.openai_model, "gcp/google/gemini-3.6-flash")
        self.assertEqual(settings.llm_base_url, "https://api.openai.com/v1")
        self.assertEqual(settings.llm_model, "gpt-5.5")
        self.assertEqual(settings.interaction_mode, "hold_to_talk")
        self.assertEqual(settings.output.target, "focused_app")
        self.assertEqual(settings.output.transform, "original")
        self.assertEqual(settings.output.translation_target, "en")
        self.assertEqual(settings.codex_bridge_port, 17321)
        self.assertEqual(
            (
                settings.power_timers.display_dim_seconds,
                settings.power_timers.display_off_seconds,
                settings.power_timers.idle_deep_sleep_seconds,
                settings.power_timers.codex_deep_sleep_seconds,
            ),
            (30, 300, 300, 900),
        )

        coordinator_source = (MACOS / "XCBuddyCoordinator.swift").read_text()
        self.assertIn(
            "minimumRecordingDuration: TimeInterval = 0.5", coordinator_source
        )
        self.assertIn("audioEndTimeout: TimeInterval = 1.0", coordinator_source)
        self.assertIn("approvalTimeout: TimeInterval = 60.0", coordinator_source)
        self.assertIn(
            "firmwareReleaseCacheDuration: TimeInterval = 60 * 60",
            coordinator_source,
        )
        self.assertEqual(MINIMUM_RECORDING_DURATION, 0.5)
        self.assertEqual(AUDIO_END_TIMEOUT, 1.0)
        self.assertEqual(APPROVAL_TIMEOUT, 60.0)
        self.assertEqual(FIRMWARE_RELEASE_CACHE_DURATION, 3600)

    def test_menu_actions_and_translation_catalog_are_exposed_on_linux(self):
        linux_directory = Path(__file__).resolve().parents[1] / "app"
        linux_source = "\n".join(
            (linux_directory / name).read_text() for name in ("ui.py", "gtk_device.py")
        )
        mac_source = (MACOS / "StatusController.swift").read_text()
        for title in (
            "Relay Mode",
            "Output",
            "Press Return After Paste",
            "Interaction",
            "Restore Last Input",
            "Overlay Color",
            "Overlay Position",
            "Translation",
            "Forget This Device",
            "Pair",
            "Settings",
            "Website",
            "Quit",
        ):
            with self.subTest(title=title):
                self.assertIn(title, mac_source)
                self.assertIn(title, linux_source)

        mac_info = (MACOS / "Info.plist").read_text()
        self.assertIn("<key>SUPublicEDKey</key>\n\t<string></string>", mac_info)
        self.assertIn("Check for App Updates...", mac_source)
        self.assertIn("Check for App Updates...", linux_source)
        self.assertIn("if onCheckForUpdates != nil", mac_source)
        self.assertIn("if self.on_check_updates is not None", linux_source)

    def test_device_window_preserves_the_macos_device_menu_order(self):
        mac_source = (MACOS / "StatusController.swift").read_text()
        mac_device_menu = mac_source.split("private func addDeviceItems", 1)[1].split(
            "func setStatus", 1
        )[0]
        mac_device_assembly = mac_device_menu.split(
            "private func addThemeColorItems", 1
        )[0]
        linux = Path(__file__).resolve().parents[1] / "app"
        linux_device_window = (linux / "gtk_device.py").read_text()
        linux_device_status = "\n".join(
            ((linux / "ui.py").read_text(), linux_device_window)
        )
        labels = (
            "Connected",
            "Scanning",
            "Bluetooth paused",
            "Overlay Color",
            "Overlay Position",
            "Translation",
            "Firmware",
            "Forget This Device",
        )
        for label in labels:
            with self.subTest(label=label):
                self.assertIn(label, mac_device_menu)
                self.assertIn(label, linux_device_status)
        self.assertIn("self._ui._device_state", linux_device_window)
        mac_order = [
            mac_device_assembly.index(marker)
            for marker in (
                "addThemeColorItems",
                "addOverlayPositionItems",
                "addDeviceTextItems",
                "addFirmwareItems",
                "Forget This Device",
            )
        ]
        linux_order = [
            linux_device_window.index(marker)
            for marker in (
                "Overlay Color",
                "Overlay Position",
                "Translation",
                "self._firmware_status",
                "Forget This Device",
            )
        ]
        self.assertEqual(mac_order, sorted(mac_order))
        self.assertEqual(linux_order, sorted(linux_order))

    def test_relay_role_and_bluetooth_pause_status_match_in_both_desktops(self):
        mac_source = (MACOS / "StatusController.swift").read_text()
        linux = Path(__file__).resolve().parents[1] / "app"
        linux_menu = (linux / "ui.py").read_text()

        self.assertIn('deviceText = "Bluetooth paused"', mac_source)
        self.assertIn('return "Device: Bluetooth paused"', linux_menu)
        self.assertIn('stateTitle = "Bluetooth paused"', mac_source)
        self.assertIn('return "Bluetooth paused"', linux_menu)
        self.assertIn("as a \\(relayMode.displayName.lowercased())", mac_source)
        self.assertIn("as a {self._config.relay_mode}", linux_menu)
        self.assertIn(
            'onConnectionStatus?("\\(mode.displayName) token is not set")',
            (MACOS / "RelayClient.swift").read_text(),
        )
        self.assertIn(
            'f"{mode.capitalize()} token is not set"', (linux / "bridge.py").read_text()
        )

    def test_onboarding_layout_and_copy_match_macos(self):
        mac_source = (MACOS / "OnboardingWindowController.swift").read_text()
        linux_source = (
            Path(__file__).resolve().parents[1] / "app" / "gtk_onboarding.py"
        ).read_text()
        for source in (mac_source, linux_source):
            with self.subTest(source=source[:20]):
                for text in (
                    "Set Up XC Buddy",
                    "Pair Device",
                    "Transcription",
                    "Accessibility",
                    "Ready",
                    "Pair XC",
                    "Configure transcription",
                    "Enter the API key used to transcribe audio.",
                    "Allow text insertion",
                    "XC Buddy is ready",
                    "Back",
                    "Continue",
                    "Finish",
                    "Device",
                    "ID",
                    "RSSI",
                ):
                    self.assertIn(text, source)
        self.assertIn('summaryLine("Transcription", value: "Configured")', mac_source)
        self.assertIn('add_summary_line("Transcription", "Configured")', linux_source)
        self.assertIn("width: 680, height: 470", mac_source)
        self.assertIn("set_default_size(680, 470)", linux_source)
        self.assertIn("equalToConstant: 170", mac_source)
        self.assertIn("set_size_request(170, -1)", linux_source)
        for width in (210, 90, 70):
            self.assertIn(f"width: {width}", mac_source)
            self.assertRegex(linux_source, rf'\("[^"]+", \d, {width}\)')

    def test_onboarding_persists_only_on_finish_and_config_controls_launch(self):
        mac_app = (MACOS / "AppDelegate.swift").read_text()
        mac_onboarding = (MACOS / "OnboardingWindowController.swift").read_text()
        linux = Path(__file__).resolve().parents[1] / "app"
        linux_app = (linux / "app.py").read_text()
        linux_onboarding = (linux / "gtk_onboarding.py").read_text()
        linux_config = (linux / "config.py").read_text()

        self.assertIn("if AppConfig.configExists", mac_app)
        self.assertIn(
            "first_launch = not config_file.config_path().exists()", linux_app
        )
        self.assertEqual(mac_onboarding.count("try config.save()"), 1)
        self.assertEqual(linux_onboarding.count("save_config()"), 1)
        self.assertNotIn("onboarding_state", linux_app)
        self.assertNotIn("onboarding_state", linux_config)

    def test_settings_and_pairing_window_sizes_match_macos(self):
        linux = Path(__file__).resolve().parents[1] / "app"
        cases = (
            ("SettingsWindowController.swift", "gtk_settings.py", 560, 640),
            ("PairDeviceWindowController.swift", "gtk_pairing.py", 420, 280),
        )
        for mac_file, linux_file, width, height in cases:
            mac_source = (MACOS / mac_file).read_text()
            linux_source = (linux / linux_file).read_text()
            with self.subTest(window=mac_file):
                self.assertIn(f"width: {width}, height: {height}", mac_source)
                self.assertIn(f"set_default_size({width}, {height})", linux_source)
                self.assertIn(f"set_size_request({width}, {height})", linux_source)
                self.assertIn(f"scale_window(window, {width}, {height})", linux_source)

    def test_linux_pairing_settings_and_save_failures_follow_macos(self):
        linux_app = (Path(__file__).resolve().parents[1] / "app" / "app.py").read_text()
        pair_block = linux_app.split("async def pair(", 1)[1].split(
            "    def forget(", 1
        )[0]
        settings_block = linux_app.split("    def open_settings(", 1)[1].split(
            "    ui.on_settings", 1
        )[0]
        self.assertNotIn('ui.status("Scanning")', pair_block)
        self.assertIn("copy.deepcopy(config_file.load())", settings_block)
        self.assertIn("schedule_candidate(candidate, raise_error=True)", settings_block)
        for message in (
            "Settings save failed",
            "Pair save failed",
            "Forget device failed",
            "Theme save failed",
            "Output save failed",
            "Position save failed",
            "Relay mode save failed",
            "Input save failed",
        ):
            with self.subTest(message=message):
                self.assertIn(message, linux_app)

    def test_subtitle_height_limit_matches_macos(self):
        mac_subtitle = (MACOS / "SubtitleController.swift").read_text()
        linux_subtitle = (
            Path(__file__).resolve().parents[1] / "app" / "gtk_runtime.py"
        ).read_text()
        self.assertIn("maxWindowHeightRatio: CGFloat = 0.36", mac_subtitle)
        self.assertIn("workarea.height * 0.36", linux_subtitle)

    def test_transcription_configuration_errors_match_macos(self):
        mac_source = (MACOS / "OpenAITranscriptionClient.swift").read_text()
        linux_source = (
            Path(__file__).resolve().parents[1] / "app" / "services.py"
        ).read_text()
        for message in (
            "Transcription API key is not set",
            "Transcription model is not set",
            "Transcription base URL is invalid",
        ):
            with self.subTest(message=message):
                self.assertIn(message, mac_source)
                self.assertIn(message, linux_source)

        for message in (
            "Could not encode transcription request:",
            "Transcription request failed:",
            "Transcription HTTP ",
            "Invalid transcription response:",
            "Transcription response contained no text",
        ):
            with self.subTest(message=message):
                self.assertIn(message, mac_source)
                self.assertIn(message, linux_source)

    def test_translation_errors_match_macos(self):
        mac_source = (MACOS / "LLMTranslationClient.swift").read_text()
        linux_source = (
            Path(__file__).resolve().parents[1] / "app" / "services.py"
        ).read_text()
        for message in (
            "Missing LLM API key",
            "Invalid LLM base URL",
            "Invalid LLM translation response",
        ):
            with self.subTest(message=message):
                self.assertIn(message, mac_source)
                self.assertIn(message, linux_source)

    def test_firmware_window_behavior_and_copy_match_macos(self):
        mac_window = (MACOS / "FirmwareUpdateWindowController.swift").read_text()
        linux = Path(__file__).resolve().parents[1] / "app"
        linux_window = (linux / "gtk_firmware.py").read_text()
        linux_coordinator = (linux / "coordinator.py").read_text()
        for text in (
            "Firmware Update",
            "Updating Firmware",
            "Firmware Updated",
            "The device is rebooting into the new firmware.",
            "Update Failed",
            "The device kept its current firmware.",
            "Cancelling Firmware Update",
            "Stopping transfer and asking the device to abort.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, mac_window)
                self.assertIn(text, linux_window)
        self.assertIn("styleMask: [.titled]", mac_window)
        self.assertIn("set_deletable(False)", linux_window)
        self.assertNotIn('self.ui.status("Checking firmware")', linux_coordinator)
        self.assertNotIn('self.ui.status("Firmware updated', linux_coordinator)
        self.assertNotIn(
            'self.ui.status("Firmware update cancelled")', linux_coordinator
        )
        self.assertNotIn('self.ui.status(f"Firmware update failed', linux_coordinator)

    def test_firmware_release_errors_match_macos(self):
        mac_source = (MACOS / "FirmwareRelease.swift").read_text()
        linux_source = (
            Path(__file__).resolve().parents[1] / "app" / "services.py"
        ).read_text()
        for message in (
            "GitHub returned an invalid firmware release response.",
            "No published firmware release is available yet.",
            "The latest release does not contain the StickS3 firmware manifest.",
            "The firmware manifest does not include a SHA-256 digest.",
            "Could not download the firmware manifest from GitHub.",
            "The downloaded firmware manifest size does not match GitHub.",
            "The downloaded firmware manifest checksum does not match GitHub.",
            "The firmware manifest is invalid.",
            "The latest release does not contain the StickS3 OTA image.",
            "The OTA release asset does not include a SHA-256 digest.",
            "The downloaded firmware checksum does not match GitHub.",
            "The downloaded firmware size does not match GitHub.",
        ):
            with self.subTest(message=message):
                self.assertIn(message, mac_source)
                self.assertIn(message, linux_source)

    def test_app_and_firmware_release_versions_are_independent(self):
        repository = Path(__file__).resolve().parents[3]
        workflow = (repository / ".github/workflows/build-firmware.yml").read_text()
        mac_source = (MACOS / "FirmwareRelease.swift").read_text()
        linux_source = (
            Path(__file__).resolve().parents[1] / "app" / "services.py"
        ).read_text()

        self.assertIn('test "$firmware_version" = "$firmware_cmake_version"', workflow)
        self.assertNotIn('test "$firmware_version" = "$version"', workflow)
        for source in (workflow, mac_source, linux_source):
            self.assertIn("xc-buddy-sticks3-firmware.json", source)

    def test_firmware_transfer_errors_match_macos(self):
        mac_source = (MACOS / "BleCentral.swift").read_text()
        linux = Path(__file__).resolve().parents[1] / "app"
        linux_source = "\n".join(
            (linux / name).read_text() for name in ("bluetooth.py", "coordinator.py")
        )
        for message in (
            "No XC device is connected.",
            "The connected firmware does not expose BLE OTA.",
            "Firmware image is larger than the OTA partition.",
            "Firmware update cancelled.",
            "BLE write failed:",
            "Device rejected OTA:",
        ):
            with self.subTest(message=message):
                self.assertIn(message, mac_source)
                self.assertIn(message, linux_source)

    def test_ble_lifecycle_allows_one_connected_device_and_handles_sleep(self):
        mac_source = (MACOS / "BleCentral.swift").read_text()
        linux_source = (
            Path(__file__).resolve().parents[1] / "app" / "bluetooth.py"
        ).read_text()
        mac_discovery = mac_source.split("didDiscover peripheral:", 1)[1].split(
            "func centralManager(_ central: CBCentralManager, didConnect", 1
        )[0]
        mac_connection = mac_source.split(
            "func centralManager(_ central: CBCentralManager, didConnect", 1
        )[1].split("didFailToConnect", 1)[0]
        mac_scan = mac_source.split("private func scanIfReady()", 1)[1].split(
            "private func restoreConnectedPeripherals", 1
        )[0]
        linux_scan = linux_source.split("async def _scan_loop", 1)[1].split(
            "async def _connect", 1
        )[0]
        linux_connect = linux_source.split("async def _connect", 1)[1].split(
            "def _disconnected", 1
        )[0]

        self.assertIn("guard connectedDevices.isEmpty", mac_discovery)
        self.assertLess(
            mac_discovery.index("central.stopScan()"),
            mac_discovery.index("central.connect(peripheral)"),
        )
        self.assertIn("guard connectedDevices.isEmpty else", mac_connection)
        self.assertIn("guard connectedDevices.isEmpty else", mac_scan)
        self.assertNotIn("pairedDeviceIDs.isSubset", mac_scan)
        self.assertIn("if self.clients:", linux_scan)
        self.assertNotIn("missing_device_ids", linux_scan)
        self.assertIn("if self.clients:", linux_connect)
        self.assertIn('"org.freedesktop.login1.Manager"', linux_source)
        self.assertIn('member="AddMatch"', linux_source)
        self.assertIn("PrepareForSleep", linux_source)
        self.assertIn('protocol.ui_state("ready")', linux_source)

    def test_connected_device_names_and_paired_order_match_macos(self):
        mac_status = (MACOS / "StatusController.swift").read_text()
        mac_ble = (MACOS / "BleCentral.swift").read_text()
        linux = Path(__file__).resolve().parents[1] / "app"
        linux_ble = (linux / "bluetooth.py").read_text()
        linux_ui = (linux / "ui.py").read_text()
        linux_device = (linux / "gtk_device.py").read_text()

        self.assertIn("struct ConnectedXCDevice", mac_ble)
        self.assertIn("class ConnectedXCDevice", linux_ble)
        self.assertIn("name: advertisedName", mac_ble)
        self.assertIn("self.device_names[address] = advertised_name", linux_ble)
        self.assertIn("for deviceID in pairedDeviceIDs.sorted()", mac_status)
        self.assertIn("sorted(", linux_ui)
        self.assertIn('return f"Device: {connected.name} connected"', linux_ui)
        self.assertIn("ui._device_name(device_id)", linux_device)

    def test_forgetting_device_updates_all_macos_config_owners(self):
        mac_app = (MACOS / "AppDelegate.swift").read_text()
        forget_block = mac_app.split("private func forgetDevice", 1)[1].split(
            "\n    }\n}", 1
        )[0]

        self.assertIn(
            "config.deviceOutputProfiles.removeValue(forKey: deviceID)", forget_block
        )
        self.assertIn("self.config = config", forget_block)
        self.assertIn("coordinator?.updateConfig(config)", forget_block)
        self.assertNotIn("coordinator?.updatePairedDeviceIDs", forget_block)

    def test_settings_footer_exposes_the_same_version_label(self):
        mac_settings = (MACOS / "SettingsWindowController.swift").read_text()
        linux_settings = (
            Path(__file__).resolve().parents[1] / "app" / "gtk_settings.py"
        ).read_text()
        mac_info = (MACOS / "Info.plist").read_text()
        linux_project = (
            Path(__file__).resolve().parents[1] / "pyproject.toml"
        ).read_text()

        mac_version = re.search(
            r"<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>",
            mac_info,
        )
        linux_version = re.search(r'^version = "([^"]+)"$', linux_project, re.M)
        self.assertIsNotNone(mac_version)
        self.assertIsNotNone(linux_version)
        self.assertEqual(mac_version.group(1), linux_version.group(1))
        self.assertIn('"Version: \\(Self.applicationVersion)"', mac_settings)
        self.assertIn('f"Version: {package_version()}"', linux_settings)
