# XC Buddy

XC Buddy turns Louis's M5Stack StickS3 into a local voice-and-status companion for macOS and Codex. The device advertises as `XC-XXXX`; the macOS menu bar app receives Ogg/Opus audio over BLE, transcribes it, pastes into the focused app, and reflects Codex lifecycle events back to the device.

> **Distribution notice:** this personal project is derived from upstream [VoiceStick](https://github.com/78/voicestick), which currently has no declared license. Keep this repository private; do not make the derivative, its binaries, or firmware public or distribute them until the upstream author grants permission or declares a compatible license.

## Architecture

```text
StickS3 mic -> Opus -> BLE (existing UUIDs) -> XC Buddy macOS
                                              |-> ASR -> focused app / subtitles
Codex notify helper -> 127.0.0.1:17321 -------|-> device lifecycle display
```

- `firmware/`: ESP-IDF firmware for StickS3. The UUIDs and audio/control framing remain VoiceStick-compatible.
- `desktop/macos/`: native AppKit menu bar application for macOS 12+.
- `scripts/xc-buddy-codex-notify.py`: quiet, standard-library Codex notify bridge.
- `docs/codex-integration.md`: loopback bridge setup and event semantics.
- `docs/internal-transcription.md`: OpenAI-compatible transcription contract.
- `docs/protocol.md`: unchanged BLE protocol framing and UUIDs.

## macOS build and run

No package installation is required beyond the dependencies already declared by SwiftPM.

```sh
swift build --package-path desktop/macos
swift run --package-path desktop/macos VoiceStickApp
```

The internal Swift target retains its upstream name to keep the patch surgical; the application and bundle identity are `XC Buddy` / `ai.xc.buddy`.

Configuration is stored separately from VoiceStick so both apps can coexist:

```text
~/Library/Application Support/XC Buddy/config.toml
```

Copy `desktop/macos/Config/config.example.toml` there, then edit it locally. Accessibility permission is needed for focused-app paste/send. API keys and the optional bridge token are stored as plain text in this local v0.1 config; protect the file and do not commit it.

## Firmware build

The recommended local build uses Docker, so ESP-IDF and its cross-compiler do
not need to be installed on the Mac:

```sh
docker run --rm --platform linux/amd64 \
  -v "$PWD:/project" -w /project/firmware \
  espressif/idf:v5.5.1 bash -lc '
    idf.py build
    mkdir -p ../dist
    cp build/xc_buddy.bin ../dist/xc-buddy-sticks3-ota.bin
    cd build
    esptool.py --chip esp32s3 merge_bin \
      -o ../../dist/xc-buddy-sticks3-merged.bin @flash_args
  '
```

The private repository also has a firmware-only GitHub Actions workflow at
`.github/workflows/build-firmware.yml`.

To use an existing native ESP-IDF 5.x environment instead:

```sh
cd firmware
. /path/to/esp-idf/export.sh
idf.py set-target esp32s3
idf.py build
```

The result advertises as `XC-XXXX` (for the target device, `XC-717C`) while preserving the established GATT service and characteristic UUIDs.

For a full browser flash, open [ESP Launchpad](https://espressif.github.io/esp-launchpad/)
in Chrome or Edge, choose **DIY**, select `dist/xc-buddy-sticks3-merged.bin`,
set the flash address to `0x0`, connect the StickS3, and program it.

## Interaction

- Front button: hold/click to record according to configuration; confirm pending paste.
- Side button: cancel or restore the most recent recoverable input.
- Device display: offline/booting, ready, listening, thinking, confirmation, Codex working, approval needed, done, and error states.
- Menu bar summary: device connection, ASR provider, output target, and Codex bridge status.

See [Codex integration](docs/codex-integration.md) and [internal transcription](docs/internal-transcription.md) for exact local setup.

## Upstream attribution

XC Buddy v0.1 is a private local derivative of VoiceStick 0.3.4 by `78`. Existing protocol names in source and unchanged cloud/update URLs are retained where they describe compatibility or an upstream service, rather than user-visible product identity.
