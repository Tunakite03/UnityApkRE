---
name: capture-device-logs
description: Capture, stream, and filter runtime Android logs from a connected USB device running a Unity game. Reads full logcat buffers (-b all -v threadtime), Unity engine debug output, C# exceptions, native crash stack traces, IL2CPP init, scene loads, and SDK diagnostics. Use when the user says /unity-apk-re:capture-device-logs, "đọc log unity", "xem log android", "adb logcat unity", "read game logs", "debug crash on phone", or needs runtime observations.
argument-hint: '[<package id>] [--dump] [--grep <regex>] [--unity-only] [--out <file>]'
allowed-tools: Read, Bash
---

# Capture runtime Unity logs from a connected Android device

Wrapper: `scripts/uare logcat` (at repository root).
Direct adb command: run in user terminal outside sandboxes.

## 1. Understanding `adb` flags & Unity logging

- **`adb -a` vs `logcat`**: `-a` is an `adb` server flag telling the adb daemon to listen on all network interfaces (e.g. `adb -a nodaemon server`). It is not a logcat flag, and `unity` is not an adb command.
- **`adb logcat -v time -s Unity`**: Filters exclusively to the `Unity` log tag. While this captures `Debug.Log`, `LogError`, and managed C# exceptions, it drops all other subsystem logs (Firebase, native C/C++ crashes, Android SDK runtime, memory warnings, tombstone dumps).
- **Core strategy**: Capture all buffers (`-b all`) with thread and process context (`-v threadtime`) first to preserve full runtime evidence, then filter during post-analysis rather than dropping events at capture time.

## 2. Standard commands

### A. Raw terminal commands (run in your terminal)

1. **Clear buffer before starting a test run**:
   ```bash
   adb logcat -c
   ```
2. **Stream full logs to file (Ctrl+C to stop)**:
   ```bash
   adb logcat -b all -v threadtime > unity_full.log
   ```
3. **Dump buffered logs non-interactively (without streaming)**:
   ```bash
   adb logcat -b all -v threadtime -d > unity_dump.log
   ```
4. **Post-analysis filtering (search for key game & subsystem events)**:
   ```bash
   grep -iE "unity|firebase|perbase|crash|exception" unity_full.log
   ```
5. **Quick tag-only inspection**:
   ```bash
   adb logcat -v time -s Unity
   ```
6. **Full device diagnostic snapshot (deep troubleshooting)**:
   ```bash
   adb bugreport bugreport.zip
   ```

### B. Toolkit CLI commands (`scripts/uare logcat`)

The CLI automatically handles ADB binary resolution (`UARE_ADB`, vendored platform-tools, PATH) and device selection:

```bash
# Clear device logcat ring buffer
scripts/uare logcat -c

# Dump all buffers and save raw evidence to workspace
scripts/uare logcat --dump --out <workdir>/observations/unity_dump.log

# Dump and filter immediately via regex
scripts/uare logcat --dump --grep "unity|firebase|perbase|crash|exception"

# Stream full logs to file and print matches to terminal
scripts/uare logcat --follow --out <workdir>/observations/unity_stream.log --grep "unity|crash"

# Strict Unity tag filter only
scripts/uare logcat --unity-only --dump
```

## 3. Key technical details & caveats

- **`-b all`**: Captures all system log buffers: `main`, `system`, `crash`, `events`, and `radio`. The default logcat buffer only includes `main`, `system`, and `crash`.
- **`-v threadtime`**: Prepends date, invocation time, PID, and TID to each line. This is essential for differentiating background worker threads (JobSystem, UnityWebRequest, IL2CPP GC) from the main Unity thread.
- **Ring buffer limits**: The Android logcat buffer has a finite size (often 256 KB - 4 MB). For long game sessions, launch logcat streaming before opening the game to avoid dropping early startup logs.
- **`--pid` limitation**: While `--pid` restricts output to a single PID, it misses logs if the application crashes and relaunches or uses secondary worker processes. Prefer full buffer capture with post-grep filtering.
- **Sandbox execution**: Sandboxed environments may block the ADB daemon socket (`localhost:5037`) or USB device communication, causing ADB commands to hang. If an adb command hangs in a subshell or sandbox, execute the command directly in the host terminal.

## 4. Recording evidence for game reconstruction

Extract facts into `<workdir>/observations.json` following `skills/rebuild-game/SKILL.md`:
- Engine version: `Initialize engine version: 2022.3.10f1`
- Scene transition markers: `[SceneManager] LoadScene Level1`, `Loading scene ...`
- Asset bundle / Addressable download URLs: `DownloadHandlerAssetBundle`, `UnityWebRequest`
- Missing components / script GUIDs: `The referenced script on this Behaviour ... is missing!`
- Native crashes & signals: `Fatal signal 11 (SIGSEGV) at 0x... in libil2cpp.so`
