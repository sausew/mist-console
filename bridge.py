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
