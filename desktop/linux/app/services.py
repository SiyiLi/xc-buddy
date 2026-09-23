from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import AppConfig


FIRMWARE_MANIFEST_ASSET = "xc-buddy-sticks3-firmware.json"
FIRMWARE_OTA_ASSET = "xc-buddy-sticks3-ota.bin"
GITHUB_REPOSITORY_URL = "https://github.com/SiyiLi/xc-buddy"
GITHUB_RELEASES_API_URL = (
    "https://api.github.com/repos/SiyiLi/xc-buddy/releases?per_page=20"
)
APP_RELEASE_TAG_PREFIX = "app-v"
GITHUB_LOOKUP_ATTEMPTS = 3
GITHUB_LOOKUP_RETRY_SECONDS = 0.5


class ServiceError(RuntimeError):
    pass


def _request(
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 90,
) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise ServiceError(f"HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ServiceError(str(error)) from error


class TranscriptionClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def transcribe(self, ogg_audio: bytes) -> str:
        api_key = self.config.openai_api_key.strip()
        if not api_key:
            raise ServiceError("Transcription API key is not set")
        if not self.config.openai_model.strip():
            raise ServiceError("Transcription model is not set")
        try:
            url = _chat_completions_url(self.config.openai_base_url, ensure_v1=True)
        except ServiceError as error:
            raise ServiceError("Transcription base URL is invalid") from error
        try:
            payload = json.dumps(
                {
                    "model": self.config.openai_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_audio",
                                    "input_audio": {
                                        "data": base64.b64encode(ogg_audio).decode(
                                            "ascii"
                                        ),
                                        "format": "ogg",
                                    },
                                },
                                {"type": "text", "text": self._prompt()},
                            ],
                        }
                    ],
                    "temperature": 0,
                    "max_tokens": 2048,
                }
            ).encode()
        except (TypeError, ValueError) as error:
            raise ServiceError(
                f"Could not encode transcription request: {error}"
            ) from error
        try:
            response = _request(
                url,
                payload,
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        except ServiceError as error:
            message = str(error)
            if message.startswith("HTTP "):
                detail = message.removeprefix("HTTP ")
                raise ServiceError(f"Transcription HTTP {detail}") from error
            raise ServiceError(f"Transcription request failed: {message}") from error
        try:
            result = json.loads(response.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ServiceError(f"Invalid transcription response: {error}") from error
        try:
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = None
        if not isinstance(text, str) or not text.strip():
            raise ServiceError("Transcription response contained no text")
        return text.strip()

    def _prompt(self) -> str:
        hotwords = ", ".join(self.config.asr_hotwords)
        prompt = self.config.openai_prompt.strip() or AppConfig().openai_prompt
        return prompt + (
            f"\nLikely technical terms and names: {hotwords}" if hotwords else ""
        )


class TranslationClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def translate(self, text: str, target: str) -> str:
        api_key = self.config.llm_api_key.strip()
        if not api_key:
            raise ServiceError("Missing LLM API key")
        try:
            url = _chat_completions_url(self.config.llm_base_url)
        except ServiceError as error:
            raise ServiceError("Invalid LLM base URL") from error
        system = (
            "You are a real-time speech translator.\n"
            f"Translate the user's text into {target}.\n"
            "Detect the source language automatically.\n"
            "Return only the translated text, with no explanations, quotes, prefixes, "
            "alternatives, or markdown.\n"
            "The text may come from live speech recognition and may contain minor "
            "recognition errors; infer the intended meaning when it is clear."
        )
        hotwords = [term.strip() for term in self.config.asr_hotwords if term.strip()]
        if hotwords:
            system += "\n\nImportant terms that may appear:\n" + "\n".join(
                f"- {term}" for term in hotwords
            )
        request_body: dict[str, Any] = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        }
        if "gpt-5.6" in self.config.llm_model.lower():
            request_body["reasoning_effort"] = "none"
        else:
            request_body["temperature"] = 0
        payload = json.dumps(request_body).encode()
        try:
            result = json.loads(
                _request(
                    url,
                    payload,
                    {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=8,
                ).decode()
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ServiceError("Invalid LLM translation response") from error
        try:
            return result["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            raise ServiceError("Invalid LLM translation response") from error


def _chat_completions_url(base: str, ensure_v1: bool = False) -> str:
    base = base.strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ServiceError("invalid API base URL")
    if base.endswith("/chat/completions"):
        return base
    if ensure_v1 and not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


@dataclass(frozen=True)
class FirmwareRelease:
    version: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class AppRelease:
    version: str
    url: str
    sha256: str
    size: int
    asset_name: str


def version_is_older(current: str, latest: str) -> bool:
    def parse(value: str):
        value = value.strip().removeprefix("v").removeprefix("V")
        numeric, separator, suffix = value.partition("-")
        try:
            numbers = tuple(int(part) for part in numeric.split("."))
        except ValueError:
            return None
        return numbers, suffix if separator else None

    left, right = parse(current), parse(latest)
    if not left or not right:
        return False
    count = max(len(left[0]), len(right[0]))
    left_numbers = left[0] + (0,) * (count - len(left[0]))
    right_numbers = right[0] + (0,) * (count - len(right[0]))
    if left_numbers != right_numbers:
        return left_numbers < right_numbers
    if left[1] is not None and right[1] is None:
        return True
    if left[1] is None or right[1] is None:
        return False

    def natural(value: str):
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.lower())
            for part in re.split(r"(\d+)", value)
        )

    return natural(left[1]) < natural(right[1])


def latest_firmware() -> FirmwareRelease:
    try:
        response = _request(
            "https://api.github.com/repos/SiyiLi/xc-buddy/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "XC-Buddy",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    except ServiceError as error:
        if str(error).startswith("HTTP 404:"):
            raise ServiceError(
                "No published firmware release is available yet."
            ) from error
        raise ServiceError(
            "GitHub returned an invalid firmware release response."
        ) from error
    try:
        data = json.loads(response.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceError(
            "GitHub returned an invalid firmware release response."
        ) from error
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        raise ServiceError("GitHub returned an invalid firmware release response.")
    manifest_asset = next(
        (
            value
            for value in data["assets"]
            if isinstance(value, dict) and value.get("name") == FIRMWARE_MANIFEST_ASSET
        ),
        None,
    )
    if not manifest_asset:
        raise ServiceError(
            "The latest release does not contain the StickS3 firmware manifest."
        )
    asset = next(
        (
            value
            for value in data["assets"]
            if isinstance(value, dict) and value.get("name") == FIRMWARE_OTA_ASSET
        ),
        None,
    )
    if not asset:
        raise ServiceError("The latest release does not contain the StickS3 OTA image.")
    manifest_digest = manifest_asset.get("digest", "")
    if not manifest_digest.startswith("sha256:"):
        raise ServiceError("The firmware manifest does not include a SHA-256 digest.")
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:"):
        raise ServiceError("The OTA release asset does not include a SHA-256 digest.")
    try:
        manifest_url = str(manifest_asset["browser_download_url"])
        manifest_size = int(manifest_asset["size"])
        manifest_sha256 = manifest_digest.removeprefix("sha256:").lower()
        ota_url = str(asset["browser_download_url"])
        ota_size = int(asset["size"])
        ota_sha256 = digest.removeprefix("sha256:").lower()
    except (KeyError, TypeError, ValueError) as error:
        raise ServiceError(
            "GitHub returned an invalid firmware release response."
        ) from error
    try:
        manifest_data = _request(
            manifest_url,
            headers={"User-Agent": "XC-Buddy"},
        )
    except ServiceError as error:
        raise ServiceError(
            "Could not download the firmware manifest from GitHub."
        ) from error
    if len(manifest_data) != manifest_size:
        raise ServiceError(
            "The downloaded firmware manifest size does not match GitHub."
        )
    if hashlib.sha256(manifest_data).hexdigest() != manifest_sha256:
        raise ServiceError(
            "The downloaded firmware manifest checksum does not match GitHub."
        )
    try:
        manifest = json.loads(manifest_data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceError("The firmware manifest is invalid.") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("hardware") != "stick_s3"
        or manifest.get("ota_asset") != FIRMWARE_OTA_ASSET
        or not isinstance(manifest.get("version"), str)
        or not manifest["version"].strip()
    ):
        raise ServiceError("The firmware manifest is invalid.")
    return FirmwareRelease(
        manifest["version"].strip(),
        ota_url,
        ota_sha256,
        ota_size,
    )


def linux_release_asset_name(version: str) -> str:
    architecture = platform.machine().lower()
    python_tag = f"py{sys.version_info.major}{sys.version_info.minor}"
    return f"xc-buddy-linux-{architecture}-{python_tag}-{version}.tar.gz"


def _latest_github_release_version() -> str:
    request = urllib.request.Request(
        GITHUB_RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "XC-Buddy",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    response_data = b""
    last_error: Exception | None = None
    last_message = ""
    for attempt in range(GITHUB_LOOKUP_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response_data = response.read()
            break
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ServiceError(
                    "No published app release is available yet."
                ) from error
            last_error = error
            last_message = f"HTTP {error.code}."
            if error.code not in (403, 429) and error.code < 500:
                break
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            last_message = str(error)
        if attempt + 1 < GITHUB_LOOKUP_ATTEMPTS:
            time.sleep(GITHUB_LOOKUP_RETRY_SECONDS * (attempt + 1))
    if not response_data:
        raise ServiceError(
            f"Could not resolve the latest GitHub release: {last_message}"
        ) from last_error

    try:
        releases = json.loads(response_data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceError(
            "GitHub returned an invalid app release response."
        ) from error
    if not isinstance(releases, list):
        raise ServiceError("GitHub returned an invalid app release response.")
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith(APP_RELEASE_TAG_PREFIX):
            continue
        version = tag.removeprefix(APP_RELEASE_TAG_PREFIX)
        if not re.fullmatch(r"\d+(?:\.\d+)*(?:-[0-9A-Za-z.-]+)?", version):
            raise ServiceError("GitHub returned an invalid app release version.")
        return version
    raise ServiceError("No published app release is available yet.")


def latest_app_release(current_version: str | None = None) -> AppRelease:
    version = _latest_github_release_version()
    asset_name = linux_release_asset_name(version)
    if current_version and not version_is_older(current_version, version):
        return AppRelease(version, "", "", 0, asset_name)

    archive_url = (
        f"{GITHUB_REPOSITORY_URL}/releases/download/"
        f"{APP_RELEASE_TAG_PREFIX}{urllib.parse.quote(version, safe='')}/{asset_name}"
    )
    try:
        checksum = _request(f"{archive_url}.sha256", headers={"User-Agent": "XC-Buddy"})
    except ServiceError as error:
        if str(error).startswith("HTTP 404:"):
            raise ServiceError(
                "The latest release does not contain an update for this Linux runtime."
            ) from error
        raise ServiceError(
            f"Could not download the app update checksum: {error}"
        ) from error
    try:
        checksum_line = checksum.decode().strip()
    except UnicodeDecodeError as error:
        raise ServiceError("The Linux app update checksum is invalid.") from error
    digest, separator, declared_name = checksum_line.partition("  ")
    if (
        not separator
        or declared_name != asset_name
        or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
    ):
        raise ServiceError("The Linux app update checksum is invalid.")
    return AppRelease(
        version=version,
        url=archive_url,
        sha256=digest.lower(),
        size=0,
        asset_name=asset_name,
    )


def download_app_release(release: AppRelease) -> bytes:
    archive = _request(release.url, headers={"User-Agent": "XC-Buddy"})
    if release.size > 0 and len(archive) != release.size:
        raise ServiceError("The downloaded app update size does not match GitHub.")
    if hashlib.sha256(archive).hexdigest() != release.sha256:
        raise ServiceError("The downloaded app update checksum does not match GitHub.")
    return archive


def download_firmware(release: FirmwareRelease) -> bytes:
    image = _request(release.url, headers={"User-Agent": "XC-Buddy"})
    if len(image) != release.size:
        raise ServiceError("The downloaded firmware size does not match GitHub.")
    if hashlib.sha256(image).hexdigest() != release.sha256:
        raise ServiceError("The downloaded firmware checksum does not match GitHub.")
    return image
