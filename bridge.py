"""
bridge.py — manages headless `claude` processes and relays their stream-json
event protocol to subscribers (the SSE endpoint), with per-session history
persistence so conversations survive reloads, tab switches, and app restarts.

A session can be DORMANT (loaded from disk, transcript visible, no process) and
starts its claude process lazily on the first send.
"""
import json
import os
import queue
import subprocess
import threading
import time

HARNESS = "/Users/alexhedtke/Documents/Exobrain harness"
CLAUDE = os.path.expanduser("~/.npm-global/bin/claude")
# Persona comes from the harness CLAUDE.md (auto-loaded via cwd=HARNESS), not a side file.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_PERMISSION_MODE = "bypassPermissions"
HISTORY_CAP = 8000   # max events kept in memory for replay (jsonl keeps all)

# Live rate-limit store, refreshed from each Console turn's rate_limit_event.
# WHY: the usage badges' % comes from ~/.claude/usage-cache.json, which only the
# interactive-CLI statusline refreshes. During Console-only use that cache goes
# stale and, once its window's resets_at passes, the front-end drops it and the
# 5h/7d badges go BLANK. The headless stream still emits rate_limit_event every
# turn carrying the live resets_at + status (but not the %), so we persist that
# here and /usage merges it — keeping a fresh reset countdown + blocked status
# (and preserving the cached % while the window hasn't rolled) so the badge
# stays populated whenever MIST has run at least one turn.
RATE_LIVE_PATH = os.path.join(DATA_DIR, "rate-live.json")
_rate_lock = threading.Lock()


def record_rate_limit(info):
    """Persist a live rate_limit_event's resets_at + status per window (atomic)."""
    t = (info or {}).get("rateLimitType")
    if t not in ("five_hour", "seven_day"):
        return
    rec = {"resets_at": info.get("resetsAt"), "status": info.get("status"),
           "ts": int(time.time())}
    if not rec["resets_at"]:
        return
    with _rate_lock:
        try:
            with open(RATE_LIVE_PATH) as f:
                d = json.load(f) or {}
        except Exception:
            d = {}
        d[t] = rec
        tmp = RATE_LIVE_PATH + ".tmp.%d" % os.getpid()
        try:
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, RATE_LIVE_PATH)
        except Exception:
            pass

# Scoped to Console sessions only (see _build_cmd). Keeps MIST from speaking
# aloud in a text chat; voice stays enabled everywhere else.
NO_VOICE_PROMPT = (
    "You are running inside the MIST Console, a text-only chat surface. "
    "Do NOT produce audio or speech here: never run mist-say, mist-notify, "
    "any mist-voice script, afplay, the `say` command, or anything else that "
    "plays sound or speaks aloud. Respond in text only. (Your voice tools "
    "remain available on other surfaces; they are simply disabled in this one.)"
)


class ClaudeSession:
    """One conversation: a persisted event history + (lazily) a claude process."""

    def __init__(self, id=None, title=None, pinned=False, claude_session_id=None,
                 last_activity=None, autostart=True, import_path=None,
                 permission_mode=DEFAULT_PERMISSION_MODE, model=None, cwd=HARNESS,
                 pin_order=0):
        self.id = id
        self.title = title
        self.pinned = pinned
        self.pin_order = pin_order   # manual sort position among pinned chats
        self.import_path = import_path   # Claude Code jsonl to lazily import on open
        self._import_done = False
        self.last_activity = last_activity or time.time()
        self.claude_session_id = claude_session_id   # for --resume after a restart
        self.permission_mode = permission_mode
        self.model = model
        self.cwd = cwd

        self.proc = None
        self.alive = False
        self.session_id = None
        self.last_init = None
        self.context_pct = None
        self._last_msg_usage = None  # usage of the latest assistant message (single-call snapshot)

        self.history = []
        self._jsonl = os.path.join(DATA_DIR, f"{id}.jsonl") if id else None
        self._subscribers = []
        self._lock = threading.Lock()
        self._hist_lock = threading.Lock()
        self._resume_tried = False
        self._started_at = 0.0
        self._saw_init = False
        self._intentional_stop = False

        self._load_history()
        if autostart:
            self.ensure_started()

    # ---- history persistence ----------------------------------------------
    def _load_history(self):
        if not self._jsonl or not os.path.exists(self._jsonl):
            return
        try:
            with open(self._jsonl) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.history.append(json.loads(line))
            if len(self.history) > HISTORY_CAP:
                self.history = self.history[-HISTORY_CAP:]
            # restore context % from the last context event we saw
            for obj in reversed(self.history):
                if obj.get("type") == "context":
                    self.context_pct = obj.get("pct")
                    break
        except Exception:
            pass

    def _record(self, obj):
        with self._hist_lock:
            self.history.append(obj)
            if len(self.history) > HISTORY_CAP:
                self.history = self.history[-HISTORY_CAP:]
        if self._jsonl:
            try:
                with open(self._jsonl, "a") as f:
                    f.write(json.dumps(obj) + "\n")
            except Exception:
                pass

    def snapshot_history(self):
        with self._hist_lock:
            return list(self.history)

    def ensure_imported(self):
        """First time an imported tab is opened, convert its Claude Code jsonl
        into display events (then they persist to data/<id>.jsonl like any chat)."""
        if self._import_done or not self.import_path:
            return
        self._import_done = True
        if self.history:
            return
        try:
            import importer
            for ev in importer.convert(self.import_path):
                self._record(ev)
        except Exception:
            pass

    # ---- process lifecycle ------------------------------------------------
    def _build_cmd(self):
        cmd = [CLAUDE, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        if self.permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        elif self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        # AskUserQuestion is an interactive picker the CLI resolves itself; in this
        # headless stream-json process there's no TTY, so it auto-dismisses with no
        # answer. Disable it so the model asks in plain text, which the user can
        # answer by typing a normal reply.
        cmd += ["--disallowed-tools", "AskUserQuestion"]
        # Full MCP parity with the interactive CLI: load every scope (user +
        # project + local) — the local stdio servers (things3/fitbit/withings),
        # linkedin (uvx @latest), and the claude.ai OAuth connectors
        # (Gmail/Calendar/Drive/MyChart). No --strict-mcp-config, so claude uses
        # its normal full resolution. The @latest/uvx servers resolve over the
        # network, so they connect a beat slower than the local ones.
        if self.claude_session_id and not self._resume_tried:
            cmd += ["--resume", self.claude_session_id]  # restore model context
        if self.model:
            cmd += ["--model", self.model]
        # MIST's persona is NOT injected from a side file. It lives in the
        # Exobrain's CLAUDE.md ("Identity & Voice: MIST"), which `claude`
        # auto-loads because we run in the harness cwd (see HARNESS / cwd below).
        #
        # Voice is OFF in the Console. CLAUDE.md grants MIST audio tools
        # (mist-say / mist-notify / mist-voice), and with bypassPermissions she
        # can run them unprompted — so she'd occasionally speak aloud mid-chat.
        # The Console is a text surface, so we scope a "no audio" instruction to
        # THIS session only. Other surfaces (news-briefing podcast, note
        # narration, the mist-terminal greeting) keep voice — we don't touch
        # CLAUDE.md.
        cmd += ["--append-system-prompt", NO_VOICE_PROMPT]
        return cmd

    def ensure_started(self):
        with self._lock:
            if self.alive:
                return
            env = dict(os.environ)
            env["PATH"] = (os.path.expanduser("~/.npm-global/bin")
                           + ":/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", ""))
            try:
                self.proc = subprocess.Popen(
                    self._build_cmd(), cwd=self.cwd, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1)
            except Exception as e:
                self._broadcast({"type": "process_exit", "code": -1, "error": str(e)})
                return
            self.alive = True
            self._started_at = time.time()
            self._saw_init = False
            threading.Thread(target=self._read_stdout, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()
            threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        code = self.proc.wait()
        self.alive = False
        if self._intentional_stop:           # close or model switch — not a crash
            self._intentional_stop = False
            return
        # If a --resume start died almost immediately without initializing, the
        # resumed session was probably invalid: retry once fresh.
        if (self.claude_session_id and not self._resume_tried and not self._saw_init
                and time.time() - self._started_at < 5):
            self._resume_tried = True
            self.claude_session_id = None
            self.ensure_started()
            return
        self._broadcast({"type": "process_exit", "code": code})

    def set_model(self, model):
        """Switch model. Goes dormant; next send revives with the new model and
        --resume (so conversation context carries over)."""
        self.model = model or None
        if self.alive:
            self._resume_tried = False
            self.stop()

    def set_permission(self, mode):
        """Switch permission mode. Goes dormant; next send revives with the new
        mode and --resume (so conversation context carries over)."""
        if not mode or mode == self.permission_mode:
            return
        self.permission_mode = mode
        if self.alive:
            self._resume_tried = False
            self.stop()

    def set_cwd(self, cwd):
        """Switch the working directory (repo MIST runs in). Goes dormant and
        starts FRESH in the new repo on the next send. We clear the resume id
        because a claude transcript is keyed to the cwd it was created in, so
        --resume can't carry a conversation across directories — and a different
        repo means a different CLAUDE.md/context anyway."""
        if not cwd or cwd == self.cwd:
            return
        self.cwd = cwd
        self.claude_session_id = None
        self._resume_tried = False
        if self.alive:
            self.stop()

    # ---- io ----------------------------------------------------------------
    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = {"type": "raw", "text": line}
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                self._saw_init = True
                self.session_id = obj.get("session_id")
                self.claude_session_id = obj.get("session_id")
                self.last_init = obj
            elif obj.get("type") == "assistant":
                # Each assistant message carries the usage of ONE API call — a true
                # snapshot of current context occupancy. Keep the latest for ctx %.
                # Skip subagent (sidechain) messages: they carry the SUBAGENT's
                # context, not this session's, and would skew ctx way low.
                mu = (obj.get("message") or {}).get("usage")
                if mu and not obj.get("parent_tool_use_id"):
                    self._last_msg_usage = mu
            elif obj.get("type") == "result":
                self.last_activity = time.time()
                self._emit_context(obj)
            elif obj.get("type") == "rate_limit_event":
                # Keep the usage badges' reset/status fresh during Console-only
                # use (see RATE_LIVE_PATH note); the front-end reads it via /usage.
                record_rate_limit(obj.get("rate_limit_info") or {})
            self._broadcast(obj)

    def _read_stderr(self):
        for line in self.proc.stderr:
            if line.strip():
                self._broadcast({"type": "stderr", "text": line.rstrip()})

    def _emit_context(self, result):
        try:
            # Use the latest assistant message's usage (one API call = current context
            # occupancy), NOT result.usage — the latter is the turn's CUMULATIVE total
            # across every internal tool-call round trip, so on a multi-step turn it sums
            # the context many times over and reads way past 100% (the old 357% bug).
            u = self._last_msg_usage or result.get("usage", {}) or {}
            used = (u.get("input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0))
            window = 0
            for mu in (result.get("modelUsage") or {}).values():
                window = max(window, mu.get("contextWindow", 0))
            if window:
                self.context_pct = round(min(used / window, 1.0) * 100, 1)
                self._broadcast({"type": "context", "pct": self.context_pct,
                                 "used": used, "window": window})
        except Exception:
            pass

    # ---- pub/sub -----------------------------------------------------------
    def _broadcast(self, obj):
        # Stamp every event with the wall-clock time it was seen, so the front-end
        # can show an accurate per-message timestamp on both live turns and replay.
        if "ts" not in obj:
            obj["ts"] = time.time()
        self._record(obj)
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(obj)
            except queue.Full:
                pass

    def subscribe(self):
        q = queue.Queue(maxsize=4000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # ---- public api --------------------------------------------------------
    def send(self, text, image_path=None, url=None):
        self.ensure_started()
        if not self.alive or not self.proc or self.proc.stdin is None:
            return False
        full = text
        display = text
        if url:
            full = (text + "\n\n" if text else "") + f"[Current page: {url}]"
            display = (text + "\n" if text else "") + f"🔗 {url}"
        content = [{"type": "text", "text": full or "(see attachment)"}]
        if image_path and os.path.exists(image_path):
            try:
                import base64
                with open(image_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": b64}})
                display += "\n📷 [screenshot]"
            except Exception:
                pass
        if not self.title:
            t = text or "Screenshot" if image_path else (text or url or "New chat")
            self.title = (t[:40] + "…") if len(t) > 40 else t
        self.last_activity = time.time()
        self._broadcast({"type": "user_text", "text": display})   # for live + replay
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            self.alive = False
            return False

    def stop(self):
        self._intentional_stop = True
        try:
            if self.proc and self.alive:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
        except Exception:
            pass
        self.alive = False

    def delete_data(self):
        try:
            if self._jsonl and os.path.exists(self._jsonl):
                os.remove(self._jsonl)
        except Exception:
            pass
