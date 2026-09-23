from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


@dataclasses.dataclass
class OutputProfile:
    target: str = "focused_app"
    transform: str = "original"
    translation_target: str = "en"


@dataclasses.dataclass
class PowerTimers:
    display_dim_seconds: int = 30
    display_off_seconds: int = 300
    idle_deep_sleep_seconds: int = 300
    codex_deep_sleep_seconds: int = 900


@dataclasses.dataclass
class AppConfig:
    openai_base_url: str = "https://inference-api.nvidia.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gcp/google/gemini-3.6-flash"
    openai_language: str = ""
    openai_prompt: str = (
        "Transcribe this audio accurately and verbatim. The speaker may mix "
        "Mandarin Chinese and English technical terms in the same sentence. "
        "Preserve each language, technical names, punctuation, and capitalization. "
        "Output transcript only."
    )
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-5.5"
    interaction_mode: str = "hold_to_talk"
    asr_hotwords: list[str] = dataclasses.field(default_factory=list)
    paired_device_ids: list[str] = dataclasses.field(default_factory=list)
    device_theme_colors: dict[str, str] = dataclasses.field(default_factory=dict)
    device_overlay_positions: dict[str, str] = dataclasses.field(default_factory=dict)
    output: OutputProfile = dataclasses.field(default_factory=OutputProfile)
    device_outputs: dict[str, OutputProfile] = dataclasses.field(default_factory=dict)
    auto_enter: bool = True
    debug_audio_cache: bool = False
    debug_audio_dir: Path = dataclasses.field(
        default_factory=lambda: config_dir() / "DebugAudio"
    )
    codex_bridge_port: int = 17321
    codex_bridge_token: str = ""
    codex_success_chime: bool = True
    relay_mode: str = "disabled"
    relay_url: str = ""
    relay_sender_token: str = ""
    relay_receiver_token: str = ""
    power_timers: PowerTimers = dataclasses.field(default_factory=PowerTimers)

    def profile_for(self, device_id: str | None) -> OutputProfile:
        profile = self.device_outputs.get(normalize_device_id(device_id or ""))
        if profile is None:
            return dataclasses.replace(self.output)
        return OutputProfile(
            target=self.output.target,
            transform=profile.transform,
            translation_target=profile.translation_target,
        )


def config_dir() -> Path:
    base = Path(
        os.environ.get(
            "XC_BUDDY_CONFIG_HOME",
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"),
        )
    )
    return base / "xc-buddy"


def config_path() -> Path:
    return config_dir() / "config.toml"


def normalize_device_id(value: str) -> str:
    value = value.strip().upper()
    if value.startswith(("XC-", "VS-")):
        value = value[3:]
    value = value[:4]
    return value if re.fullmatch(r"[0-9A-F]{4}", value) else ""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                return decoded
        except json.JSONDecodeError:
            pass
        value = value[1:-1]
    return value.replace(r"\"", '"').replace(r"\\", "\\")


def _boolean(value: str, fallback: bool) -> bool:
    return {
        "true": True,
        "yes": True,
        "1": True,
        "on": True,
        "false": False,
        "no": False,
        "0": False,
        "off": False,
    }.get(value.strip().lower(), fallback)


def _integer(value: str, fallback: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
        return parsed if low <= parsed <= high else fallback
    except ValueError:
        return fallback


def load(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    config = AppConfig()
    if not path.exists():
        return config
    text = path.read_text(encoding="utf-8")
    values: dict[tuple[str, str], str] = {}
    try:
        document = tomllib.loads(text)

        def value_text(value: object) -> str:
            if isinstance(value, bool):
                return str(value).lower()
            return str(value)

        for key, value in document.items():
            if not isinstance(value, dict):
                values[("", key)] = value_text(value)
        output = document.get("output")
        if isinstance(output, dict):
            for key, value in output.items():
                if not isinstance(value, dict):
                    values[("output", key)] = value_text(value)
        devices = document.get("device")
        if isinstance(devices, dict):
            for raw_device_id, device in devices.items():
                if not isinstance(device, dict):
                    continue
                device_output = device.get("output")
                if not isinstance(device_output, dict):
                    continue
                section = f'device."{raw_device_id}".output'
                for key, value in device_output.items():
                    if not isinstance(value, dict):
                        values[(section, key)] = value_text(value)
    except tomllib.TOMLDecodeError:
        # Preserve compatibility with the original permissive key=value format.
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                values[(section, key.strip())] = _unquote(value)

    def get(key: str, fallback: str) -> str:
        return values.get(("", key), fallback)

    for field in (
        "openai_base_url",
        "openai_api_key",
        "openai_model",
        "openai_language",
        "openai_prompt",
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "interaction_mode",
        "codex_bridge_token",
        "relay_mode",
        "relay_url",
        "relay_sender_token",
        "relay_receiver_token",
    ):
        setattr(config, field, get(field, getattr(config, field)))
    if config.interaction_mode not in ("hold_to_talk", "click_to_talk"):
        config.interaction_mode = "hold_to_talk"
    if config.relay_mode not in ("disabled", "sender", "receiver"):
        config.relay_mode = "disabled"
    config.asr_hotwords = list(
        dict.fromkeys(
            x.strip()
            for x in re.split(r"[,\r\n]", get("asr_hotwords", ""))
            if x.strip()
        )
    )
    config.paired_device_ids = list(
        dict.fromkeys(
            filter(
                None,
                (
                    normalize_device_id(x)
                    for x in get("paired_device_ids", "").split(",")
                ),
            )
        )
    )
    config.auto_enter = _boolean(get("auto_enter", "true"), config.auto_enter)
    config.debug_audio_cache = _boolean(get("debug_audio_cache", "false"), False)
    debug_dir = get("debug_audio_dir", "")
    if debug_dir:
        config.debug_audio_dir = Path(debug_dir).expanduser()
    config.codex_bridge_port = _integer(
        get("codex_bridge_port", "17321"), 17321, 1, 65535
    )
    config.codex_success_chime = _boolean(get("codex_success_chime", "true"), True)
    config.power_timers = PowerTimers(
        _integer(get("display_dim_seconds", "30"), 30, 5, 3600),
        _integer(get("display_off_seconds", "300"), 300, 30, 86400),
        _integer(get("idle_deep_sleep_seconds", "300"), 300, 60, 86400),
        _integer(get("codex_deep_sleep_seconds", "900"), 900, 60, 86400),
    )
    translation_target = values.get(
        ("output", "translation_target"), get("translation_target", "en")
    ).strip()
    config.output = OutputProfile(
        values.get(("output", "target"), get("output_target", "focused_app")),
        values.get(("output", "transform"), get("text_transform", "original")),
        translation_target or "en",
    )
    if config.output.target not in ("focused_app", "subtitle"):
        config.output.target = "focused_app"
    if config.output.transform not in ("original", "translate"):
        config.output.transform = "original"
    for (section_name, key), value in values.items():
        match = re.fullmatch(r'device\."?([^".]+)"?\.output', section_name)
        if not match:
            continue
        device_id = normalize_device_id(match.group(1))
        if not device_id:
            continue
        profile = config.device_outputs.setdefault(
            device_id, dataclasses.replace(config.output)
        )
        if key == "transform":
            profile.transform = (
                value if value in ("original", "translate") else "original"
            )
        elif key == "translation_target" and value.strip():
            profile.translation_target = value.strip()
    for key, target, allowed in (
        (
            "device_theme_colors",
            config.device_theme_colors,
            {"white", "pink", "green", "yellow", "blue", "purple"},
        ),
        (
            "device_overlay_positions",
            config.device_overlay_positions,
            {"center", "top_left", "top_right", "bottom_left", "bottom_right"},
        ),
    ):
        for item in get(key, "").split(","):
            if ":" in item:
                device_id, value = item.split(":", 1)
                normalized_value = value.strip().lower()
                if (
                    device_id := normalize_device_id(device_id)
                ) and normalized_value in allowed:
                    target[device_id] = normalized_value
    return config


def _escape(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', r"\"")
        .replace("\n", r"\n")
        .replace("\r", r"\r")
        .replace("\t", r"\t")
    )


def save(config: AppConfig, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    scalar = [
        ("openai_base_url", config.openai_base_url),
        ("openai_api_key", config.openai_api_key),
        ("openai_model", config.openai_model),
        ("openai_language", config.openai_language),
        ("openai_prompt", config.openai_prompt),
        ("llm_base_url", config.llm_base_url),
        ("llm_api_key", config.llm_api_key),
        ("llm_model", config.llm_model),
        ("interaction_mode", config.interaction_mode),
        ("asr_hotwords", ",".join(config.asr_hotwords)),
        ("paired_device_ids", ",".join(config.paired_device_ids)),
        (
            "device_theme_colors",
            ",".join(
                f"{key}:{value}"
                for key, value in sorted(config.device_theme_colors.items())
                if key in config.paired_device_ids and value != "white"
            ),
        ),
        (
            "device_overlay_positions",
            ",".join(
                f"{key}:{value}"
                for key, value in sorted(config.device_overlay_positions.items())
                if key in config.paired_device_ids and value != "center"
            ),
        ),
    ]
    lines = [f'{key} = "{_escape(value)}"' for key, value in scalar]
    lines += [
        f"auto_enter = {str(config.auto_enter).lower()}",
        f"debug_audio_cache = {str(config.debug_audio_cache).lower()}",
        f'debug_audio_dir = "{_escape(config.debug_audio_dir)}"',
        f"codex_bridge_port = {config.codex_bridge_port}",
        f'codex_bridge_token = "{_escape(config.codex_bridge_token)}"',
        f"codex_success_chime = {str(config.codex_success_chime).lower()}",
        f'relay_mode = "{config.relay_mode}"',
        f'relay_url = "{_escape(config.relay_url)}"',
        f'relay_sender_token = "{_escape(config.relay_sender_token)}"',
        f'relay_receiver_token = "{_escape(config.relay_receiver_token)}"',
        f"display_dim_seconds = {config.power_timers.display_dim_seconds}",
        f"display_off_seconds = {config.power_timers.display_off_seconds}",
        f"idle_deep_sleep_seconds = {config.power_timers.idle_deep_sleep_seconds}",
        f"codex_deep_sleep_seconds = {config.power_timers.codex_deep_sleep_seconds}",
        "",
        "[output]",
        f'target = "{config.output.target}"',
        f'transform = "{config.output.transform}"',
        f'translation_target = "{_escape(config.output.translation_target)}"',
    ]
    for device_id, profile in sorted(config.device_outputs.items()):
        if device_id not in config.paired_device_ids or profile == config.output:
            continue
        lines += [
            "",
            f'[device."{device_id}".output]',
            f'transform = "{profile.transform}"',
            f'translation_target = "{_escape(profile.translation_target)}"',
        ]
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
