"""
config_win.py: shared configuration for the Windows build of MIST Console.

Everything user-specific lives under %APPDATA%\\MIST Console (config, chat
data, pasted images, API keys). Nothing is stored next to the exe, which for a
PyInstaller onefile build unpacks to a throwaway temp dir on every launch.
"""
import json
import os
import shutil
import sys

APP_NAME = "MIST Console"
PORT = 5014


def appdata_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = os.path.join(appdata_dir(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(appdata_dir(), "config.json")
ENV_PATH = os.path.join(appdata_dir(), ".env")   # API keys (image generator)

# Where generated images land. Pictures is the natural Windows home for them
# and the /file route allowlists it, so they render inline in chat.
GALLERY_DIR = os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), "Pictures", "MIST Gallery")

DEFAULT_WORKSPACE = os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), "MIST Workspace")

_DEFAULTS = {
    "setup_complete": False,
    "user_name": "",          # how MIST greets you; blank is fine
    "workspace": "",          # the folder claude runs in (its CLAUDE.md loads)
}


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        cfg = {}
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in _DEFAULTS})
    return out


def save_config(**updates):
    cfg = load_config()
    cfg.update({k: v for k, v in updates.items() if k in _DEFAULTS})
    tmp = CONFIG_PATH + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)
    return cfg


def load_env_file():
    """Pull KEY=VALUE lines from the app .env into the environment (existing
    real env vars win)."""
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def set_env_key(key, value):
    """Write or replace one KEY=VALUE line in the app .env."""
    lines = []
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f]
    except FileNotFoundError:
        pass
    lines = [l for l in lines if not l.strip().startswith(key + "=")]
    if value:
        lines.append(f"{key}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)


# ---- locating executables ------------------------------------------------------
# Windows GUI subprocesses each flash a console window unless told not to.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_CLAUDE_CANDIDATES = [
    # native installer (irm https://claude.ai/install.ps1 | iex)
    os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe"),
    # npm global installs
    os.path.join(os.environ.get("APPDATA") or "", "npm", "claude.cmd"),
    os.path.join(os.environ.get("APPDATA") or "", "npm", "claude"),
]


def find_claude():
    """Absolute path to the claude CLI, or None if not installed."""
    w = shutil.which("claude")
    if w:
        return w
    for c in _CLAUDE_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    return None


def find_git_bash():
    """Claude Code's Bash tool on Windows needs Git for Windows. Return the
    bash.exe path if present, else None."""
    for c in (shutil.which("bash"),
              r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe",
              os.path.expanduser(r"~\AppData\Local\Programs\Git\bin\bash.exe")):
        if c and os.path.isfile(c):
            return c
    return None


def claude_path_prefix():
    """Directories prepended to PATH for spawned claude processes, so the CLI
    and git are found regardless of how this exe was launched."""
    dirs = [os.path.join(os.path.expanduser("~"), ".local", "bin"),
            os.path.join(os.environ.get("APPDATA") or "", "npm")]
    gb = find_git_bash()
    if gb:
        dirs.append(os.path.dirname(gb))
    return os.pathsep.join(d for d in dirs if d)


def resource_path(rel):
    """Path to a bundled read-only asset (static/ etc). PyInstaller onefile
    unpacks bundled data to sys._MEIPASS; from source it is the repo root."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, rel)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel)
