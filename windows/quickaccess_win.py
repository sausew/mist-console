"""
quickaccess_win.py: summon the MIST quick-entry overlay with a global hotkey.

Windows counterpart of the macOS double-tap-Option agent, using RegisterHotKey
via ctypes (stdlib, no extra deps). Default gesture: Ctrl+Alt+Space, falling
back to Ctrl+Alt+M if something else already owns it. The hotkey lives for the
lifetime of the Console process (there is no separate always-on agent in this
build; summoning works while MIST is running).
"""
import ctypes
import json
import os
import threading

import config_win

CONFIG = os.path.join(config_win.DATA_DIR, "quick_access.json")
DEFAULT = {"enabled": True}

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_VK_SPACE = 0x20
_VK_M = 0x4D

# Registration candidates, tried in order. The label of whichever sticks is
# reported to the UI so the settings panel shows the real gesture.
_CANDIDATES = [
    (_MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, _VK_SPACE, "Ctrl+Alt+Space"),
    (_MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, _VK_M, "Ctrl+Alt+M"),
]

_cfg = dict(DEFAULT)
_show_fn = None
_registered_label = ""
_started = False


# ---- config ------------------------------------------------------------------
def load():
    global _cfg
    try:
        with open(CONFIG, encoding="utf-8") as f:
            _cfg = {**DEFAULT, **json.load(f)}
    except Exception:
        _cfg = dict(DEFAULT)
    return dict(_cfg)


def get():
    return {**_cfg, "supported": True, "gesture_label": _registered_label or "Ctrl+Alt+Space",
            "note": ("Press %s to summon MIST from anywhere while the Console is "
                     "running. Type a thought, hit Enter, and the main window opens "
                     "on that chat; Esc dismisses it."
                     % (_registered_label or "Ctrl+Alt+Space"))}


def save(body):
    global _cfg
    _cfg["enabled"] = bool((body or {}).get("enabled", True))
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump({"enabled": _cfg["enabled"]}, f)
    except Exception:
        pass
    return get()


def diagnostics():
    return {"supported": True, "ax_trusted": True,   # no permission gate on Windows
            "registered": bool(_registered_label), "gesture_label": _registered_label}


# ---- hotkey ------------------------------------------------------------------
def install(show_fn):
    """Register the global hotkey and start the message loop thread. show_fn is
    called (from that thread) on every press while enabled."""
    global _show_fn, _started
    _show_fn = show_fn
    if _started or os.name != "nt":
        return
    _started = True
    threading.Thread(target=_message_loop, daemon=True).start()


def _message_loop():
    global _registered_label
    user32 = ctypes.windll.user32
    hot_id = 1   # single hotkey id
    for mods, vk, label in _CANDIDATES:
        if user32.RegisterHotKey(None, hot_id, mods, vk):
            _registered_label = label
            break
    if not _registered_label:
        return

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_size_t),
                    ("time", ctypes.c_uint), ("pt_x", ctypes.c_long),
                    ("pt_y", ctypes.c_long)]

    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == _WM_HOTKEY and _cfg.get("enabled") and _show_fn:
            try:
                _show_fn()
            except Exception:
                pass
