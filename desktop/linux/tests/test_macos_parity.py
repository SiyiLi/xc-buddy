import re
import unittest
from pathlib import Path

from app import protocol
from app.config import AppConfig
from app.ui import THEME_NAMES, TRANSLATION_TARGETS


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

    def test_app_versions_match(self):
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
