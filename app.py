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

MODELS = [
    {"id": "", "label": "Default"},
    {"id": "claude-opus-4-8[1m]", "label": "Opus 4.8 (1M)"},
    {"id": "claude-opus-4-8", "label": "Opus 4.8"},
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
]
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
    return jsonify({"spinner_verbs": SPINNER_VERBS, "models": MODELS,
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
