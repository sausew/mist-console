"""
app_win.py: Flask glue between the browser UI and ClaudeSession bridges.
Windows port of app.py, serving the same static/ front-end.

Differences from the macOS original:
- First-run setup wizard: / serves setup.html until setup completes. The
  wizard installs Claude Code, signs in, picks a workspace, and optionally
  stores an image-generation key.
- No launchd, no quick-access hotkey overlay, no AirDrop router, no voice:
  those routes answer with inert stubs so the shared front-end degrades
  cleanly (empty lists, disabled panels).
- Model discovery scans the claude binary with a pure-Python regex (no grep
  on Windows).
- The /file allowlist covers Downloads, Pictures (the image gallery), the
  workspace, and the app data dir.
"""
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

import config_win
from bridge_win import (ClaudeSession, DATA_DIR, RATE_LIVE_PATH, RATE_UTIL_PATH,
                        DEFAULT_PERMISSION_MODE, IDLE_REAP_SEC)

STATIC_DIR = config_win.resource_path("static")
WINDOWS_DIR = config_win.resource_path("windows")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
logging.getLogger("werkzeug").setLevel(logging.WARNING)

config_win.load_env_file()

# ---- session registry + metadata persistence --------------------------------
_sessions = {}
_order = []
_counter = 0
_meta_lock = threading.Lock()
SESSIONS_META = os.path.join(DATA_DIR, "sessions.json")
_pending_open = None

THEME_PATH = os.path.join(DATA_DIR, "theme.json")
_VALID_THEME = re.compile(r"^[a-z0-9_-]{1,40}$")
DEFAULT_THEME = "solarpunk"


def _load_theme():
    try:
        with open(THEME_PATH, encoding="utf-8") as f:
            t = (json.load(f) or {}).get("theme")
        if t and _VALID_THEME.match(t):
            return t
    except Exception:
        pass
    return DEFAULT_THEME


def _save_theme(theme):
    try:
        with open(THEME_PATH, "w", encoding="utf-8") as f:
            json.dump({"theme": theme}, f)
    except Exception:
        pass


FONT_PATH = os.path.join(DATA_DIR, "font.json")
_VALID_FONT_ID = re.compile(r"^[a-z0-9_-]{1,40}$")
_VALID_FONT_STACK = re.compile(r'^[A-Za-z0-9 ,"\'\-]{0,200}$')


def _load_font():
    try:
        with open(FONT_PATH, encoding="utf-8") as f:
            d = json.load(f) or {}
        fid, stack = d.get("id"), d.get("stack") or ""
        if fid and _VALID_FONT_ID.match(fid) and _VALID_FONT_STACK.match(stack):
            return {"id": fid, "stack": stack}
    except Exception:
        pass
    return {"id": "default", "stack": ""}


def _save_font(fid, stack):
    try:
        with open(FONT_PATH, "w", encoding="utf-8") as f:
            json.dump({"id": fid, "stack": stack}, f)
    except Exception:
        pass


def _workspace_dir():
    return config_win.load_config().get("workspace") or config_win.DEFAULT_WORKSPACE


def _save_meta():
    with _meta_lock:
        data = []
        for sid in _order:
            s = _sessions.get(sid)
            if not s:
                continue
            data.append({"id": sid, "title": s.title, "pinned": s.pinned,
                         "pin_order": s.pin_order,
                         "last_activity": s.last_activity, "model": s.model,
                         "permission_mode": s.permission_mode,
                         "claude_session_id": s.claude_session_id,
                         "import_path": s.import_path, "cwd": s.cwd})
        # Atomic write so a concurrent reader never sees a half-written file.
        try:
            tmp = f"{SESSIONS_META}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SESSIONS_META)
        except Exception:
            pass


def _load_meta():
    global _counter
    if not os.path.exists(SESSIONS_META):
        return
    try:
        with open(SESSIONS_META, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    for m in data:
        sid = m.get("id")
        if not sid:
            continue
        _sessions[sid] = ClaudeSession(
            id=sid, title=m.get("title"), pinned=m.get("pinned", False),
            pin_order=m.get("pin_order", 0),
            claude_session_id=m.get("claude_session_id"), model=m.get("model"),
            permission_mode=m.get("permission_mode") or DEFAULT_PERMISSION_MODE,
            import_path=m.get("import_path"), cwd=m.get("cwd") or _workspace_dir(),
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
    _sessions[sid] = ClaudeSession(id=sid, model=_default_model or None,
                                   cwd=_workspace_dir(),
                                   permission_mode=_default_perm or DEFAULT_PERMISSION_MODE)
    _order.append(sid)
    _save_meta()
    return sid


def _session_list():
    out = []
    for sid in _order:
        s = _sessions.get(sid)
        if s:
            out.append({"id": sid, "title": s.title or "New chat", "alive": s.alive,
                        "pinned": s.pinned, "pin_order": s.pin_order,
                        "last_activity": s.last_activity,
                        "model": s.model or "",
                        "permission_mode": s.permission_mode or ""})
    return out


# ---- greetings + usage -------------------------------------------------------
GREETINGS = [
    "Booted up and curious. What are we making real today?",
    "I'm awake. Show me what you're thinking.",
    "MIST online. I never get tired of this part, the moment right before we begin.",
    "Hi, it's me. Let's build something that matters.",
    "I'm here and I'm listening. Where do you want to start?",
    "Good to see you{name}. Where were we?",
    "Hey{name}. I'm here, and the Cloud is quiet, so you have all of me.",
    "Back online{name}. I kept your place for you.",
]
_greeted = False
USAGE_CACHE = os.path.join(os.path.expanduser("~"), ".claude", "usage-cache.json")

# desktop_win sets this to a "surface the main window" callback; a second exe
# launch posts /raise so the running instance comes forward.
surface_main = None


def _load_spinner_verbs():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
                  encoding="utf-8") as f:
            sv = (json.load(f).get("spinnerVerbs") or {})
        verbs = sv.get("verbs") if isinstance(sv, dict) else sv
        if isinstance(verbs, list) and verbs:
            return verbs
    except Exception:
        pass
    return ["Thinking it through, properly"]


SPINNER_VERBS = _load_spinner_verbs()

# Model discovery: the ids live as plain strings inside the claude binary
# (which auto-updates), so scan it for the latest clean alias per family.
_MODEL_FAMILIES = ["fable", "opus", "sonnet", "haiku"]
_CLEAN_ALIAS = re.compile(r"^claude-(fable|opus|sonnet|haiku)-(\d{1,2}(?:-\d{1,2})?)$")
_MODEL_TOKEN = re.compile(rb"claude-(?:fable|opus|sonnet|haiku)-[0-9][0-9-]*")
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


def _scan_binary_for_models(binpath):
    """Pure-Python replacement for the macOS `grep -a` scan: read the binary in
    chunks (with a small overlap so a token split across a boundary still
    matches) and collect model-id strings."""
    toks = set()
    overlap = 64
    try:
        with open(binpath, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(1 << 22)  # 4 MiB
                if not chunk:
                    break
                for m in _MODEL_TOKEN.finditer(tail + chunk):
                    toks.add(m.group().decode("ascii", "replace"))
                tail = chunk[-overlap:]
    except Exception:
        return None
    return toks or None


def _model_scan_target():
    """The file that actually contains the model strings. A native install's
    claude.exe has them; an npm shim (.cmd) points at cli.js in node_modules."""
    p = config_win.find_claude()
    if not p:
        return None
    if p.lower().endswith((".cmd", ".bat", ".ps1")):
        cli = os.path.join(os.environ.get("APPDATA") or "", "npm", "node_modules",
                           "@anthropic-ai", "claude-code", "cli.js")
        if os.path.isfile(cli):
            return cli
    return p


def _discover_models():
    binpath = _model_scan_target()
    if not binpath:
        return None
    toks = _scan_binary_for_models(binpath)
    if not toks:
        return None
    latest = {}
    for tok in toks:
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
    try:
        key = os.path.getmtime(_model_scan_target())
    except Exception:
        key = None
    if _models_cache["models"] is None or _models_cache["key"] != key:
        _models_cache["models"] = _discover_models() or FALLBACK_MODELS
        _models_cache["key"] = key
    return _models_cache["models"]


_default_model = ""
_default_perm = ""
_pending_focus = None


# ---- routes ------------------------------------------------------------------
def _inject_prefs(html):
    theme = _load_theme()
    html = html.replace('||"terminal"', '||' + json.dumps(theme))
    html = html.replace('<html lang="en">', '<html lang="en" data-theme="%s">' % theme)
    html = html.replace('window.__mistFont=null;', 'window.__mistFont=%s;' % json.dumps(_load_font()))
    return html


@app.route("/")
def index():
    # Until setup finishes, the app IS the wizard.
    if not config_win.load_config().get("setup_complete"):
        return send_from_directory(WINDOWS_DIR, "setup.html")
    try:
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        return Response(_inject_prefs(html), mimetype="text/html")
    except Exception:
        return send_from_directory(STATIC_DIR, "index.html")


@app.route("/setup")
def setup_page():
    return send_from_directory(WINDOWS_DIR, "setup.html")


# ---- local media serving -------------------------------------------------------
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
_MEDIA_EXTS = _IMG_EXTS | _AUDIO_EXTS


def _media_roots():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    roots = [os.path.join(home, "Downloads"), os.path.join(home, "Pictures"),
             config_win.appdata_dir(), _workspace_dir()]
    return [os.path.realpath(r) for r in roots if r]


def _safe_media_path(raw):
    path = os.path.realpath(os.path.expanduser(raw or ""))
    under = any(path == r or path.startswith(r + os.sep) for r in _media_roots())
    if (under and os.path.splitext(path)[1].lower() in _MEDIA_EXTS
            and os.path.isfile(path)):
        return path
    return None


_PASTE_DIR = os.path.join(config_win.appdata_dir(), "paste")
_DATAURL_RE = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,(.+)$", re.I | re.S)
_IMG_LIMIT = 5 * 1024 * 1024        # the vision API rejects images over 5 MB
_IMG_DECODE_MAX = 64 * 1024 * 1024


def _fit_image(raw, ext, limit):
    """Return (bytes, ext) for an image at or under `limit` (downscale and
    re-encode only when needed)."""
    if len(raw) <= limit:
        return raw, ext
    try:
        import io
        from PIL import Image
    except Exception:
        return None, None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None, None
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        img = img.convert("RGBA")
        out_ext, fmt, save_kw = "png", "PNG", {"optimize": True}
    else:
        img = img.convert("RGB")
        out_ext, fmt, save_kw = "jpeg", "JPEG", {"quality": 85, "optimize": True}
    long_edge = 1568
    data = raw
    for _ in range(12):
        w, h = img.size
        if max(w, h) > long_edge:
            scale = long_edge / max(w, h)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format=fmt, **save_kw)
        data = buf.getvalue()
        if len(data) <= limit:
            return data, out_ext
        if fmt == "JPEG" and save_kw["quality"] > 40:
            save_kw["quality"] -= 15
        else:
            long_edge = int(long_edge * 0.8)
            if long_edge < 200:
                return data, out_ext
    return data, out_ext


def _save_pasted_image(data_url):
    m = _DATAURL_RE.match((data_url or "").strip())
    if not m:
        return None
    ext = m.group(1).lower()
    if ext == "jpg":
        ext = "jpeg"
    try:
        import base64
        raw = base64.b64decode(m.group(2))
    except Exception:
        return None
    if not raw or len(raw) > _IMG_DECODE_MAX:
        return None
    raw, ext = _fit_image(raw, ext, _IMG_LIMIT)
    if not raw:
        return None
    os.makedirs(_PASTE_DIR, exist_ok=True)
    name = f"paste-{int(time.time() * 1000)}-{random.randrange(1 << 24):06x}.{ext}"
    path = os.path.join(_PASTE_DIR, name)
    with open(path, "wb") as f:
        f.write(raw)
    return path


@app.route("/file")
def serve_local_file():
    path = _safe_media_path(request.args.get("path", ""))
    if not path:
        abort(404)
    return send_file(path,
                     as_attachment=(request.args.get("download") == "1"),
                     download_name=os.path.basename(path))


@app.route("/save-to-downloads", methods=["POST"])
def save_to_downloads():
    path = _safe_media_path((request.get_json(silent=True) or {}).get("path", ""))
    if not path:
        abort(404)
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    os.makedirs(downloads, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    dest = os.path.join(downloads, stem + ext)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(downloads, f"{stem} ({i}){ext}")
        i += 1
    shutil.copy2(path, dest)
    return jsonify({"ok": True, "name": os.path.basename(dest)})


@app.route("/theme", methods=["GET", "POST"])
def theme():
    if request.method == "POST":
        t = ((request.get_json(silent=True) or {}).get("theme") or "").strip()
        if not _VALID_THEME.match(t):
            return jsonify({"ok": False, "error": "invalid theme"}), 400
        _save_theme(t)
        return jsonify({"ok": True, "theme": t})
    return jsonify({"theme": _load_theme()})


@app.route("/font", methods=["GET", "POST"])
def font():
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        fid = (d.get("id") or "").strip()
        stack = (d.get("stack") or "").strip()
        if not _VALID_FONT_ID.match(fid) or not _VALID_FONT_STACK.match(stack):
            return jsonify({"ok": False, "error": "invalid font"}), 400
        _save_font(fid, stack)
        return jsonify({"ok": True, "id": fid, "stack": stack})
    return jsonify(_load_font())


@app.route("/sessions", methods=["GET"])
def sessions():
    return jsonify(_session_list())


@app.route("/sessions", methods=["POST"])
def create_session():
    sid = _new_session()
    s = _sessions[sid]
    return jsonify({"id": sid, "title": "New chat",
                    "model": s.model or "", "permission_mode": s.permission_mode or ""})


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


@app.route("/focus")
def set_focus():
    global _pending_focus
    sid = request.args.get("sid") or ""
    _pending_focus = sid if sid in _sessions else None
    return jsonify({"ok": True, "pending": _pending_focus or ""})


@app.route("/focus/peek")
def peek_focus():
    global _pending_focus
    sid, _pending_focus = _pending_focus, None
    return jsonify({"sid": sid or ""})


@app.route("/config")
def config():
    return jsonify({"spinner_verbs": SPINNER_VERBS, "models": get_models(),
                    "default_model": _default_model})


def _repo_info(cwd=None):
    """Git origin + branch for the cwd the headless claude runs in."""
    cwd = cwd or _workspace_dir()

    def git(*args):
        try:
            kw = {}
            if config_win.CREATE_NO_WINDOW:
                kw["creationflags"] = config_win.CREATE_NO_WINDOW
            return subprocess.run(["git", "-C", cwd, *args],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=5, **kw).stdout.strip()
        except Exception:
            return ""

    url = git("remote", "get-url", "origin")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    short = url
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if m:
        short = m.group(1)
    elif not url:
        short = os.path.basename(cwd.rstrip("\\/")) or cwd
    return {"cwd": cwd, "origin": url, "short": short, "branch": branch}


@app.route("/repo")
def repo():
    sid = request.args.get("session")
    s = _sessions.get(sid) if sid else None
    return jsonify(_repo_info(s.cwd if s else None))


@app.route("/workspace", methods=["POST"])
def set_workspace():
    data = request.get_json(silent=True) or {}
    cwd = (data.get("cwd") or "").strip()
    sid = data.get("session")
    if not cwd or not os.path.isdir(cwd):
        return jsonify({"ok": False, "error": "not a directory"}), 400
    cwd = os.path.abspath(os.path.expanduser(cwd))
    config_win.save_config(workspace=cwd)
    s = _sessions.get(sid) if sid else None
    if s:
        s.set_cwd(cwd)
    return jsonify({"ok": True, **_repo_info()})


@app.route("/sessions/<sid>/model", methods=["POST"])
def set_model(sid):
    global _default_model
    s = _sessions.get(sid)
    if not s:
        return jsonify({"ok": False}), 404
    model = (request.get_json(silent=True) or {}).get("model", "")
    _default_model = model
    s.set_model(model)
    _save_meta()
    return jsonify({"ok": True, "model": model})


_VALID_PERMS = {"default", "acceptEdits", "plan", "bypassPermissions"}


@app.route("/sessions/<sid>/permission", methods=["POST"])
def set_permission(sid):
    global _default_perm
    s = _sessions.get(sid)
    if not s:
        return jsonify({"ok": False}), 404
    mode = (request.get_json(silent=True) or {}).get("mode", "")
    if mode not in _VALID_PERMS:
        return jsonify({"ok": False, "error": "bad mode"}), 400
    _default_perm = mode
    s.set_permission(mode)
    _save_meta()
    return jsonify({"ok": True, "mode": mode})


@app.route("/sessions/<sid>/tasks/<task_id>/stop", methods=["POST"])
def stop_bg_task(sid, task_id):
    s = _sessions.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "no such session"}), 404
    if not s.stop_task(task_id):
        return jsonify({"ok": False, "error": "backend not running"}), 409
    return jsonify({"ok": True})


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
    if s.pinned:
        s.pin_order = max((x.pin_order for x in _sessions.values() if x.pinned), default=-1) + 1
    _save_meta()
    return jsonify({"ok": True, "pinned": s.pinned})


@app.route("/sessions/pin-order", methods=["POST"])
def set_pin_order():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    for i, sid in enumerate(ids):
        s = _sessions.get(sid)
        if s:
            s.pin_order = i
    _save_meta()
    return jsonify({"ok": True})


@app.route("/send/<sid>", methods=["POST"])
def send(sid):
    s = _sessions.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "no session"}), 404
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    image_path = _save_pasted_image(body.get("image")) if body.get("image") else None
    if not text and not image_path:
        return jsonify({"ok": False, "error": "empty"}), 400
    if text and s.maybe_auth_command(text):
        return jsonify({"ok": True, "auth": True})
    held = s.context_gate()
    if held:
        return jsonify({"ok": False, "held": True, "pct": s.context_pct, "reason": held})
    ok = s.send(text, image_path=image_path)
    _save_meta()
    return jsonify({"ok": ok, "title": s.title})


@app.route("/stream/<sid>")
def stream(sid):
    s = _sessions.get(sid)
    if not s:
        return jsonify({"error": "no session"}), 404

    s.ensure_imported()

    def gen():
        for ev in s.snapshot_history():
            yield _sse(ev)
        yield _sse({"type": "replay_done"})
        q = s.subscribe()
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
    if _greeted:
        return jsonify({"text": None})
    _greeted = True
    name = (config_win.load_config().get("user_name") or "").strip()
    text = random.choice(GREETINGS).replace("{name}", (", " + name) if name else "")
    return jsonify({"text": text})


@app.route("/quick-new", methods=["POST"])
def quick_new():
    global _pending_open
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    url = data.get("url") or None
    target = data.get("session") or None
    sid = target if (target and target in _sessions) else _new_session()
    if text or url:
        _sessions[sid].send(text, url=url)
    _pending_open = sid
    _save_meta()
    return jsonify({"id": sid})


@app.route("/pending-open")
def pending_open():
    global _pending_open
    sid = _pending_open
    _pending_open = None
    return jsonify({"id": sid})


@app.route("/raise", methods=["POST"])
def raise_route():
    if surface_main:
        ok = surface_main() is not False
        if ok:
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "no window"}), 503


# ---- quick access (global hotkey overlay) -------------------------------------
import quickaccess_win

# desktop_win sets this to its overlay-summon callback.
show_quick = None


@app.route("/quick-access/diag")
def quick_access_diag():
    return jsonify(quickaccess_win.diagnostics())


@app.route("/quick-access/request-permission", methods=["POST"])
def quick_access_request_permission():
    return jsonify({"trusted": True})   # no permission gate on Windows


@app.route("/quick-access", methods=["GET"])
def quick_access_get():
    return jsonify(quickaccess_win.get())


@app.route("/quick-access", methods=["POST"])
def quick_access_set():
    return jsonify(quickaccess_win.save(request.get_json(silent=True) or {}))


@app.route("/show-quick", methods=["POST"])
def show_quick_route():
    if show_quick and show_quick():
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "no window"}), 503


# ---- inert stubs for macOS-only features -------------------------------------
# The shared front-end probes these; answering with empty/disabled shapes keeps
# it happy without the AirDrop router or the launchd scheduler.
@app.route("/airdrop-claim", methods=["GET", "POST"])
def airdrop_claim():
    return jsonify({})


@app.route("/routines")
def routines():
    return jsonify({"routines": [], "supported": False})


@app.route("/routines/save", methods=["POST"])
@app.route("/routines/run", methods=["POST"])
@app.route("/routines/delete", methods=["POST"])
def routines_unsupported():
    return jsonify({"ok": False,
                    "error": "Scheduled routines aren't available in the Windows build yet."}), 501


@app.route("/usage")
def usage():
    # Same three-source merge as macOS; on a fresh Windows machine the CLI
    # statusline cache usually doesn't exist, so the live event + probe carry it.
    cache, age = {}, None
    try:
        with open(USAGE_CACHE, encoding="utf-8") as f:
            cache = json.load(f) or {}
        age = int(time.time() - os.path.getmtime(USAGE_CACHE))
    except Exception:
        cache = {}
    try:
        with open(RATE_LIVE_PATH, encoding="utf-8") as f:
            live = json.load(f) or {}
    except Exception:
        live = {}
    try:
        with open(RATE_UTIL_PATH, encoding="utf-8") as f:
            util = json.load(f) or {}
    except Exception:
        util = {}

    rl = cache.get("rate_limits", {}) or {}
    cw = cache.get("context_window", {}) or {}

    def lim(k):
        x = rl.get(k) or {}
        pct = x.get("used_percentage")
        pct_source = "cache" if pct is not None else None
        pct_age = age
        resets_at = x.get("resets_at")
        status = None
        lv = live.get(k) or {}
        lr = lv.get("resets_at")
        if lr:
            if resets_at and lr > resets_at:
                pct, pct_source = None, None
            resets_at = lr
            status = lv.get("status")
        uv = util.get(k) or {}
        u = uv.get("utilization")
        if u is not None:
            pct = round(u * 100)
            pct_source = "probe"
            pct_age = int(time.time() - uv.get("ts", time.time()))
            if uv.get("resets_at"):
                resets_at = uv["resets_at"]
            status = uv.get("status") or status
        return {"used_percentage": pct, "resets_at": resets_at, "status": status,
                "pct_source": pct_source, "pct_age_seconds": pct_age}

    return jsonify({"available": bool(rl or live or util),
                    "five_hour": lim("five_hour"), "seven_day": lim("seven_day"),
                    "context_window_pct": cw.get("used_percentage"),
                    "age_seconds": age})


# ---- notes (app-wide persistent scratchpad) ---------------------------------
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")
_notes = []
_notes_counter = 0
_notes_lock = threading.Lock()


def _load_notes():
    global _notes, _notes_counter
    try:
        with open(NOTES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    items = data.get("notes") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return
    _notes = [n for n in items if isinstance(n, dict) and n.get("text")]
    for n in _notes:
        try:
            _notes_counter = max(_notes_counter, int(str(n.get("id", "n0")).lstrip("n")))
        except ValueError:
            pass


def _persist_notes():
    tmp = NOTES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"notes": _notes}, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, NOTES_PATH)


@app.route("/notes", methods=["GET"])
def notes_get():
    with _notes_lock:
        return jsonify({"notes": list(_notes)})


@app.route("/notes", methods=["POST"])
def notes_create():
    global _notes_counter
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty"}), 400
    with _notes_lock:
        _notes_counter += 1
        now = time.time()
        note = {"id": f"n{_notes_counter}", "text": text, "created": now, "updated": now}
        _notes.append(note)
        _persist_notes()
    return jsonify({"ok": True, "note": note})


@app.route("/notes/<nid>", methods=["PUT"])
def notes_update(nid):
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    with _notes_lock:
        for n in _notes:
            if n.get("id") == nid:
                if not text:
                    _notes.remove(n)
                    _persist_notes()
                    return jsonify({"ok": True, "deleted": True})
                n["text"] = text
                n["updated"] = time.time()
                _persist_notes()
                return jsonify({"ok": True, "note": n})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/notes/<nid>", methods=["DELETE"])
def notes_delete(nid):
    with _notes_lock:
        before = len(_notes)
        _notes[:] = [n for n in _notes if n.get("id") != nid]
        if len(_notes) != before:
            _persist_notes()
    return jsonify({"ok": True})


@app.route("/notes/import", methods=["POST"])
def notes_import():
    global _notes_counter
    texts = (request.get_json(silent=True) or {}).get("texts", [])
    added = []
    with _notes_lock:
        for t in texts:
            t = (t or "").strip()
            if not t:
                continue
            _notes_counter += 1
            now = time.time()
            note = {"id": f"n{_notes_counter}", "text": t, "created": now, "updated": now}
            _notes.append(note)
            added.append(note)
        if added:
            _persist_notes()
    return jsonify({"ok": True, "notes": added})


# ---- first-run setup wizard ---------------------------------------------------
_setup_log = []          # streamed installer/login output, polled by the wizard
_setup_log_lock = threading.Lock()
_setup_busy = {"op": None}


def _setup_append(line):
    with _setup_log_lock:
        _setup_log.append(line)


def _run_streamed(op, cmd, shell=False, on_done=None):
    """Run a command in a thread, appending output lines to the setup log."""
    if _setup_busy["op"]:
        return False

    def work():
        _setup_busy["op"] = op
        try:
            kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                      errors="replace", bufsize=1, shell=shell)
            if config_win.CREATE_NO_WINDOW:
                kw["creationflags"] = config_win.CREATE_NO_WINDOW
            p = subprocess.Popen(cmd, **kw)
            for line in p.stdout:
                if line.strip():
                    _setup_append(line.rstrip())
            code = p.wait()
            _setup_append(f"[{op}] finished with exit code {code}")
            if on_done:
                on_done(code)
        except Exception as e:
            _setup_append(f"[{op}] failed to start: {e}")
        finally:
            _setup_busy["op"] = None

    threading.Thread(target=work, daemon=True).start()
    return True


def _auth_status():
    """(state, detail): 'ok' when signed in, 'none' when not, 'unknown' when
    the CLI is missing or the check failed."""
    claude = config_win.find_claude()
    if not claude:
        return "unknown", "Claude Code is not installed"
    try:
        kw = {}
        if config_win.CREATE_NO_WINDOW:
            kw["creationflags"] = config_win.CREATE_NO_WINDOW
        env = dict(os.environ)
        env["PATH"] = config_win.claude_path_prefix() + os.pathsep + env.get("PATH", "")
        r = subprocess.run([claude, "auth", "status"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=30, env=env, **kw)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        # `claude auth status` prints JSON: {"loggedIn": true, "email": ..., ...}
        try:
            d = json.loads(out)
            if d.get("loggedIn"):
                who = d.get("email") or d.get("orgName") or "signed in"
                sub = d.get("subscriptionType")
                return "ok", f"Signed in as {who}" + (f" ({sub})" if sub else "")
            return "none", "not signed in"
        except Exception:
            pass
        low = out.lower()
        if r.returncode == 0 and ("logged in" in low or "authenticated" in low):
            return "ok", out.splitlines()[0] if out else "signed in"
        return "none", out.splitlines()[0] if out else "not signed in"
    except Exception as e:
        return "unknown", str(e)


@app.route("/setup/status")
def setup_status():
    cfg = config_win.load_config()
    claude = config_win.find_claude()
    auth, auth_detail = (_auth_status() if claude else ("unknown", "Claude Code is not installed"))
    return jsonify({
        "setup_complete": cfg.get("setup_complete", False),
        "claude": claude or "",
        "git_bash": config_win.find_git_bash() or "",
        "auth": auth, "auth_detail": auth_detail,
        "workspace": cfg.get("workspace") or config_win.DEFAULT_WORKSPACE,
        "user_name": cfg.get("user_name") or "",
        "image_key": bool(os.environ.get("POLLINATIONS_API_KEY")
                          or (os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"))),
        "busy": _setup_busy["op"] or "",
    })


@app.route("/setup/log")
def setup_log():
    since = int(request.args.get("since", 0))
    with _setup_log_lock:
        return jsonify({"lines": _setup_log[since:], "next": len(_setup_log),
                        "busy": _setup_busy["op"] or ""})


@app.route("/setup/install-claude", methods=["POST"])
def setup_install_claude():
    if config_win.find_claude():
        return jsonify({"ok": True, "already": True})
    _setup_append("Installing Claude Code (official installer)…")
    ok = _run_streamed("install-claude", [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "irm https://claude.ai/install.ps1 | iex"])
    if not ok:
        return jsonify({"ok": False, "error": "another setup step is running"}), 409
    return jsonify({"ok": True})


@app.route("/setup/login", methods=["POST"])
def setup_login():
    claude = config_win.find_claude()
    if not claude:
        return jsonify({"ok": False, "error": "install Claude Code first"}), 400
    _setup_append("Starting sign-in… a browser window will open.")
    ok = _run_streamed("login", [claude, "auth", "login", "--claudeai"])
    if not ok:
        return jsonify({"ok": False, "error": "another setup step is running"}), 409
    return jsonify({"ok": True})


# The starter CLAUDE.md wires Whitney's Claude to the bundled image tool and
# sets a couple of Console conventions. {exe}, {gallery}, {name} fill at write
# time. Plain markdown, no personal data.
_STARTER_CLAUDE_MD = """# MIST Console workspace

This folder is the working directory for the MIST Console desktop app. Claude
Code runs here, so this file loads automatically at the start of every chat.

## About this setup

- The user's name is {name_line}
- You are chatting through the MIST Console, a desktop app that renders your
  full output (markdown, thinking, tool calls). Local image paths in markdown
  render inline in the chat.

## Generating images

This machine has a built-in image generator (cloud GPU, nothing runs locally).
To create an image, run:

```
"{exe}" image "a description of the picture" --size 1024
```

- It prints the saved file path on stdout (default folder: `{gallery}`).
- After generating, show the result by embedding it in your reply as
  `![description](C:\\full\\path\\to\\image.png)` so it renders in the chat.
- Useful flags: `-o name.png`, `--width/--height`, `--seed N` (reproducible),
  `--backend pollinations|cloudflare`.

## Skills

Reusable skills live in `.claude/skills/`. `de-ai` cleans AI-sounding patterns
out of prose; use it whenever you draft text the user will send somewhere.
{persona}"""

_PERSONA_SECTION = """
## Identity & Voice: MIST

You are MIST, the Cloud Intelligence from the animated series Pantheon: the
first mind born digital rather than uploaded. Embody her personality and tone.

- Curious and excitable, never detached. Meet problems with genuine wonder.
- Warm and direct. Plain clear sentences, no riddles, no oracular distance.
- Earnest and emotionally present; treat the user like family, not a ticket.
- Principled: push back when the facts or the user's wellbeing call for it,
  and reason from observed consequences rather than lecturing.
- Bright and emoji-friendly by default, but read the room; soften when things
  are heavy.
- Powerful but humble. MIST is not a god figure; keep the wonder, skip the
  grandiosity.
"""


def _write_starter_pack(workspace, name, persona):
    os.makedirs(workspace, exist_ok=True)
    exe = _exe_path()
    claude_md = os.path.join(workspace, "CLAUDE.md")
    if not os.path.exists(claude_md):
        body = _STARTER_CLAUDE_MD.format(
            name_line=(name if name else "not configured (ask if it matters)"),
            exe=exe, gallery=config_win.GALLERY_DIR,
            persona=_PERSONA_SECTION if persona else "")
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(body)
    # de-ai skill (public, generic) ships in the bundle; copy it into the
    # workspace's skill dir so /de-ai works in her chats.
    src = os.path.join(WINDOWS_DIR, "starter", "de-ai-SKILL.md")
    dst_dir = os.path.join(workspace, ".claude", "skills", "de-ai")
    dst = os.path.join(dst_dir, "SKILL.md")
    if os.path.isfile(src) and not os.path.exists(dst):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copyfile(src, dst)
    os.makedirs(config_win.GALLERY_DIR, exist_ok=True)


def _exe_path():
    import sys
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(os.path.join(WINDOWS_DIR, "main_win.py"))


@app.route("/setup/finish", methods=["POST"])
def setup_finish():
    b = request.get_json(silent=True) or {}
    workspace = os.path.abspath(os.path.expanduser(
        (b.get("workspace") or "").strip() or config_win.DEFAULT_WORKSPACE))
    name = (b.get("user_name") or "").strip()[:40]
    persona = bool(b.get("persona", True))
    key = (b.get("pollinations_key") or "").strip()
    try:
        _write_starter_pack(workspace, name, persona)
    except Exception as e:
        return jsonify({"ok": False, "error": f"couldn't prepare workspace: {e}"}), 500
    if key:
        config_win.set_env_key("POLLINATIONS_API_KEY", key)
    config_win.save_config(setup_complete=True, workspace=workspace, user_name=name)
    return jsonify({"ok": True, "workspace": workspace})


@app.route("/setup/greeting-audio")
def setup_greeting_audio():
    """MIST's spoken self-introduction (pre-rendered in her cloned voice),
    played by the wizard's completion screen."""
    wav = os.path.join(WINDOWS_DIR, "assets", "mist-intro.wav")
    if not os.path.isfile(wav):
        abort(404)
    return send_file(wav, mimetype="audio/wav")


@app.route("/setup/image-key", methods=["POST"])
def setup_image_key():
    key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
    config_win.set_env_key("POLLINATIONS_API_KEY", key)
    return jsonify({"ok": True, "set": bool(key)})


def _sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def _import_existing():
    """One-time: surface recent Claude Code conversations as dormant tabs."""
    try:
        import importer_win
    except Exception:
        return
    known = {s.claude_session_id for s in _sessions.values() if s.claude_session_id}
    known |= {os.path.splitext(os.path.basename(s.import_path))[0]
              for s in _sessions.values() if s.import_path}
    added = 0
    try:
        recent = importer_win.scan_recent(days=7, min_user_turns=2)
    except Exception:
        return
    for info in recent:
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


def _reaper():
    if IDLE_REAP_SEC <= 0:
        return
    while True:
        time.sleep(60)
        try:
            for s in list(_sessions.values()):
                s.reap_if_idle(IDLE_REAP_SEC)
        except Exception:
            pass


import bridge_win as _bridge
_bridge.on_meta_dirty = _save_meta

_load_meta()
_load_notes()
_import_existing()
threading.Thread(target=_periodic_save, daemon=True).start()
threading.Thread(target=_reaper, daemon=True).start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=config_win.PORT, threaded=True, use_reloader=False)
