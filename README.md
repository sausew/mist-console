# MIST Console

A terminal-style desktop app for talking to Claude, built **from the ground up** so we own the entire UI. Instead of skinning the closed-source `claude` TUI, the Console runs the official `claude` binary **headlessly** and renders every pixel of the interface ourselves.

## Why this architecture

`claude`'s interactive TUI is a compiled binary we can't restyle. But the CLI exposes a full bidirectional streaming protocol:

```
claude -p --input-format stream-json --output-format stream-json --include-partial-messages --verbose
```

So we run Claude as a long-lived subprocess that emits structured JSON events (init/capabilities, token-level text + thinking deltas, tool calls, tool results, usage, rate limits) and we render them however we want. The **brain stays Anthropic's** — we never reimplement it — so capability and correctness come for free, and we get total control of the surface.

This hits the design criteria:
- **Fewest catastrophic errors** — leans on the official engine; one runtime; pure Python + web.
- **All the CLI capabilities** — tools, MCP servers, skills, slash commands, `/resume`, persona — all flow through the subprocess.
- **Easy to bolt features on** — Flask + plain web UI.
- **Low RAM** — one Python process + the system WebView (WKWebView). No Chromium, no Node. ~60–100MB vs Electron's 250MB+.

## Architecture

```
┌────────────────────┐     POST /send      ┌──────────┐   stdin (stream-json)   ┌────────────┐
│  WKWebView UI       │ ──────────────────▶ │  Flask   │ ─────────────────────▶ │  claude    │
│  (static/, app.js)  │ ◀── SSE /stream ─── │  app.py  │ ◀── stdout (events) ─── │  (headless)│
└────────────────────┘                      │ bridge.py│                         └────────────┘
                                            └──────────┘
```

- `bridge.py` — `ClaudeSession`: spawns/owns one headless `claude` process (cwd = Exobrain harness, MIST persona appended via `--append-system-prompt-file`), parses its stdout JSON, fans events out to subscribers. Robust subprocess lifecycle, fails loudly.
- `app.py` — Flask. `POST /send` writes a user turn to claude's stdin; `GET /stream` is Server-Sent Events of the live event stream; `POST /new` restarts the session.
- `static/` — the UI we fully control: `index.html`, `style.css` (flat/sharp MIST Cloud theme), `app.js` (renders streaming text, collapsible thinking, tool cards + results, capabilities panel, usage).
- `desktop.py` — native macOS window via pywebview/WKWebView. `uv run --script desktop.py`.
- `make-app.sh` — builds `~/Desktop/Apps/MIST Console.app`.

## Run

```sh
# native window
uv run --script desktop.py
# or browser dev mode
uv run --with flask python app.py   # http://127.0.0.1:5014
```

Port: **5014**.

## Auth: subscription, not API

The Console spawns the official `claude` binary, which authenticates with the **Claude subscription via OAuth** (`apiKeySource: none`, no `ANTHROPIC_API_KEY`, API overage disabled). No Anthropic API key, no per-token API billing. The usage numbers below are the subscription's own rate-limit windows — the same data the statusline shows — so displaying them costs nothing.

## Tabs (multi-session) + persistence

The left rail is a stacked list of independent conversations, each backed by its own headless `claude` process. `+ new chat` spawns one; `×` closes it (deletes its data); the `◆/◇` toggle pins it; clicking switches. Typing **`/new`** in the composer (and Enter) does the same as `+ new chat` from the keyboard; `/new <text>` opens a fresh chat and seeds it with that first message. It's a Console-local command, intercepted in `sendActive()` and never forwarded to Claude (distinct from the harness `slash_commands`, which do reach claude).

- **Sorted newest-first**, pinned conversations on top (with a divider).
- **Persistent.** Every event is recorded to `data/<id>.jsonl`; metadata (title, pinned, last_activity, claude session id) to `data/sessions.json`. On connect, `/stream/<id>` replays the full transcript, so reload / tab-switch / app-restart all show history.
- **Dormant revival.** On restart, conversations load as dormant (transcript visible, *no* process). The claude process spawns lazily on the first send, with `--resume <claude_session_id>` so the model's context is restored too (verified: a revived chat still remembers earlier turns). A watchdog retries fresh if a resumed session fails to start.
- The user bubble is rendered from a broadcast `user_text` event (not optimistically), so live and replayed transcripts are identical.

Backend: `/sessions` GET/POST, `/sessions/<id>` DELETE, `/sessions/<id>/pin` POST, `/stream/<id>`, `/send/<id>`. `data/` is gitignored (personal conversation history).

## Usage metrics (top bar)

- **ctx %** — context window used for the *active* tab, computed live in `bridge.py` from the latest assistant message's usage (`input + cache_read + cache_creation`) ÷ the model's `contextWindow`, broadcast as a `context` event. Uses the per-message usage (a single API call = current context occupancy), not the `result` event's turn-cumulative total, which sums every internal tool-call round trip and reads past 100%.
- **5h %** and **7d %** (with reset countdown / days remaining) — account-level rate limits read from `~/.claude/usage-cache.json` via `/usage` (polled every 45s). That cache is written by the statusline (`statusline-command.sh` tees its payload), so it refreshes whenever any interactive session renders. Shows a "cache Nm old" hint if stale. No API cost.

## Composer & boot

- **Full text editing** — a native macOS Edit menu (built in `desktop.py` via pyobjc) wires Cmd+X/C/V/A/Z and the right-click menu to the web view. Click-drag selection works natively. (WKWebView has no clipboard shortcuts without this menu.)
- **File picker** — the `file` button opens the native open dialog (`window.pywebview.api.pick_file`) and inserts the chosen absolute path(s) into the input, so MIST can `Read` them. Browser dev mode falls back to a hidden file input (filenames only).
- **Spoken boot greeting** — on launch MIST speaks one of several in-character greetings (`GREETINGS` in `app.py`) in her cloned voice, and shows it in the log. The greetings are **pre-rendered** to `greetings/greet_N.wav` so playback is instant (no ~28s TTS cold start). To change them: edit `GREETINGS`, start the voice service, and re-render the WAVs (index-aligned).

## Permissions (v1 caveat)

v1 runs the session with `--dangerously-skip-permissions` (same as the harness already runs headless) so tools execute without a permission round-trip the UI doesn't render yet. **Phase 2** replaces this with interactive permission cards + a mode switcher (default / acceptEdits / plan / bypass). See the roadmap.

## MCP parity with the CLI

The session loads **all** MCP scopes (no `--strict-mcp-config`), exactly like the interactive `claude` CLI: things3, fitbit, withings, linkedin, and the claude.ai connectors (Gmail/Calendar/Drive/MyChart). 8 servers, ~90 MCP tools.

**Important:** `init` (with the server/tool list) doesn't fire until the **first user message** — claude's stream-json mode is request-driven. So a brand-new chat shows `—` for model/MCP until you send something; after the first message everything populates (and the settings panel caches the last-known set). This is normal, not a hang.

## Quick access (always-on, even when MIST is closed)

Double-tap the **Option (⌥)** key to summon the glowing quick-entry overlay from anywhere; type + Enter starts a new chat. Attach the current page **URL** (🔗) or a **screenshot** selection (⛶), and use the **conversation picker** (⤷, or press ↓ on an empty input) to drop the message + attachments into an existing chat instead of a new one — the overlay grows upward to show a searchable, pinned-first list, and the main window slides straight into the chosen conversation.

The gesture is owned by a tiny windowless background agent (`mist-hotkey-agent.py`), **not** by MIST herself — so it works even when MIST is fully quit. On the gesture: if MIST is running it POSTs `/show-quick`; if she's closed it `open`s her, waits for her to bind, then summons the overlay. The agent runs as a LaunchAgent (`com.exobrain.mist-hotkey-agent`, RunAtLoad + KeepAlive) installed by `install-agent.sh` — always on, starts at login.

- Needs macOS **Accessibility** permission for the agent (global modifier monitoring). The agent self-requests it on first run.
- The overlay joins all Spaces / floats over fullscreen apps, so it appears on whatever Space you're on.
- Enable/disable + the gesture live in MIST **settings → quick access** (the agent re-reads the config every 10s).
- Uninstall: `launchctl unload ~/Library/LaunchAgents/com.exobrain.mist-hotkey-agent.plist`.

## Roadmap (bolt-on order)

1. **Interactive permissions** — render permission requests as Allow/Deny cards; mode switcher in the top bar.
2. **Slash-command palette** — `/` autocomplete from the init `slash_commands` list.
3. **Interrupt / stop** — cancel an in-flight turn.
4. **Diff viewer** — pretty Edit/Write tool inputs as diffs.
5. **Image paste + @-file mentions.**
6. **MIST voice** — speak responses via `mist-voice`; reactive avatar.
7. **Session list / resume** via `--resume <session_id>`.

## Dependencies

- `claude` CLI at `~/.npm-global/bin/claude` (provides the stream-json protocol).
- `uv` (self-installs `flask`, `pywebview`/`pyobjc` on first run).
- Reuses `mist-terminal/mist-persona.md` from the harness for MIST's voice.

## Privacy / legibility

No personal data in this project. The session runs in the Exobrain harness (which has its own privacy rules); this app is just transport + UI.
