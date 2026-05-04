# AGENT.md

## Project Summary

This repository is a local Android dynamic analysis workbench built around:

- `ADB`
- `root` / `su`
- `frida-server`
- `radar.dex`
- reusable Frida JavaScript scripts

It provides two user-facing entry points:

- GUI: `app_gui.py`
- CLI: `hookers.py`

At the current stage of the repository, the GUI is the recommended primary entry point for understanding and using the project. The CLI still matters, but the public-facing docs and recent improvements are centered on the GUI workflow.

The project is primarily for working against one target Android app at a time, then creating a per-package local workspace under `workspaces/` for scripts, helper batch files, APK pulls, and hook outputs.

## Important Scope

When understanding or modifying this project:

- Treat `workspaces/com.secret.prettyhezi/` as out of scope unless the user explicitly asks for it.
- That directory is a per-app workspace, not part of the core framework architecture.
- Focus on `core/`, `ui/`, top-level Python entry files, `js/`, and `mobile-deploy/`.
- Treat `docs/images/` as documentation assets for the GitHub README, not runtime code.

## High-Level Architecture

The codebase is split into:

- orchestration entrypoints
- shared state models
- service layer
- GUI layer
- Frida JS assets
- device deployment assets

### Entrypoints

- `hookers.py`
  - CLI shell for device bootstrap, app selection, attach/spawn, RPC-style inspection commands, and hook generation.
- `app_gui.py`
  - PySide6 GUI entrypoint.
  - Builds the shared context and injects service dependencies into the main window.
  - Explicitly enables persistent RPC reuse for GUI actions.

### Shared State

- `core/models.py`
  - `AppRecord`: app list item from device enumeration.
  - `AppContext`: full context for the currently selected target app.
  - `HookSession`: current active Frida session/script.
  - `HookerContext`: repository-wide shared runtime state.

`HookerContext` is the center of the app. Services do not maintain isolated copies of state; they collaborate through this shared context.

## Core Services

### `core/device_service.py`

Responsible for device and runtime preparation:

- connect to ADB device
- detect root / Magisk
- determine CPU architecture
- start `frida-server`
- deploy `radar.dex`
- enumerate installed/running applications
- bring target app to foreground
- prepare `AppContext`

This is the service that bridges local Python logic with the Android device.

### `core/workspace_service.py`

Responsible for local per-package workspace management:

- create `workspaces/<package>/` workspace directory
- create `workspaces/<package>/js/`
- copy built-in JS templates from global `js/`
- generate helper bat files like `attach.bat`, `spawn.bat`, `hooking.bat`
- pull target APK into the workspace
- resolve script paths
- persist decrypted output sent back from Frida scripts

This service is what turns the repository into a reusable “one app, one workspace” workbench.

### `core/session_service.py`

Responsible for Frida session lifecycle:

- attach to current process
- spawn target process and attach early
- load script source
- prepend common console bridge and wrapping JS
- handle script messages
- stop and clean up active session
- restart current app

It owns the active Frida session stored in `HookerContext.active_session`.

### `core/rpc_service.py`

Responsible for RPC-style Frida interactions through `js/rpc.js`:

- attach/load RPC script for inspection and hook generation
- call exported RPC methods
- query Activity / Service / Object / View information
- generate hook scripts into the current app workspace
- optionally start a device-side HTTP service

This service is used by both CLI debug commands and GUI utility actions.
CLI keeps the original short-lived behavior; GUI enables a reusable persistent RPC session to avoid repeated attach/load/detach on every button click.

## GUI Layer

### `ui/main_window.py`

The GUI is not a separate architecture. It is mainly a visual orchestrator over the same services used by the CLI.

Main responsibilities:

- choose script directory
- prepare device environment
- refresh app list
- choose target app
- initialize workspace
- choose attach/spawn mode
- start and stop hook session
- run RPC utility actions
- display logs

Important recent GUI behavior:

- GUI utility actions reuse a persistent RPC session through `RpcService`
- when a valid active hook session already exists, GUI inspection actions reuse the current app context instead of always forcing a new foreground check
- the middle “debug tools” panel is a key surface for script generation, object inspection, and Activity/Service queries

The GUI uses background workers to avoid blocking the Qt main thread during ADB/Frida operations.

### `ui/workers/`

- `device_worker.py`
  - bootstrap device environment and refresh app list
- `workspace_worker.py`
  - ensure target app foreground state and initialize workspace
- `hook_worker.py`
  - prepare app context and start attach/spawn
- `action_worker.py`
  - execute generic one-shot GUI actions asynchronously

## JS Assets

Global Frida scripts live under `js/`.

Important categories:

- network inspection and okhttp hooks
- root / VPN / trust bypass helpers
- UI interaction helpers
- dex dumping / keystore dumping
- RPC support
- hook generation wrappers

Especially important files:

- `js/rpc.js`
  - exported RPC methods used by `RpcService`
- `js/_hook_js_prepare.js`
  - prefix template for generated hooks
- `js/_hook_js_enhance.js`
  - extra helper logic appended to generated hooks
- `js/_hook_js_warp.js`
  - common wrapping code appended when scripts are loaded

## Device Assets

`mobile-deploy/` contains artifacts pushed or used against the Android device, including:

- `frida-server` binaries
- `radar.dex`
- helper native/network binaries

These are part of runtime deployment, not business logic.

## Typical Runtime Flow

### Recommended flow

1. Run `python app_gui.py`
2. Click “准备环境并刷新 App”
3. Select target app
4. Optionally initialize the workspace and pull APK
5. Choose script or generate a hook script
6. Start attach/spawn injection
7. Use GUI debug tools to inspect Activity / Service / Object / View state

### CLI flow

1. Run `python hookers.py`
2. Bootstrap device environment
3. Refresh app list
4. Select package name
5. Ensure app is running/in foreground or prepare spawn context
6. Ensure local workspace
7. Attach or spawn with selected script
8. Optionally run RPC-style inspection and hook-generation commands

### GUI flow

1. Run `python app_gui.py`
2. Build `HookerContext`
3. Construct service instances
4. Inject them into `MainWindow`
5. Use GUI actions to prepare environment, choose app, initialize workspace, and start hook session
6. Reuse persistent RPC calls for high-frequency inspection buttons

## Repository Layout To Prioritize

When a new conversation needs to understand this repo quickly, read files in roughly this order:

1. `README.md`
2. `app_gui.py`
3. `ui/main_window.py`
4. `core/models.py`
5. `core/device_service.py`
6. `core/workspace_service.py`
7. `core/session_service.py`
8. `core/rpc_service.py`
9. `hookers.py`

## Environment Assumptions

The project assumes:

- Windows host environment
- Python 3.12 or 3.13
- `adb` installed and available
- Android device available over ADB
- target device has root access
- correct `frida-server` binary available for device architecture

Python dependencies are listed in `requirements.txt`, mainly:

- `frida`
- `frida-tools`
- `adbutils`
- `PySide6`
- `prompt_toolkit`
- `jsbeautifier`

## Maintenance Notes

- The codebase is already partially refactored from a single-script design into services plus CLI/GUI shells.
- The GUI file `ui/main_window.py` is large and contains signs of iterative migration.
- Per-package directories under `workspaces/` are generated workspaces and should not be mistaken for core framework modules.
- `ui/main_window.py` still contains a large legacy comment block with retired helper methods, so future cleanup should distinguish active code from migration leftovers.
- `README.md` now contains substantial product-facing guidance, screenshots, GIF demos, and GUI tool explanations; keep `AGENT.md` aligned with that high-level positioning.

## Practical Guidance For Future Agents

- If the user asks “what is this project,” describe it as an Android Frida/ADB workbench with per-app workspaces under `workspaces/`.
- Default to explaining the GUI workflow first unless the user explicitly asks about the CLI.
- If the user asks to modify behavior, determine first whether the change belongs in:
  - device preparation
  - workspace creation
  - session lifecycle
  - RPC tooling
  - GUI orchestration
- If the user is asking about screenshot-visible controls, inspect `ui/main_window.py` and `README.md` together because the README now documents the intended GUI usage surface.
- Prefer reading `core/` first before changing `ui/`.
- Do not treat `workspaces/com.secret.prettyhezi/` as core source code unless explicitly requested.
