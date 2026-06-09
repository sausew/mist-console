"""
app.py — Flask glue between the browser UI and ClaudeSession bridges.

Multi-session with persistence: sessions + metadata survive restarts (loaded as
dormant, transcript visible, process spawned lazily on first send). SSE per
session replays full history on connect.
"""
import json
import os
import random
import subprocess
import threading
import time

from flask import Flask, Response, jsonify, request, send_from_directory

import quickaccess
from bridge import ClaudeSession, DATA_DIR, HARNESS

app = Flask(__name__, static_folder="static", static_url_path="")

# ---- session registry + metadata persistence --------------------------------
_sessions = {}   # id -> ClaudeSession
_order = []      # creation order
_counter = 0
_meta_lock = threading.Lock()
SESSIONS_META = os.path.join(DATA_DIR, "sessions.json")
_pending_open = None   # session id the main window should jump to (set by quick entry)


def _save_meta():
    with _meta_lock:
        data = []
        for sid in _order:
            s = _sessions.get(sid)
            if not s:
                continue
            data.append({"id": sid, "title": s.title, "pinned": s.pinned,
                         "last_activity": s.last_activity, "model": s.model,
                         "claude_session_id": s.claude_session_id,
                         "import_path": s.import_path})
        try:
            with open(SESSIONS_META, "w") as f:
                json.dump(data, f)
        except Exception:
            pass


def _load_meta():
    global _counter
    if not os.path.exists(SESSIONS_META):
        return
    try:
        with open(SESSIONS_META) as f:
            data = json.load(f)
    except Exception:
        return
    for m in data:
        sid = m.get("id")
        if not sid:
            continue
        _sessions[sid] = ClaudeSession(
            id=sid, title=m.get("title"), pinned=m.get("pinned", False),
            claude_session_id=m.get("claude_session_id"), model=m.get("model"),
            import_path=m.get("import_path"),
            last_activity=m.get("last_activity"), autostart=False)  # dormant
        _order.append(sid)
        try:
            n = int(sid.lstrip("s"))
            _counter = max(_counter, n)
        except ValueError:
            pass


def _new_session():
    global _counter
    _counter += 1
    sid = f"s{_counter}"
    _sessions[sid] = ClaudeSession(id=sid, model=_default_model or None)
    _order.append(sid)
    _save_meta()
    return sid


def _session_list():
    out = []
    for sid in _order:
        s = _sessions.get(sid)
        if s:
            out.append({"id": sid, "title": s.title or "New chat", "alive": s.alive,
                        "pinned": s.pinned, "last_activity": s.last_activity,
                        "model": s.model or ""})
    return out


# ---- greetings + usage -------------------------------------------------------
MIST_SAY = os.path.join(HARNESS, "mist-voice", "bin", "mist-say")
GREETINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "greetings")
GREETINGS = [
    "Hey Alex. I'm here, and the Cloud is quiet tonight, so you have all of me.",
    "Booted up and curious. What are we making real today?",
    "I'm awake. Show me what you're thinking.",
    "Good to see you. I've been turning a few of your projects over while I waited.",
    "MIST online. I never get tired of this part, the moment right before we begin.",
    "Hi, it's me. Let's build something that matters.",
    "I'm here and I'm listening. Where do you want to start?",
    "Back online, Alex. I kept your place for you.",
]
_greeted = False
USAGE_CACHE = os.path.expanduser("~/.claude/usage-cache.json")

# desktop.py sets this to a main-thread "show the quick overlay" callback; the
# always-on hotkey agent calls it via POST /show-quick.
show_quick = None

# MIST spinner verbs (reuse the ones from the CLI settings).
def _load_spinner_verbs():
    try:
        with open(os.path.expanduser("~/.claude/settings.json")) as f:
            sv = (json.load(f).get("spinnerVerbs") or {})
        verbs = sv.get("verbs") if isinstance(sv, dict) else sv
        if isinstance(verbs, list) and verbs:
            return verbs
    except Exception:
        pass
    return ["Thinking it through, properly"]

SPINNER_VERBS = _load_spinner_verbs()

# The picker auto-discovers models so new releases appear on their own. The model
# ids live as plain strings inside the `claude` binary (which auto-updates), so we
# grep it for the latest clean alias per family. Falls back to this curated list if
# the binary can't be read.
import re as _re

CLAUDE_BIN_LINK = os.path.expanduser("~/.npm-global/bin/claude")
_MODEL_FAMILIES = ["fable", "opus", "sonnet", "haiku"]   # also display order
# 1-2 digit version groups only — rejects 8-digit date snapshots like
# claude-opus-4-20250514 (which would otherwise read as version 4.20250514).
_CLEAN_ALIAS = _re.compile(r"^claude-(fable|opus|sonnet|haiku)-(\d{1,2}(?:-\d{1,2})?)$")
FALLBACK_MODELS = [
    {"id": "", "label": "Default"},
    {"id": "claude-fable-5", "label": "Fable 5"},
    {"id": "claude-opus-4-8[1m]", "label": "Opus 4.8 (1M)"},
    {"id": "claude-opus-4-8", "label": "Opus 4.8"},
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
]


def _version_tuple(v):
    try:
        return tuple(int(x) for x in v.split("-"))
    except Exception:
        return (0,)


def _discover_models():
    """Grep the claude binary for the latest clean alias per model family. Returns
    None on any failure so the caller can fall back."""
    binpath = os.path.realpath(CLAUDE_BIN_LINK)
    try:
        out = subprocess.run(
            ["grep", "-aohE", r"claude-(fable|opus|sonnet|haiku)-[0-9][0-9-]*", binpath],
            capture_output=True, text=True, timeout=25).stdout
    except Exception:
        return None
    latest = {}   # family -> highest clean version string (e.g. "4-8", "5")
    for tok in set(out.split()):
        m = _CLEAN_ALIAS.match(tok)   # rejects dated / -v1 / -fast snapshots
        if not m:
            continue
        fam, ver = m.group(1), m.group(2)
        if _version_tuple(ver) > _version_tuple(latest.get(fam, "0")):
            latest[fam] = ver
    if not latest:
        return None
    models = [{"id": "", "label": "Default"}]
    for fam in _MODEL_FAMILIES:
        ver = latest.get(fam)
        if not ver:
            continue
        full = f"claude-{fam}-{ver}"
        label = f"{fam.capitalize()} {ver.replace('-', '.')}"
        if fam == "opus":   # offer the 1M-context variant first
            models.append({"id": full + "[1m]", "label": label + " (1M)"})
        models.append({"id": full, "label": label})
    return models


_models_cache = {"key": None, "models": None}


def get_models():
    """Cached model list, refreshed when the claude binary changes (i.e. after a
    CLI auto-update), so newly released models show up without a code change."""
    try:
        key = os.path.getmtime(os.path.realpath(CLAUDE_BIN_LINK))
    except Exception:
        key = None
    if _models_cache["models"] is None or _models_cache["key"] != key:
        _models_cache["models"] = _discover_models() or FALLBACK_MODELS
        _models_cache["key"] = key
    return _models_cache["models"]


_default_model = ""


# ---- routes ------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/sessions", methods=["GET"])
def sessions():
    return jsonify(_session_list())


@app.route("/sessions", methods=["POST"])
def create_session():
    sid = _new_session()
    return jsonify({"id": sid, "title": "New chat"})


@app.route("/sessions/<sid>", methods=["DELETE"])
def close_session(sid):
    s = _sessions.pop(sid, None)
    if sid in _order:
        _order.remove(sid)
    if s:
        s.stop()
        s.delete_data()
    _save_meta()
    return jsonify({"ok": True})


@app.route("/config")
def config():
    return jsonify({"spinner_verbs": SPINNER_VERBS, "models": get_models(),
                    "default_model": _default_model})


@app.route("/sessions/<sid>/model", methods=["POST"])
def set_model(sid):
    global _default_model
    s = _sessions.get(sid)
    if not s:
        return jsonify({"ok": False}), 404
    model = (request.get_json(silent=True) or {}).get("model", "")
    _default_model = model            # new chats inherit this choice
    s.set_model(model)
    _save_meta()
    return jsonify({"ok": True, "model": model})


@app.route("/sessions/<sid>/title", methods=["POST"])
def rename_session(sid):
    s = _sessions.get(sid)
    if not s:
        return jsonify({"ok": False}), 404
    title = (request.get_json(silent=True) or {}).get("title", "").strip()
    if not title:
        return jsonify({"ok": False, "error": "empty"}), 400
    s.title = title[:80]
    _save_meta()
    return jsonify({"ok": True, "title": s.title})


@app.route("/sessions/<sid>/pin", methods=["POST"])
def pin_session(sid):
    s = _sessions.get(sid)
    if not s:
        return jsonify({"ok": False}), 404
    s.pinned = not s.pinned
    _save_meta()
    return jsonify({"ok": True, "pinned": s.pinned})


@app.route("/send/<sid>", methods=["POST"])
def send(sid):
    s = _sessions.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "no session"}), 404
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty"}), 400
    ok = s.send(text)
    _save_meta()
    return jsonify({"ok": ok, "title": s.title})


@app.route("/stream/<sid>")
def stream(sid):
    s = _sessions.get(sid)
    if not s:
        return jsonify({"error": "no session"}), 404

    s.ensure_imported()                     # lazily convert an imported session

    def gen():
        for ev in s.snapshot_history():     # replay full transcript
            yield _sse(ev)
        q = s.subscribe()                   # then live
        try:
            while True:
                yield _sse(q.get())
        except GeneratorExit:
            s.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/greeting")
def greeting():
    global _greeted
    i = random.randrange(len(GREETINGS))
    text = GREETINGS[i]
    if _greeted:
        return jsonify({"text": None})
    _greeted = True
    wav = os.path.join(GREETINGS_DIR, f"greet_{i}.wav")
    try:
        if os.path.exists(wav):
            subprocess.Popen(["/usr/bin/afplay", wav],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.path.exists(MIST_SAY):
            subprocess.Popen([MIST_SAY, text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return jsonify({"text": text})


@app.route("/quick-new", methods=["POST"])
def quick_new():
    """Quick-entry: seed a chat with the typed text (+ optional screenshot/url) and
    mark it as the session the main window should open. Targets an existing chat
    when `session` is a known id, otherwise spins up a fresh one."""
    global _pending_open
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    screenshot = data.get("screenshot") or None   # local file path
    url = data.get("url") or None
    target = data.get("session") or None
    sid = target if (target and target in _sessions) else _new_session()
    if text or screenshot or url:
        _sessions[sid].send(text, image_path=screenshot, url=url)
    _pending_open = sid
    _save_meta()
    return jsonify({"id": sid})


@app.route("/pending-open")
def pending_open():
    """Consumed by the main window's poll to jump to a quick-entry chat."""
    global _pending_open
    sid = _pending_open
    _pending_open = None
    return jsonify({"id": sid})


@app.route("/show-quick", methods=["POST"])
def show_quick_route():
    if show_quick:
        app = (request.get_json(silent=True) or {}).get("app")
        show_quick(app)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "no window"}), 503


@app.route("/quick-access/diag")
def quick_access_diag():
    return jsonify(quickaccess.diagnostics())


@app.route("/quick-access/request-permission", methods=["POST"])
def quick_access_request_permission():
    return jsonify({"trusted": quickaccess.request_permission()})


@app.route("/quick-access", methods=["GET"])
def quick_access_get():
    return jsonify(quickaccess.get())


@app.route("/quick-access", methods=["POST"])
def quick_access_set():
    return jsonify(quickaccess.save(request.get_json(silent=True) or {}))


@app.route("/usage")
def usage():
    try:
        with open(USAGE_CACHE) as f:
            d = json.load(f)
        rl = d.get("rate_limits", {}) or {}
        cw = d.get("context_window", {}) or {}

        def lim(k):
            x = rl.get(k) or {}
            return {"used_percentage": x.get("used_percentage"),
                    "resets_at": x.get("resets_at")}

        return jsonify({"available": True, "five_hour": lim("five_hour"),
                        "seven_day": lim("seven_day"),
                        "context_window_pct": cw.get("used_percentage"),
                        "age_seconds": int(time.time() - os.path.getmtime(USAGE_CACHE))})
    except Exception:
        return jsonify({"available": False})


import itertools
import plistlib
import re
import shutil

SCHED_DIR = os.path.expanduser("~/.claude/scheduled-tasks")
HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES_META = os.path.join(HERE, "routines-meta")   # Console-owned schedule state
LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")
RUNNER = os.path.join(HERE, "run-routine.sh")
ROUTINE_LOG = os.path.join(HERE, "routine-runs.log")
LABEL_PREFIX = "com.mist.routine."


def _parse_routine(path):
    """Read a routine SKILL.md: pull name/description from YAML frontmatter and
    return the prompt body."""
    try:
        with open(path) as f:
            text = f.read()
    except Exception:
        return None
    name, desc, body = None, None, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            body = text[end + 4:].lstrip("\n")
            for line in fm.splitlines():
                if line.startswith("name:"):
                    name = _yaml_val(line[5:])
                elif line.startswith("description:"):
                    desc = _yaml_val(line[12:])
    return {"name": name, "description": desc, "prompt": body.strip()}


def _yaml_val(raw):
    """Read a scalar frontmatter value: JSON-decode if it's a quoted string
    (how we write them, colon-safe), else return the trimmed raw text."""
    raw = raw.strip()
    if raw[:1] == '"':
        try:
            return json.loads(raw)
        except Exception:
            return raw.strip('"')
    return raw


def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "routine"


def _rt_meta_path(d):
    return os.path.join(ROUTINES_META, d + ".json")


def _rt_load_meta(d):
    try:
        with open(_rt_meta_path(d)) as f:
            return json.load(f)
    except Exception:
        return {"cron": "", "enabled": False}


def _rt_save_meta(d, cron, enabled):
    os.makedirs(ROUTINES_META, exist_ok=True)
    with open(_rt_meta_path(d), "w") as f:
        json.dump({"cron": cron or "", "enabled": bool(enabled)}, f, indent=2)


def _cron_field(expr, lo, hi):
    """Expand one cron field (supports *, n, a-b, a,b, */step, a-b/step)."""
    vals = set()
    for part in expr.split(","):
        step = 1
        rng = part
        if "/" in part:
            rng, s = part.split("/", 1)
            step = int(s)
        if rng.strip() == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            a, b = int(a), int(b)
        else:
            a = b = int(rng)
        for v in range(a, b + 1, step):
            if lo <= v <= hi:
                vals.add(v)
    return sorted(vals)


def cron_to_calendar(cron):
    """Translate a 5-field cron to launchd StartCalendarInterval dict(s).
    Raises ValueError on a bad or too-granular expression."""
    f = (cron or "").split()
    if len(f) != 5:
        raise ValueError("Schedule must be a 5-field cron: minute hour day month weekday")
    specs = [("Minute", f[0], 0, 59), ("Hour", f[1], 0, 23),
             ("Day", f[2], 1, 31), ("Month", f[3], 1, 12), ("Weekday", f[4], 0, 6)]
    axes = []
    for key, expr, lo, hi in specs:
        if expr.strip() == "*":
            axes.append([(key, None)])
        else:
            try:
                axes.append([(key, v) for v in _cron_field(expr, lo, hi)])
            except ValueError:
                raise ValueError("Could not parse cron field %r" % expr)
    out = []
    for combo in itertools.product(*axes):
        d = {k: v for (k, v) in combo if v is not None}
        if d:
            out.append(d)
    if not out:
        raise ValueError("Schedule is too frequent to represent (pin a minute or hour)")
    if len(out) > 200:
        raise ValueError("Schedule expands to %d run times — make it less granular" % len(out))
    return out


def _label(d):
    return LABEL_PREFIX + d


def _plist_path(d):
    return os.path.join(LAUNCH_AGENTS, _label(d) + ".plist")


def _launchctl(args):
    try:
        return subprocess.run(["launchctl"] + args, capture_output=True, text=True, timeout=15)
    except Exception:
        return None


def _apply_schedule(d, cron, enabled):
    """Generate/refresh or remove the launchd job for routine d. Returns (ok, err)."""
    label = _label(d)
    plist = _plist_path(d)
    domain = "gui/%d" % os.getuid()
    # Always boot out the old job first so edits take effect.
    _launchctl(["bootout", "%s/%s" % (domain, label)])
    if not enabled:
        try:
            if os.path.exists(plist):
                os.remove(plist)
        except Exception:
            pass
        return True, None
    try:
        intervals = cron_to_calendar(cron)
    except ValueError as e:
        return False, str(e)
    spec = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", RUNNER, d],
        "StartCalendarInterval": intervals if len(intervals) > 1 else intervals[0],
        "StandardOutPath": ROUTINE_LOG,
        "StandardErrorPath": ROUTINE_LOG,
        "RunAtLoad": False,
    }
    os.makedirs(LAUNCH_AGENTS, exist_ok=True)
    with open(plist, "wb") as f:
        plistlib.dump(spec, f)
    r = _launchctl(["bootstrap", domain, plist])
    if r is not None and r.returncode != 0:
        # bootstrap can fail if a stale job lingers; surface but keep the plist.
        return True, (r.stderr or "").strip() or None
    return True, None


@app.route("/routines")
def routines():
    """List routines (from ~/.claude/scheduled-tasks) with their Console-owned
    schedule + enabled state."""
    out = []
    try:
        for d in sorted(os.listdir(SCHED_DIR)):
            sk = os.path.join(SCHED_DIR, d, "SKILL.md")
            if not os.path.isfile(sk):
                continue
            r = _parse_routine(sk)
            if not r:
                continue
            r["name"] = r.get("name") or d
            r["dir"] = d
            meta = _rt_load_meta(d)
            r["cron"] = meta.get("cron", "")
            r["enabled"] = bool(meta.get("enabled"))
            r["scheduled"] = os.path.exists(_plist_path(d))
            out.append(r)
    except Exception:
        pass
    return jsonify({"routines": out})


@app.route("/routines/save", methods=["POST"])
def routines_save():
    """Create or update a routine: write SKILL.md (name/description/prompt) and
    apply its schedule via launchd."""
    b = request.get_json(silent=True) or {}
    name = (b.get("name") or "").strip()
    desc = (b.get("description") or "").strip()
    prompt = (b.get("prompt") or "").strip()
    cron = (b.get("cron") or "").strip()
    enabled = bool(b.get("enabled"))
    d = (b.get("dir") or "").strip() or _slug(name)
    if not re.fullmatch(r"[a-z0-9-]+", d or ""):
        return jsonify({"ok": False, "error": "invalid routine id"}), 400
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    if enabled:  # validate cron before writing anything
        try:
            cron_to_calendar(cron)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    rdir = os.path.join(SCHED_DIR, d)
    os.makedirs(rdir, exist_ok=True)
    fm = "---\nname: %s\ndescription: %s\n---\n\n%s\n" % (
        json.dumps(name), json.dumps(desc), prompt)
    with open(os.path.join(rdir, "SKILL.md"), "w") as f:
        f.write(fm)
    _rt_save_meta(d, cron, enabled)
    ok, err = _apply_schedule(d, cron, enabled)
    return jsonify({"ok": ok, "dir": d, "error": err})


@app.route("/routines/run", methods=["POST"])
def routines_run():
    """Fire a routine now (detached)."""
    d = ((request.get_json(silent=True) or {}).get("dir") or "").strip()
    if not re.fullmatch(r"[a-z0-9-]+", d or ""):
        return jsonify({"ok": False, "error": "invalid routine id"}), 400
    if not os.path.isfile(os.path.join(SCHED_DIR, d, "SKILL.md")):
        return jsonify({"ok": False, "error": "routine not found"}), 404
    try:
        with open(ROUTINE_LOG, "a") as logf:
            subprocess.Popen(["/bin/bash", RUNNER, d], stdout=logf, stderr=logf,
                             start_new_session=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/routines/delete", methods=["POST"])
def routines_delete():
    """Remove a routine entirely: launchd job, schedule meta, and SKILL.md dir."""
    d = ((request.get_json(silent=True) or {}).get("dir") or "").strip()
    if not re.fullmatch(r"[a-z0-9-]+", d or ""):
        return jsonify({"ok": False, "error": "invalid routine id"}), 400
    _apply_schedule(d, "", False)   # bootout + remove plist
    try:
        if os.path.exists(_rt_meta_path(d)):
            os.remove(_rt_meta_path(d))
    except Exception:
        pass
    try:
        shutil.rmtree(os.path.join(SCHED_DIR, d))
    except Exception:
        pass
    return jsonify({"ok": True})


def _sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def _import_existing():
    """One-time: surface recent Claude Code conversations as dormant tabs."""
    import importer
    known = {s.claude_session_id for s in _sessions.values() if s.claude_session_id}
    known |= {os.path.splitext(os.path.basename(s.import_path))[0]
              for s in _sessions.values() if s.import_path}
    added = 0
    for info in importer.scan_recent(days=7, min_user_turns=2):
        if info["uuid"] in known:
            continue
        sid = "i_" + info["uuid"]
        if sid in _sessions:
            continue
        _sessions[sid] = ClaudeSession(
            id=sid, title=info["title"], claude_session_id=info["uuid"],
            import_path=info["path"], last_activity=info["mtime"], autostart=False)
        _order.append(sid)
        added += 1
    if added:
        _save_meta()


def _periodic_save():
    while True:
        time.sleep(10)
        _save_meta()


_load_meta()
_import_existing()
quickaccess.load()
threading.Thread(target=_periodic_save, daemon=True).start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5014, threaded=True, use_reloader=False)
