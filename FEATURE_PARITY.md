# Temporary macOS and Linux feature-parity checklist

This is a working qualification checklist, not permanent product
documentation. macOS is the behavioral reference unless both implementations
adopt a demonstrably better shared design. Delete this file after every item is
implemented and verified; durable usage and build instructions belong in
`README.md`.

## Aligned in source and automated checks

- Shared configuration fields, defaults, protocol UUIDs, interaction modes,
  output profiles, relay modes, device power controls, and per-device choices.
- Shared tray summaries and primary menu wording, including sender/receiver
  relay state and Bluetooth-paused sender behavior.
- Shared transcription onboarding and Settings wording.
- First launch is controlled by config-file existence on both platforms;
  onboarding keeps edits in memory and persists only when Finish succeeds.
- Settings, Pairing, and firmware window sizes and control order.
- Pairing cancellation does not replace the global runtime status with a scan
  status; Settings reloads persisted configuration whenever it opens.
- Linux device controls use an XC device window in the same order as the macOS
  nested device menu.
- Paired-device ordering and connected advertised names match macOS, with
  `XC-<ID>` used only when no connected advertised name is available.
- Listening, transcribing, confirmation, result, error, subtitle, and firmware
  progress surfaces.
- Shared main-menu, device, pairing, onboarding, settings, overlay, relay,
  transcription, translation, firmware-release, and firmware-transfer wording.
- Single-stick BLE lifecycle plus explicit sleep/wake recovery.
- Version display in Settings and platform-appropriate app update entry points.
- Immutable, isolated Linux build and per-user installation.
- Standard installed builds provide **Check for App Updates...** on both
  platforms. Linux verifies and installs a runtime-specific immutable release,
  switches the managed release link, and restarts through the stable launcher.

## Accepted platform substitutions

- Native AppKit and GTK controls may differ in texture and OS-standard details.
- GNOME tray menus retain one submenu level; a device item opens the equivalent
  GTK device window instead of a third-level tray menu.
- GNOME Shell owns tray font, popup placement, focus, and outside-click
  dismissal.
- macOS Accessibility permission maps to Linux clipboard and input-helper
  validation.
- Sparkle and GTK provide native update dialogs while both perform a verified
  in-place app update and restart.
- The Linux subtitle uses 46 px and macOS uses 46 pt, by explicit acceptance.

## Required acceptance evidence

- Build and run the macOS changes on macOS.
- Visually review the latest installed Linux build: tray, window layout,
  positioning, focus, stacking, and controls.
- Qualify Linux normal mode with a real XC device: pair, reconnect, record,
  Listening to Transcribing to result, confirm/cancel/restore, focused-app
  paste, restart, and persistence.
- Exercise subtitles, every per-device choice, firmware progress/cancel, and
  sleep/wake recovery. Confirm that a second paired stick cannot connect while
  one stick is active and that scanning resumes after disconnection.
- Exercise an automated tagged test release, confirm both Linux runtime
  archives are attached, and run the installed app's complete
  check/download/install/restart update path.
- Treat sender mode as externally observed only where a real receiver confirms
  the state change; local queues, sockets, and loopback tests are not delivery
  evidence.
