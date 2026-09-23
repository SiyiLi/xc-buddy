# XC Buddy

XC Buddy turns Louis's M5Stack StickS3 into a local voice-and-status companion for macOS, Linux, and Codex. The device advertises as `XC-XXXX`; the desktop app receives Ogg/Opus audio over BLE, transcribes it, pastes into the focused app, and reflects Codex lifecycle events back to the device.

> **Licensing notice:** this personal project is derived from upstream [VoiceStick](https://github.com/78/voicestick), which currently has no declared license. Public repository visibility does not grant redistribution rights; resolve the upstream licensing status before distributing XC Buddy binaries or firmware.

## Architecture

```text
StickS3 mic -> Opus -> BLE (existing UUIDs) -> XC Buddy desktop
                                              |-> ASR -> focused app / subtitles
Codex notify helper -> 127.0.0.1:17321 -------|-> device lifecycle display
```

- `firmware/`: ESP-IDF firmware for StickS3. The UUIDs and audio/control framing remain VoiceStick-compatible.
- `desktop/macos/`: native AppKit menu bar application for macOS 12+.
- `desktop/linux/`: BlueZ-based Linux tray application with X11 and Wayland input support.
- `scripts/xc-buddy-codex-notify.py`: quiet, standard-library Codex lifecycle bridge.
- `scripts/install-xc-buddy-codex-hooks.py`: idempotent user-level Codex hook installer.
- `docs/codex-integration.md`: loopback bridge setup and event semantics.
- `docs/internal-transcription.md`: OpenAI-compatible transcription contract.
- `docs/protocol.md`: unchanged BLE protocol framing and UUIDs.

The macOS and Linux clients share the same workflows, controls, persisted
options, and user-facing wording. Native widget texture may differ. macOS uses
its nested menu-bar device menu; GNOME's tray protocol cannot reliably present
that third menu level, so selecting a device on Linux opens the equivalent
XC device window. The temporary [feature parity checklist](FEATURE_PARITY.md)
tracks the remaining qualification work and will be removed after parity is
verified.

## Operations runbook

This runbook was completed end-to-end on August 5, 2026 using an Apple Silicon
Mac running macOS 26.5.2, Docker Desktop 29.4.2, ESP-IDF 5.5.1, and the target
StickS3 `XC-717C`. Run all commands from the repository root unless a step says
otherwise.

### 1. Check prerequisites and connect the StickS3

- Start Docker Desktop and wait for `docker info` to succeed.
- Connect the StickS3 with a data-capable USB cable.
- Find its USB serial/JTAG port before running any flash command:

```sh
docker info --format '{{.ServerVersion}} {{.OSType}}/{{.Architecture}}'
ls -l /dev/cu.usbmodem*
```

The tested device appeared as `/dev/cu.usbmodem1101`. The suffix can change
after reconnecting, so verify the port every time instead of copying it
blindly.

### 2. Build both firmware images with Docker

The pinned Docker image contains ESP-IDF, its cross-compiler, and the `esptool`
used to merge images. Nothing from ESP-IDF is installed into macOS or a local
Python environment.

The first pull is large and can take several minutes:

```sh
docker pull --platform linux/amd64 espressif/idf:v5.5.1
```

Build the firmware and generate the two distribution artifacts:

```sh
docker run --rm --platform linux/amd64 \
  -v "$PWD:/project" -w /project/firmware \
  espressif/idf:v5.5.1 bash -lc '
    set -euo pipefail
    idf.py build
    mkdir -p ../dist
    cp build/xc_buddy.bin ../dist/xc-buddy-sticks3-ota.bin
    cd build
    esptool.py --chip esp32s3 merge_bin \
      -o ../../dist/xc-buddy-sticks3-merged.bin @flash_args
    cd ../../dist
    sha256sum xc-buddy-sticks3-ota.bin \
      > xc-buddy-sticks3-ota.bin.sha256
    sha256sum xc-buddy-sticks3-merged.bin \
      > xc-buddy-sticks3-merged.bin.sha256
  '
```

A clean Docker build took about 148 seconds on the tested Mac. Give automated
build commands at least a 240-second timeout. Keep the build directory on the
host—the default `firmware/build` path above already does this. If overriding
it with `idf.py -B`, use a host-mounted path rather than container-only `/tmp`;
otherwise `docker run --rm` discards the compiled objects when a timeout kills
the container.

The outputs have different purposes:

- `dist/xc-buddy-sticks3-ota.bin` is the app-slot image for BLE OTA.
- `dist/xc-buddy-sticks3-merged.bin` contains the bootloader, partition table,
  initial OTA data, and app. Flash this image at `0x0` for a full USB install.

Inspect and verify the artifacts without hard-coding version-specific hashes:

```sh
ls -lh dist/xc-buddy-sticks3-ota.bin dist/xc-buddy-sticks3-merged.bin
(cd dist && shasum -a 256 -c \
  xc-buddy-sticks3-ota.bin.sha256 \
  xc-buddy-sticks3-merged.bin.sha256)
```

The verified v0.1.1 build produced a `0x15b950`-byte app image with 55% of the
3 MB OTA slot free. GitHub Actions runs the same build and checksum commands
inside the ESP-IDF container, then uploads both images, their checksums, and the
generated firmware manifest with its checksum. Keep checksum generation inside
that container because it owns the `dist` directory.

### Publish firmware for XC Buddy OTA

XC Buddy checks this public repository's latest GitHub Release anonymously. It
does not use a GitHub token, store GitHub credentials, or require a GitHub
configuration field. The app only accepts the exact StickS3 release asset
`xc-buddy-sticks3-ota.bin` named by
`xc-buddy-sticks3-firmware.json`. It verifies both assets against their
GitHub-provided sizes and SHA-256 digests before transferring the image through
BLE OTA.

The app and firmware have independent versions. The app version must agree in:

- `VERSION`, which is also consumed by the Windows build
- both bundle version values in `desktop/macos/Sources/XCBuddy/Info.plist`
- `desktop/linux/pyproject.toml`

The firmware version must agree in:

- `firmware/version.txt`
- the `project(xc_buddy VERSION ...)` value in `firmware/CMakeLists.txt`

The two versions do not need to match. The release tag is `v<app-version>`, and
the generated firmware manifest carries the independent firmware version.

Public firmware publishing is disabled by default while the upstream licensing
status remains unresolved. After redistribution rights are confirmed, enable
the repository Actions variable once:

```sh
gh variable set ENABLE_PUBLIC_FIRMWARE_RELEASES \
  --body true \
  --repo SiyiLi/xc-buddy
```

Then, after building and testing a new app version, commit it on `main` and push
a new, never-reused app version tag. For example, for app version `0.2.2`:

```sh
git tag -a v0.2.2 -m "XC Buddy v0.2.2"
git push origin main
git push origin v0.2.2
```

The `v*` tag starts `.github/workflows/build-firmware.yml`. The workflow builds
the OTA and full USB images and writes checksums. When
`ENABLE_PUBLIC_FIRMWARE_RELEASES` is `true`, it also builds the Python 3.10 and
3.12 Linux update archives, checks the app and firmware version groups
independently, and creates one public GitHub Release containing all assets using
GitHub Actions' built-in token. Leave the variable unset or set it to `false`
until redistribution is permitted.

Do not attach the merged image to the BLE updater. The release contains it only
for manual USB recovery; XC Buddy downloads only the OTA app-slot image.
The website's Web Serial flasher independently selects the exact
`xc-buddy-sticks3-merged.bin` asset from the same latest public Release.

### 3. Create the repository-local USB flashing environment

Never install `esptool` into the system Python or an active Conda environment.
Create the ignored `.venv` inside this repository and invoke its Python by
its explicit repo-relative path:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir esptool==4.9.0
.venv/bin/python -m esptool version
```

This is a real Python installation, but it is isolated under `.venv/`. Deleting
that directory removes it; system Python and Conda remain unchanged.

### 4. Flash and verify the merged firmware

Recheck the port, then use the exact device path in the command. This overwrites
the beginning of the connected device's flash, so do not substitute an
unverified serial port.

```sh
ls -l /dev/cu.usbmodem*

.venv/bin/python -m esptool \
  --chip esp32s3 \
  --port /dev/cu.usbmodem1101 \
  --baud 460800 \
  --before default_reset \
  --after hard_reset \
  write_flash \
  --flash_mode dio \
  --flash_size 8MB \
  --flash_freq 80m \
  0x0 dist/xc-buddy-sticks3-merged.bin
```

A successful flash ends with output equivalent to:

```text
Writing ... (100 %)
Wrote 1488768 bytes ...
Hash of data verified.
Hard resetting via RTS pin...
```

The byte count changes when the firmware changes; `Hash of data verified` is
the important integrity check. The StickS3 side control is a combined
reset/power button: long press enters download mode, double-click powers off,
and single click powers on or resets. If the internal green LED keeps blinking
after a flash, release the button and single-click it to boot the application.
On the tested USB Serial/JTAG connection, `esptool run` could report a hard
reset while the device remained in download mode, so verify the screen rather
than treating that command's exit status as proof of a normal boot. See the
[official StickS3 button instructions](https://docs.m5stack.com/en/core/StickS3#button-operation-instructions).

After boot, the target firmware advertises as `XC-717C` while preserving the
VoiceStick-compatible GATT service and characteristic UUIDs.

### 5. Build and launch the desktop app

#### macOS

SwiftPM owns all application dependencies; do not install them with a package
manager. On a normally matched Xcode/Command Line Tools installation, use:

```sh
swift build --package-path desktop/macos
swift run --package-path desktop/macos XCBuddy
```

On the tested Mac, the default `MacOSX.sdk` Swift interface did not match the
installed Swift compiler. The following fallback was verified using the
installed macOS 15.4 SDK and temporary build/module caches:

```sh
mkdir -p /tmp/xc-buddy-swiftpm-cache \
  /tmp/xc-buddy-clang-cache \
  /tmp/xc-buddy-swift-build

SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk \
CLANG_MODULE_CACHE_PATH=/tmp/xc-buddy-clang-cache \
SWIFTPM_MODULECACHE_OVERRIDE=/tmp/xc-buddy-swiftpm-cache \
swift build \
  --package-path desktop/macos \
  --scratch-path /tmp/xc-buddy-swift-build
```

Launch the tested debug executable and keep a local diagnostic log:

```sh
nohup /tmp/xc-buddy-swift-build/arm64-apple-macosx/debug/XCBuddy \
  >/tmp/xc-buddy-app.log 2>&1 &
```

The Swift target and executable are `XCBuddy`; the application and bundle
identity are `XC Buddy` and `ai.xc.buddy`. This verifies the local debug
executable; universal application packaging must still be exercised on a Mac.

##### Private macOS releases and automatic updates

Private releases use an ad-hoc application signature and a Sparkle-signed ZIP.
They do not require an Apple Developer ID, notarization credentials, or a DMG.
On each Mac, the first downloaded installation must be approved once with
**Privacy & Security > Open Anyway**. Sparkle verifies subsequent update ZIPs
with a separate EdDSA key before installing them.

Create the Sparkle key once on the release Mac. First run a debug package build
so SwiftPM downloads Sparkle into the repository-local build directory:

```sh
scripts/build-macos.sh --debug
generate_keys="$(find -L desktop/macos/.build-arm64/artifacts \
  -name generate_keys -type f | head -1)"
"$generate_keys"
```

The tool stores the private key in the login Keychain and prints the public
key. Back up the private key using Sparkle's documented export command. Do not
commit or print the private key. Supply the printed public key when producing a
release build:

```sh
SPARKLE_PUBLIC_ED_KEY='<public key>' scripts/build-macos.sh --release
```

The release build defaults to the repository's HTTPS appcast, signs the app
ad-hoc, signs each Sparkle nested component explicitly, verifies the complete
bundle, and creates:

```text
build/XC-Buddy-<version>.app
build/XC-Buddy-<version>.zip
build/XC-Buddy-<version>.signature
```

It fails instead of producing a release when the public key, Sparkle signing
tool, private Keychain key, or update signature is unavailable. Attach the ZIP
to the corresponding app release, then pass its URL, byte length, and the
contents of the `.signature` file to `scripts/update-appcast.py`. Commit and
publish the resulting `website/public/appcast.xml` only after the release asset
is available.

Before accepting the update path, install one older ZIP on a real Mac, approve
it once, and update it to a newer test version through **Check for App
Updates...**. Verify the version and relaunch, then verify that Bluetooth and
Accessibility permission still work and that recognized text can still be
pasted. This physical test is required because ad-hoc signing may not preserve
macOS privacy permissions across application updates.

`scripts/make-dmg.sh` remains an optional public-distribution helper. It never
re-signs the application; notarization runs only when the input already has a
Developer ID signature and the `AC_PASSWORD` Keychain profile exists.

#### Linux

XC Buddy supports Python 3.10+, BlueZ 5.55+, and a graphical X11 or Wayland
session. On Ubuntu, install only the native helpers needed by the active
session:

```sh
# X11
sudo apt install python3-venv python3-tk bluez xdotool xclip

# GNOME Wayland
sudo apt install python3-venv python3-tk bluez wl-clipboard \
  x11-xserver-utils gir1.2-atspi-2.0

# Other wlroots Wayland compositors
sudo apt install python3-venv python3-tk bluez wl-clipboard wtype
```

Build an immutable artifact, install that exact artifact into XC Buddy's
per-user release directory, and run the installed application:

```sh
python3 desktop/linux/scripts/build-user.py
# Substitute the exact artifact directory printed by the build command.
python3 desktop/linux/scripts/install-user.py desktop/linux/dist/linux/<build-id>
~/.local/bin/xc-buddy --doctor
~/.local/bin/xc-buddy --version
```

The builder uses an app-scoped environment under
`$XDG_CACHE_HOME/xc-buddy/build-venv`. The installer verifies the artifact and
creates an isolated runtime under
`$XDG_DATA_HOME/xc-buddy/releases/<build-id>`. It does not install packages into
the system Python environment. Launch XC Buddy from **Show Applications** for
desktop acceptance testing; direct source and editable-install launches are
development tools, not acceptance builds.

The builder also creates a runtime-specific release archive named like
`xc-buddy-linux-x86_64-py310-0.2.2.tar.gz` plus its checksum. For public `v*`
tags, the release workflow builds and attaches Python 3.10 and 3.12 archives
automatically. This enables **Check for App Updates...** for either supported
runtime on that Linux architecture. The installed app downloads the matching
archive, verifies GitHub's SHA-256 digest and every internal artifact checksum,
installs it as another immutable per-user release, switches the `current` link,
and restarts through the stable launcher.

Ubuntu normally provides the StatusNotifier host through its AppIndicator
extension. GNOME Shell owns the tray popup, including outside-click dismissal.
The Linux app uses native GTK windows for Settings, Pairing, firmware progress,
app updates, and device controls. App updates are enabled only for a standard
immutable per-user installation; source runs never modify their environment.

### 6. Complete first-run onboarding and pairing

When no configuration exists, XC Buddy opens onboarding automatically:

1. Select `XC-717C` from the discovered devices.
2. Enter the transcription API key locally.
3. Enable the platform's focused-app integration: Accessibility permission on
   macOS, or the clipboard and input helpers reported by `--doctor` on Linux.
4. Finish setup and confirm the menu bar or tray reports `XC-717C` as
   connected.

Configuration is stored separately from VoiceStick:

```text
macOS: ~/Library/Application Support/XC Buddy/config.toml
Linux: $XDG_CONFIG_HOME/xc-buddy/config.toml
       (normally ~/.config/xc-buddy/config.toml)
```

API keys and bridge or relay tokens are plain text in this local v0.2
configuration. Protect the file and never copy its secrets into the repository
or diagnostic output. `desktop/macos/Config/config.example.toml` documents the
available fields.

### 7. Run the end-to-end voice test

The verified focused-app test uses `hold_to_talk`, `focused_app`, `original`,
and `auto_enter = true`:

1. Open TextEdit or another controlled input target and click inside its text
   field.
2. Hold the StickS3 front button for longer than 0.5 seconds.
3. Speak a test sentence and release the button.
4. Wait for listening, thinking, and confirmation to finish without pressing
   another device button.
5. Confirm the transcription is pasted and Return is sent automatically.

With `auto_enter = true`, Return inserts a newline in TextEdit and sends the
message in chat applications. The front button pauses the confirmation
countdown and confirms it when pressed again; the side button cancels.

The August 5, 2026 macOS test successfully dictated, transcribed, pasted, and
sent two separate chat messages from `XC-717C`. That validates this complete
path on that tested Mac; Linux normal mode still requires its own real-device
qualification:

```text
StickS3 mic -> Opus -> BLE -> XC Buddy -> transcription
             -> focused-app paste -> Return/send
```

## Development checks

Keep Linux development dependencies inside a project-local environment:

```sh
python3 -m venv desktop/linux/.venv
desktop/linux/.venv/bin/python -m pip install -e desktop/linux ruff mypy
desktop/linux/.venv/bin/python -m unittest discover -s desktop/linux/tests -v
desktop/linux/.venv/bin/ruff format --check desktop/linux/app desktop/linux/tests
desktop/linux/.venv/bin/ruff check desktop/linux/app desktop/linux/tests
desktop/linux/.venv/bin/mypy --ignore-missing-imports desktop/linux/app
```

The test suite does not require Bluetooth hardware, a display, or external
network access. Local bridge and relay integration tests use loopback only;
they do not prove delivery to a remote receiver. Use `swift test
--package-path desktop/macos` for macOS source tests.

### Alternative firmware workflows

The following alternatives remain available but were not exercised during the
August 5 end-to-end test.

To use an existing native ESP-IDF 5.x environment instead of Docker:

```sh
cd firmware
. /path/to/esp-idf/export.sh
idf.py set-target esp32s3
idf.py build
```

For a full browser flash, open [ESP Launchpad](https://espressif.github.io/esp-launchpad/)
in Chrome or Edge, choose **DIY**, select
`dist/xc-buddy-sticks3-merged.bin`, set the flash address to `0x0`, connect the
StickS3, and program it.

## Interaction

- Front button: hold/click to record according to configuration; confirm pending paste.
- Side button: cancel or restore the most recent recoverable input.
- Device display: offline/booting, ready, listening, thinking, confirmation, Codex working, approval needed, done, and error states.
- Firmware `0.1.3` and later shows `Codex done` with its version for ten seconds, then returns to Ready.
- XC Buddy normally disconnects explicitly when it quits. As a crash-only fallback, it renews the BLE connection lease with one small heartbeat every 30 seconds; firmware releases a stale desktop BLE link after 90 seconds. Heartbeats do not wake the display or postpone the five-minute deep-sleep timer.
- Menu bar/tray summary: device connection, output target, Codex bridge status,
  and relay status.

See [Codex integration](docs/codex-integration.md) and [internal transcription](docs/internal-transcription.md) for exact local setup.

## Upstream attribution

XC Buddy v0.1 is derived from VoiceStick 0.3.4 by `78`. Existing protocol names
remain where they describe BLE compatibility; XC Buddy's website and firmware
release integration point only to `https://github.com/SiyiLi/xc-buddy/`.
