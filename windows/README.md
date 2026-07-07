# MIST Console for Windows

A self-contained Windows build of the MIST Console: the same UI (shared
`static/`), running the official `claude` CLI headlessly over stream-json,
packaged as a single `MIST Console.exe` with a first-run setup wizard. Built
for sharing with friends; it contains no personal data, no API keys, and no
Mac-specific machinery.

## What's in the exe

- **The Console** (`app_win.py` + `bridge_win.py` + `desktop_win.py`):
  Windows ports of the macOS originals. Same routes, same front-end, same
  multi-session persistence (under `%APPDATA%\MIST Console`). WebView2
  window via pywebview; falls back to the default browser if WebView2 is
  missing. Solarpunk is the default theme.
- **Setup wizard** (`setup.html`): served at `/` until setup completes.
  Detects/installs Claude Code (official PowerShell installer), checks Git
  for Windows, runs `claude auth login` with streamed output, picks a
  workspace, writes a starter pack (CLAUDE.md + the public de-ai skill),
  and optionally stores a Pollinations key.
- **Image generator** (`mist_image_win.py`): the harness mist-image CLI
  ported and built into the exe, so no Python install is needed:
  `"MIST Console.exe" image "a foggy harbor at dawn"`. Output lands in
  `Pictures\MIST Gallery`, which the `/file` route serves inline in chat.
  Needs the user's own free key (https://auth.pollinations.ai) or Cloudflare
  Workers AI credentials.

## What was left out (macOS-only)

Quick-access hotkey overlay, AirDrop router, launchd routines scheduler,
voice greetings, Dock/menu integration. The shared front-end probes those
routes; `app_win.py` answers with inert stubs so the panels degrade cleanly.

## Windows-specific gotchas encoded here

- All claude subprocesses run with `encoding="utf-8"` (Windows text mode
  defaults to cp1252, which corrupts the JSON stream) and
  `CREATE_NO_WINDOW` (else every backend flashes a console window).
- The OAuth usage probe reads `~/.claude/.credentials.json` (no Keychain on
  Windows).
- Model discovery scans the claude binary with a chunked pure-Python regex
  (no `grep`); an npm-shim install scans `cli.js` instead.
- In a `--noconsole` PyInstaller exe, `sys.stdout` is None unless the caller
  redirects it; `main_win.py` guards that so the `image` subcommand can't
  crash when double-clicked.

## Build

GitHub Actions (`.github/workflows/windows-exe.yml`) builds on
`windows-latest` with PyInstaller and uploads a `MIST-Console-Windows`
artifact. Trigger it manually (`gh workflow run windows-exe.yml`) or push
anything under `windows/` or `static/`.

## Run from source (on Windows)

```
pip install flask pillow pywebview pythonnet
python windows\main_win.py
```
