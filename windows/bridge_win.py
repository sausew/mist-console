"""
bridge_win.py: manages headless `claude` processes and relays their stream-json
event protocol to subscribers (the SSE endpoint), with per-session history
persistence. Windows port of bridge.py; the class API is identical so app_win
mirrors app.py.

Windows-specific differences from the macOS original:
- claude.exe is discovered at spawn time (config_win.find_claude), not pinned
  to a hardcoded path, so the setup wizard can install it mid-session.
- Subprocesses use encoding="utf-8" explicitly. Windows text mode defaults to
  the ANSI codepage (cp1252), which corrupts the JSON stream on any non-ASCII
  character.
- Subprocesses pass CREATE_NO_WINDOW so each claude backend does not flash a
  console window under the GUI exe.
- The OAuth rate-limit probe reads ~/.claude/.credentials.json (where Claude
  Code stores credentials on Windows) instead of the macOS Keychain.
"""
import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request

import config_win

DATA_DIR = config_win.DATA_DIR

DEFAULT_PERMISSION_MODE = "bypassPermissions"
HISTORY_CAP = 8000   # max events kept in memory for replay (jsonl keeps all)

# app_win sets this to its _save_meta so a session can persist itself the
# instant its claude_session_id is assigned (on the backend's init event).
on_meta_dirty = None

# Event slimming: raw stream-json events carry a `tool_use_result` sidecar the
# Console never reads; large file edits would otherwise persist whole file
# snapshots into the jsonl. Same caps as the macOS build.
TOOL_RESULT_STUB_OVER = 32_768   # bytes of serialized tool_use_result kept as-is
STRING_CAP = 262_144             # any longer string field is truncated


def _slim_event(obj):
    """Return obj with oversized payloads stubbed/truncated. Copy-on-write:
    the dict already broadcast to live subscribers is never mutated."""
    try:
        out = obj
        tur = obj.get("tool_use_result")
        if tur is not None:
            blob = json.dumps(tur)
            if len(blob) > TOOL_RESULT_STUB_OVER:
                out = dict(obj)
                out["tool_use_result"] = {"_stubbed": True, "_bytes": len(blob)}
        if len(json.dumps(out)) > 2 * STRING_CAP:
            def walk(v):
                if isinstance(v, str) and len(v) > STRING_CAP:
                    return v[:STRING_CAP] + "…[+%d chars stripped]" % (len(v) - STRING_CAP)
                if isinstance(v, list):
                    return [walk(x) for x in v]
                if isinstance(v, dict):
                    return {k: walk(x) for k, x in v.items()}
                return v
            out = walk(out)
        return out
    except Exception:
        return obj


# Context-cost cap: a headless `claude -p --resume` re-bills the whole
# conversation every turn, so long-lived chats get expensive. One-shot notice
# at CTX_WARN_PCT; soft gate at CTX_HARD_PCT (first over-threshold send is
# held, the next goes through).
CTX_WARN_PCT = 60
CTX_HARD_PCT = 80

# Idle-process reaper: a live backend pins ~300 MB; after IDLE_REAP_SEC of no
# activity it goes dormant and revives on the next send via --resume.
IDLE_REAP_SEC = int(os.environ.get("MIST_CONSOLE_IDLE_REAP_SEC", "900"))


def _tail_lines(path, max_lines):
    """Last `max_lines` lines of a possibly huge jsonl, reading only the tail."""
    max_lines = max(1, max_lines)
    block = 1 << 20  # 1 MiB
    chunks = []
    newlines = 0
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        while pos > 0 and newlines <= max_lines:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    return data.decode("utf-8", "replace").splitlines()[-max_lines:]


# Live rate-limit store, refreshed from each turn's rate_limit_event. The
# interactive-CLI statusline cache usually does not exist on a fresh Windows
# machine, so these live events (plus the probe below) are the usage badges'
# primary source here.
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
            with open(RATE_LIVE_PATH, encoding="utf-8") as f:
                d = json.load(f) or {}
        except Exception:
            d = {}
        d[t] = rec
        tmp = RATE_LIVE_PATH + ".tmp.%d" % os.getpid()
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, RATE_LIVE_PATH)
        except Exception:
            pass
    # A rate_limit_event means a real turn just ran, so the 5h/7d windows are
    # active right now: the one safe moment to read the live utilization %.
    maybe_probe_rate_util()


# Live utilization %, probed from the subscription's own rate-limit response
# headers just after a real turn. See bridge.py for the full rationale.
RATE_UTIL_PATH = os.path.join(DATA_DIR, "rate-util.json")
_PROBE_MIN_INTERVAL = 300          # seconds between probes during continuous use
_probe_state = {"last_ts": 0.0}
_probe_state_lock = threading.Lock()


def _read_oauth_token():
    """Claude Code's subscription OAuth access token. On Windows the CLI keeps
    credentials in ~/.claude/.credentials.json (no Keychain here)."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude",
                               ".credentials.json"), encoding="utf-8") as f:
            return (json.load(f).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def _probe_rate_util():
    """One minimal OAuth-authenticated request; persist the unified utilization
    headers per window. Silent no-op on any failure."""
    token = _read_oauth_token()
    if not token:
        return
    body = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "anthropic-version": "2023-06-01",
                 "anthropic-beta": "oauth-2025-04-20",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            hdrs = resp.headers
    except Exception:
        return
    rec, now = {}, int(time.time())
    for win, tag in (("five_hour", "5h"), ("seven_day", "7d")):
        util = hdrs.get("anthropic-ratelimit-unified-%s-utilization" % tag)
        if util is None:
            continue
        reset = hdrs.get("anthropic-ratelimit-unified-%s-reset" % tag)
        try:
            rec[win] = {"utilization": float(util),
                        "resets_at": int(reset) if reset else None,
                        "status": hdrs.get("anthropic-ratelimit-unified-%s-status" % tag),
                        "ts": now}
        except (TypeError, ValueError):
            continue
    if not rec:
        return
    tmp = RATE_UTIL_PATH + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, RATE_UTIL_PATH)
    except Exception:
        pass


def maybe_probe_rate_util():
    """Throttled, non-blocking trigger. Caller guarantees an active window."""
    with _probe_state_lock:
        now = time.time()
        if now - _probe_state["last_ts"] < _PROBE_MIN_INTERVAL:
            return
        _probe_state["last_ts"] = now
    threading.Thread(target=_probe_rate_util, daemon=True).start()


# Scoped to Console sessions. The Console renders text only; keep the model
# from trying to produce audio or notifications it has no surface for.
CONSOLE_SURFACE_PROMPT = (
    "You are running inside the MIST Console, a text-only desktop chat app on "
    "Windows. Respond in text only; do not attempt to play audio or send "
    "desktop notifications from this surface."
)


def _spawn_kwargs():
    """Common Popen keyword arguments for claude subprocesses."""
    env = dict(os.environ)
    env["PATH"] = config_win.claude_path_prefix() + os.pathsep + env.get("PATH", "")
    kw = dict(env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)
    if config_win.CREATE_NO_WINDOW:
        kw["creationflags"] = config_win.CREATE_NO_WINDOW
    return kw


class ClaudeSession:
    """One conversation: a persisted event history + (lazily) a claude process."""

    def __init__(self, id=None, title=None, pinned=False, claude_session_id=None,
                 last_activity=None, autostart=True, import_path=None,
                 permission_mode=DEFAULT_PERMISSION_MODE, model=None, cwd=None,
                 pin_order=0):
        self.id = id
        self.title = title
        self.pinned = pinned
        self.pin_order = pin_order
        self.import_path = import_path
        self._import_done = False
        self.last_activity = last_activity or time.time()
        self.claude_session_id = claude_session_id
        self.permission_mode = permission_mode
        self.model = model
        self.cwd = cwd or config_win.load_config().get("workspace") or config_win.DEFAULT_WORKSPACE

        self.proc = None
        self.alive = False
        self.session_id = None
        self.last_init = None
        self.context_pct = None
        self._last_msg_usage = None

        self.history = []
        self._jsonl = os.path.join(DATA_DIR, f"{id}.jsonl") if id else None
        self._subscribers = []
        self._lock = threading.Lock()
        self._hist_lock = threading.Lock()
        self._resume_tried = False
        self._started_at = 0.0
        self._saw_init = False
        self._intentional_stop = False
        self._turn_active = False
        self._ctx_warned = False
        self._ctx_override = False
        self._ctl_seq = 0
        self._pending_stops = {}

        self._history_loaded = False
        if autostart:
            self.ensure_started()

    # ---- history persistence ----------------------------------------------
    def _load_history(self):
        with self._hist_lock:
            if self._history_loaded:
                return
            self._history_loaded = True
        if not self._jsonl or not os.path.exists(self._jsonl):
            return
        try:
            for line in _tail_lines(self._jsonl, HISTORY_CAP):
                line = line.strip()
                if not line:
                    continue
                try:
                    self.history.append(json.loads(line))
                except Exception:
                    pass  # skip a truncated/corrupt line, keep the rest
            for obj in reversed(self.history):
                if obj.get("type") == "context":
                    self.context_pct = obj.get("pct")
                    break
        except Exception:
            pass

    def _record(self, obj):
        obj = _slim_event(obj)
        with self._hist_lock:
            self.history.append(obj)
            if len(self.history) > HISTORY_CAP:
                self.history = self.history[-HISTORY_CAP:]
        if self._jsonl:
            try:
                with open(self._jsonl, "a", encoding="utf-8") as f:
                    f.write(json.dumps(obj) + "\n")
            except Exception:
                pass

    def snapshot_history(self):
        self._load_history()
        with self._hist_lock:
            return list(self.history)

    def ensure_imported(self):
        """First time an imported tab is opened, convert its Claude Code jsonl
        into display events."""
        if self._import_done or not self.import_path:
            return
        self._import_done = True
        self._load_history()
        if self.history:
            return
        try:
            import importer_win
            for ev in importer_win.convert(self.import_path):
                self._record(ev)
        except Exception:
            pass

    # ---- process lifecycle ------------------------------------------------
    def _build_cmd(self, claude):
        cmd = [claude, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        if self.permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        elif self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        # AskUserQuestion is an interactive picker that auto-dismisses with no
        # TTY; disable it so the model asks in plain text instead.
        cmd += ["--disallowed-tools", "AskUserQuestion"]
        if self.claude_session_id and not self._resume_tried:
            cmd += ["--resume", self.claude_session_id]
        if self.model:
            cmd += ["--model", self.model]
        cmd += ["--append-system-prompt", CONSOLE_SURFACE_PROMPT]
        return cmd

    def ensure_started(self):
        with self._lock:
            if self.alive:
                return
            claude = config_win.find_claude()
            if not claude:
                self._broadcast({"type": "process_exit", "code": -1,
                                 "error": "Claude Code is not installed. Open Settings "
                                          "to run setup, or install it from "
                                          "https://claude.ai/download."})
                return
            if not os.path.isdir(self.cwd):
                try:
                    os.makedirs(self.cwd, exist_ok=True)
                except Exception:
                    pass
            try:
                self.proc = subprocess.Popen(
                    self._build_cmd(claude), cwd=self.cwd,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    **_spawn_kwargs())
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
        self._turn_active = False
        if self._intentional_stop:
            self._intentional_stop = False
            return
        if (self.claude_session_id and not self._resume_tried and not self._saw_init
                and time.time() - self._started_at < 5):
            self._resume_tried = True
            self.claude_session_id = None
            self.ensure_started()
            return
        self._broadcast({"type": "process_exit", "code": code})

    def set_model(self, model):
        self.model = model or None
        if self.alive:
            self._resume_tried = False
            self.stop()

    def set_permission(self, mode):
        if not mode or mode == self.permission_mode:
            return
        self.permission_mode = mode
        if self.alive:
            self._resume_tried = False
            self.stop()

    def set_cwd(self, cwd):
        """Switch working directory. Starts fresh there on the next send
        (claude transcripts are keyed to the cwd they were created in)."""
        if not cwd or cwd == self.cwd:
            return
        self.cwd = cwd
        self.claude_session_id = None
        self._resume_tried = False
        if self.alive:
            self.stop()

    def reap_if_idle(self, timeout):
        """Put a live-but-idle backend dormant to reclaim RAM/CPU."""
        if timeout <= 0 or not self.alive or self._turn_active:
            return False
        if time.time() - self.last_activity < timeout:
            return False
        self._resume_tried = False
        self.stop()
        return True

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
                new_csid = obj.get("session_id")
                _changed = new_csid and new_csid != self.claude_session_id
                self.claude_session_id = new_csid
                self.last_init = obj
                if _changed and on_meta_dirty:
                    try:
                        on_meta_dirty()
                    except Exception:
                        pass
            elif obj.get("type") == "assistant":
                mu = (obj.get("message") or {}).get("usage")
                if mu and not obj.get("parent_tool_use_id"):
                    self._last_msg_usage = mu
            elif obj.get("type") == "result":
                self.last_activity = time.time()
                self._turn_active = False
                self._emit_context(obj)
            elif obj.get("type") == "rate_limit_event":
                record_rate_limit(obj.get("rate_limit_info") or {})
            elif obj.get("type") == "control_response":
                resp = obj.get("response") or {}
                task_id = self._pending_stops.pop(resp.get("request_id"), None)
                if task_id:
                    if resp.get("subtype") == "success":
                        self._broadcast({"type": "system", "subtype": "task_updated",
                                         "task_id": task_id, "status": "killed"})
                    else:
                        self._broadcast({"type": "system", "subtype": "task_stop_failed",
                                         "task_id": task_id,
                                         "error": str(resp.get("error") or "stop failed")})
            self._broadcast(obj)

    def _read_stderr(self):
        for line in self.proc.stderr:
            if line.strip():
                self._broadcast({"type": "stderr", "text": line.rstrip()})

    def _emit_context(self, result):
        try:
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
                self._check_context_cost()
        except Exception:
            pass

    def _check_context_cost(self):
        pct = self.context_pct
        if pct is None:
            return
        if pct < CTX_WARN_PCT:
            self._ctx_warned = False
            self._ctx_override = False
        elif CTX_WARN_PCT <= pct < CTX_HARD_PCT and not self._ctx_warned:
            self._ctx_warned = True
            self._broadcast({
                "type": "context_warning", "level": "warn", "pct": pct,
                "text": (f"This chat is at {pct:.0f}% of the context window. Long "
                         "conversations re-bill their whole history every message; "
                         "start a “+ new chat” for unrelated tasks to save tokens.")})

    def context_gate(self):
        """Cost cap checked before forwarding a message."""
        pct = self.context_pct
        if pct is None or pct < CTX_HARD_PCT:
            return None
        if self._ctx_override:
            return None
        self._ctx_override = True
        return (f"This chat is at {pct:.0f}% of the context window. Every message "
                "now re-bills the whole conversation, which burns usage fast. "
                "Start a “+ new chat” for a new task, or send again to continue here.")

    # ---- pub/sub -----------------------------------------------------------
    def _broadcast(self, obj):
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
        img_for_display = None
        if image_path and os.path.exists(image_path):
            try:
                import base64
                ext = os.path.splitext(image_path)[1].lower().lstrip(".")
                media = {"jpg": "jpeg", "jpeg": "jpeg", "gif": "gif",
                         "webp": "webp"}.get(ext, "png")
                with open(image_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/" + media, "data": b64}})
                img_for_display = image_path
            except Exception:
                pass
        if not self.title:
            t = text or "Screenshot" if image_path else (text or url or "New chat")
            self.title = (t[:40] + "…") if len(t) > 40 else t
        self.last_activity = time.time()
        ev = {"type": "user_text", "text": display}
        if img_for_display:
            ev["image"] = img_for_display
        self._broadcast(ev)
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            self._turn_active = True
            return True
        except (BrokenPipeError, ValueError, OSError):
            self.alive = False
            return False

    def stop_task(self, task_id):
        """Kill a running background task via the stream-json control protocol."""
        if not self.alive or not self.proc or self.proc.stdin is None:
            return False
        with self._lock:
            self._ctl_seq += 1
            req_id = f"console-stop-{self._ctl_seq}"
        self._pending_stops[req_id] = task_id
        req = {"type": "control_request", "request_id": req_id,
               "request": {"subtype": "stop_task", "task_id": task_id}}
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            self._pending_stops.pop(req_id, None)
            self.alive = False
            return False

    # ---- auth slash commands ----------------------------------------------
    # /login etc can't run inside the headless process; shell out to
    # `claude auth ...` and stream its output into the chat as notices.
    def maybe_auth_command(self, text):
        t = (text or "").strip()
        low = t.lower()
        if low in ("/login", "/signin") or low.startswith(("/login ", "/signin ")):
            rest = t.split(None, 1)[1].strip() if " " in t else ""
            if rest.lower() == "status":
                self._dispatch_auth(t, "status", [])
            else:
                args = ["--claudeai"]
                if rest.lower() == "console":
                    args = ["--console"]
                elif "@" in rest:
                    args += ["--email", rest]
                self._dispatch_auth(t, "login", args)
            return True
        if low == "/logout":
            self._dispatch_auth(t, "logout", [])
            return True
        if low in ("/auth", "/auth status", "/whoami"):
            self._dispatch_auth(t, "status", [])
            return True
        return False

    def _dispatch_auth(self, echo, action, args):
        self._broadcast({"type": "user_text", "text": echo})
        if action == "login":
            self._broadcast({"type": "notice",
                             "text": "Signing in… a browser window will open to "
                                     "finish authentication."})
        threading.Thread(target=self._run_auth, args=(action, args), daemon=True).start()

    def _run_auth(self, action, args):
        claude = config_win.find_claude()
        if not claude:
            self._broadcast({"type": "notice", "text": "Claude Code is not installed.",
                             "err": True})
            self._broadcast({"type": "status_idle"})
            return
        cmd = [claude, "auth", action] + list(args)
        try:
            p = subprocess.Popen(cmd, cwd=self.cwd,
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 **_spawn_kwargs())
        except Exception as e:
            self._broadcast({"type": "notice", "text": f"Couldn't start auth: {e}",
                             "err": True})
            self._broadcast({"type": "status_idle"})
            return
        for line in p.stdout:
            line = line.rstrip()
            if line:
                self._broadcast({"type": "notice", "text": line})
        code = p.wait()
        if action == "login" and code == 0:
            if self.alive:
                self._resume_tried = False
                self.stop()
            self._broadcast({"type": "notice",
                             "text": "Signed in. Send your message again to continue."})
        elif action == "logout" and code == 0:
            self._broadcast({"type": "notice", "text": "Signed out."})
        elif code != 0:
            self._broadcast({"type": "notice",
                             "text": f"`claude auth {action}` exited with code {code}.",
                             "err": True})
        self._broadcast({"type": "status_idle"})

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
