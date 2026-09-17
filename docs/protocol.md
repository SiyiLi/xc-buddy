# XC Buddy Protocol

This document describes the protocol implemented by the current firmware and macOS desktop app.

## Goals

- Low-latency push-to-talk audio from StickS3 to macOS.
- Opus over BLE to keep wireless bandwidth low.
- Ogg Opus forwarding from macOS to NVIDIA Inference.
- Final ASR text insertion into the focused macOS input field after release and confirmation.

## BLE GATT

Device name: `XC-XXXX`, where `XXXX` is derived from the last two bytes of the device eFuse MAC.

Service UUID:

```text
8f2f0b84-6e6f-4b23-88f7-3a3ceafc5100
```

Characteristics:

| Name | UUID | Direction | Properties |
| --- | --- | --- | --- |
| `audio_tx` | `8f2f0b84-6e6f-4b23-88f7-3a3ceafc5101` | StickS3 -> Mac | notify |
| `state_tx` | `8f2f0b84-6e6f-4b23-88f7-3a3ceafc5102` | StickS3 -> Mac | notify |
| `control_rx` | `8f2f0b84-6e6f-4b23-88f7-3a3ceafc5103` | Mac -> StickS3 | write without response |
| `ota_rx` | `8f2f0b84-6e6f-4b23-88f7-3a3ceafc5104` | Mac -> StickS3 | write, write without response |
| `ota_tx` | `8f2f0b84-6e6f-4b23-88f7-3a3ceafc5105` | StickS3 -> Mac | notify |

The desktop app scans for this service and only connects to devices whose `XC-XXXX` ID is present in the local paired-device list. Multiple paired devices may be connected at the same time; audio, state, control, and OTA handling are scoped by CoreBluetooth peripheral identity.

## Audio Frame

All multibyte fields are little-endian.

```text
struct AudioBleFrame {
  uint8_t  version;       // 1
  uint8_t  type;          // 0x01 audio
  uint16_t header_len;    // 16
  uint32_t session_id;
  uint32_t seq;
  uint8_t  flags;         // bit0=start, bit1=end
  uint8_t  reserved;      // currently 0
  uint16_t payload_len;
  uint8_t  payload[payload_len];
}
```

The payload contains one raw Opus packet when `payload_len > 0`. The firmware currently encodes 60 ms of 16 kHz mono audio per packet. When recording stops, the firmware also sends an end frame with `flags & 0x02` and an empty payload.

The macOS app wraps incoming Opus packets into an Ogg Opus stream before sending them to ASR. It does not decode Opus to PCM.

## State Event

All multibyte fields are little-endian.

```text
struct StateBleFrame {
  uint8_t  version;       // 1
  uint8_t  type;          // 0x10 state
  uint16_t payload_len;
  uint8_t  json[payload_len];
}
```

State events report device facts from the firmware to the app. They do not carry
business actions such as "cancel" or "confirm"; the app owns that interpretation.

Currently emitted state events:

```json
{"event":"device_info","hardware":"stick_s3","firmware_version":"0.2.2","buttons":["primary","secondary"],"interaction_modes":["hold_to_talk","click_to_talk"],"ui_states":["ready","recording","thinking","pending_confirmation","error"]}
{"event":"button_down","button":"primary","session_id":1234}
{"event":"button_up","button":"primary","duration_ms":620,"session_id":1234}
{"event":"button_down","button":"secondary"}
{"event":"button_up","button":"secondary","duration_ms":90}
```

Buttons are named by role instead of physical placement. On StickS3, the front
button maps to `primary` and the side button maps to `secondary`. `session_id` is
included when a `primary` press starts or stops a local audio recording.

Deprecated firmware-to-app events:

| Event | Replacement | Reason |
| --- | --- | --- |
| `press_start` | `button_down` with `button:"primary"` | The old name assumed the front button and implied recording semantics. |
| `press_end` | `button_up` with `button:"primary"` | The old name implied recording semantics and did not include a button role. |
| `cancel` | `button_down` / `button_up` with `button:"secondary"` | The old event encoded app meaning; the same button can cancel, restore, or be ignored depending on app state. |

## Control Event

The Mac writes compact JSON to `control_rx`. Control events are authoritative UI
state from the app to the firmware display.

Current desktop events:

```json
{"event":"ui_state","state":"ready","text":"","background_state":"ready"}
{"event":"ui_state","state":"recording","text":""}
{"event":"ui_state","state":"thinking","text":"partial text"}
{"event":"ui_state","state":"pending_confirmation","text":"final text"}
{"event":"ui_state","state":"codex_working","text":"Codex is working"}
{"event":"ui_state","state":"approval_needed","text":"Approval needed"}
{"event":"ui_state","state":"codex_done","text":"Turn complete"}
{"event":"ui_state","state":"error","text":"ASR timeout"}
{"event":"interaction_mode","mode":"hold_to_talk"}
{"event":"interaction_mode","mode":"click_to_talk"}
{"event":"codex_success_chime","enabled":true}
{"event":"codex_notification_chime"}
{"event":"power_timers","dim_seconds":30,"screen_off_seconds":300,"idle_sleep_seconds":300,"codex_sleep_seconds":900}
{"event":"heartbeat"}
{"event":"disconnect"}
```

The desktop helper always includes a `text` field, even for states without text
content. Firmware may immediately render local physical feedback, such as
showing the recording cat when the primary button starts audio, but the app's
`ui_state` is the authoritative display state. XC Buddy applies each button
event to its authoritative state and sends only the resulting state to the
device. Reconnection uses the same XC Buddy-owned state; firmware-local state
is never treated as the source of truth. Current StickS3 firmware does not
render recognition text on-device because the LVGL font set does not include
Chinese glyphs; `text` is used only to choose fixed English hints.

Completion and error notices are live-only. If the Stick is disconnected or in
deep sleep when one occurs, XC Buddy does not queue or replay it.

For `ready`, `background_state` records XC Buddy's underlying Codex state:
`ready`, `codex_working`, or `approval_needed`. The Stick initially renders
Ready after a front-button interaction, then uses this value at the next display
dim transition instead of assuming that Ready always means Resting.

`interaction_mode` controls the front-button behavior and idle screen hint.
`hold_to_talk` starts audio on primary down and stops on primary up.
`click_to_talk` starts audio on the first primary click and stops on the next
primary click.

`codex_success_chime` enables or disables Codex sounds on the StickS3. The
desktop app sends the persisted XC Buddy setting whenever the device connects.
`codex_notification_chime` plays the configured sound immediately without
changing the display state.

`power_timers` updates all inactivity stages in seconds. XC Buddy validates and
persists the values, then sends them whenever the device connects or settings
change. The default timers are 30 seconds to dim, 5 minutes to turn the display
off, 5 minutes to deep-sleep while idle, and 15 minutes to deep-sleep while
Codex is Working or awaiting Approval.

`heartbeat` renews the application-level connection lease without changing UI
or power-management state. `disconnect` asks the peripheral to release the BLE
link immediately during a normal app shutdown.

In firmware `0.1.3` and later, `codex_done` displays the running firmware
version for ten seconds and then returns the device to `ready` automatically.

Deprecated app-to-firmware events:

| Event | Replacement | Reason |
| --- | --- | --- |
| `connected` | `ui_state:ready` | Connection is not a display state after pairing. |
| `partial` | `ui_state:thinking` with `text` | Partial text is display content for the thinking state. |
| `final` | `ui_state:pending_confirmation` with `text` | Final text is still cancellable until pasted. |
| `paste_done` | `ui_state:ready` | Once pasted, the device returns to ready. |
| `paste_cancelled` | `ui_state:ready` | Once cancelled, the device returns to ready. |
| `error` | `ui_state:error` with `text` | Errors are another UI state. |

## BLE OTA

The firmware uses a custom OTA channel over the same Voice Stick service. The macOS app writes OTA `begin` and `end` frames with BLE write-with-response, and streams OTA `data` frames with write-without-response using CoreBluetooth flow control.
The device sends progress notifications roughly every 32 KB of accepted firmware data.

XC Buddy discovers published firmware anonymously from this repository's latest
public GitHub Release. It accepts only `xc-buddy-sticks3-ota.bin`, verifies the
GitHub asset size and SHA-256 digest, and then transfers it through this BLE OTA
channel. The merged image remains restricted to manual USB flashing.

The 8 MB flash layout uses two 3 MB OTA app slots and keeps the remaining flash as a reserved SPIFFS data partition:

| Name | Offset | Size |
| --- | ---: | ---: |
| `ota_0` | `0x10000` | 3 MB |
| `ota_1` | `0x310000` | 3 MB |
| `storage` | `0x610000` | 1984 KB |

All multibyte fields are little-endian.

```text
struct OtaBeginFrame {
  uint8_t  version;       // 1
  uint8_t  type;          // 0x20 begin
  uint16_t header_len;    // 12
  uint32_t image_size;
  uint32_t transfer_id;
}

struct OtaDataFrame {
  uint8_t  version;       // 1
  uint8_t  type;          // 0x21 data
  uint16_t header_len;    // 12
  uint32_t transfer_id;
  uint32_t offset;
  uint8_t  payload[];
}

struct OtaEndFrame {
  uint8_t  version;       // 1
  uint8_t  type;          // 0x22 end
  uint16_t header_len;    // 12
  uint32_t transfer_id;
  uint32_t image_size;
}

struct OtaAbortFrame {
  uint8_t  version;       // 1
  uint8_t  type;          // 0x23 abort
  uint16_t header_len;    // 8
  uint32_t transfer_id;
}
```

`ota_tx` sends a state frame:

```text
struct OtaStateFrame {
  uint8_t  version;       // 1
  uint8_t  type;          // 0x30 OTA state
  uint16_t payload_len;
  uint8_t  json[payload_len];
}
```

OTA state events include:

```json
{"event":"ready","transfer_id":1,"size":1385760,"partition":"ota_1"}
{"event":"progress","transfer_id":1,"written":32768,"size":1385760}
{"event":"done","transfer_id":1,"reboot_ms":500}
{"event":"error","code":"bad_offset","esp_err":258}
{"event":"aborted"}
```

On the device display, OTA switches the normal idle/recording UI into an update state:

- `Updating` with percentage while the image is being written.
- `Rebooting` after the new boot partition is selected.

While OTA is active, the device ignores push-to-talk input and pauses display dimming/deep sleep timers. After a successful transfer, the firmware waits about 500 ms after sending the `done` event and then calls `esp_restart()`.
The desktop updater can cancel an in-progress transfer by sending `OtaAbortFrame`; the device aborts the OTA handle and keeps booting the current firmware.

## Runtime State Machine

StickS3:

```text
boot -> advertising -> connected -> idle -> recording -> idle
```

The firmware dims any non-recording, non-OTA display after the configured dim
delay while preserving the current scene. Only a connected `ready` scene
changes its label to `Resting`; Working, Approval, error, and offline scenes
remain identifiable at the lower brightness. It turns the LCD panel and
backlight off after the configured screen-off delay. On battery power it enters
deep sleep after the idle or Codex-specific delay; Codex Working and Approval
therefore cannot keep the device awake indefinitely. Charging or USB power
suppresses deep sleep but not the screen-off timer. Any new state or button
activity turns the display back on. The front button wakes the device from deep
sleep. XC Buddy retains only the latest persistent lifecycle state while the
Stick sleeps; transient completion and error notices are not replayed.

macOS:

```text
needs_pairing -> scanning -> ready -> recording -> thinking -> pending_confirmation -> ready
```

During recognition and confirmation, the firmware keeps showing the thinking cat
until the app sends `ui_state:ready`. During pending confirmation, `primary`
confirms or pauses according to the app's internal countdown mode, and
`secondary` cancels. When idle, `secondary` restores the last recoverable input
confirmation. Cancelling that restored confirmation returns to the active
Codex state when Codex is still Working or awaiting Approval, instead of
resetting the Stick to Ready. These meanings are app state-machine behavior,
not firmware protocol events.

Recordings shorter than 0.5 seconds are discarded locally and are not sent to ASR.

## ASR Transport

The desktop app buffers incoming Ogg/Opus audio and sends it to the NVIDIA
Inference OpenAI-compatible chat-completions endpoint after recording ends.
Audio is base64 encoded in an `input_audio` content item:

```json
{
  "model": "gcp/google/gemini-3.6-flash",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "input_audio", "input_audio": {"data": "<base64 Ogg/Opus>", "format": "ogg"}},
      {"type": "text", "text": "<transcription prompt and hotwords>"}
    ]
  }]
}
```

Recordings shorter than 0.5 seconds are discarded before an inference request.
The transcript is read from `choices[0].message.content`. See
`docs/internal-transcription.md` for the complete request contract.
