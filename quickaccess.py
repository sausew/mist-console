"""
quickaccess.py — summon the MIST quick-entry overlay by DOUBLE-TAPPING the
Option (⌥) key. Detected via NSEvent flagsChanged monitors (global = from any
app, local = when MIST is focused). The global monitor needs macOS Accessibility
permission; request_permission() prompts + registers the app.
"""
import ctypes
import json
import os

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "quick_access.json")
DEFAULT = {"enabled": True}   # gesture is fixed: double-tap Option

_OPTION_FLAG = 1 << 19            # NSEventModifierFlagOption
_OPTION_KEYCODES = (58, 61)      # left / right Option
_MASK_FLAGS = 1 << 12            # NSEventMaskFlagsChanged
_DOUBLE = 0.40                   # max seconds between the two taps

_cfg = dict(DEFAULT)
_show_fn = None
_gmon = None                     # keep monitor refs alive
_lmon = None
_last = [0.0]
_installed = False
_appserv = None


# ---- config ------------------------------------------------------------------
def load():
    global _cfg
    try:
        with open(CONFIG) as f:
            _cfg = {**DEFAULT, **json.load(f)}
    except Exception:
        _cfg = dict(DEFAULT)
    return dict(_cfg)


def get():
    return dict(_cfg)


def save(cfg):
    global _cfg
    _cfg = {**DEFAULT, **(cfg or {})}
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(_cfg, f)
    except Exception:
        pass
    return dict(_cfg)


# ---- accessibility -----------------------------------------------------------
def _ax():
    global _appserv
    if _appserv is None:
        _appserv = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        _appserv.AXIsProcessTrusted.restype = ctypes.c_bool
    return _appserv


def ax_trusted():
    try:
        return bool(_ax().AXIsProcessTrusted())
    except Exception as e:
        return "err:%s" % e


def request_permission():
    """Register the app for Accessibility and show the macOS grant dialog."""
    try:
        a = _ax()
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        k_prompt = ctypes.c_void_p.in_dll(a, "kAXTrustedCheckOptionPrompt")
        k_true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        key_cb = ctypes.byref(ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        val_cb = ctypes.byref(ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
        keys = (ctypes.c_void_p * 1)(k_prompt)
        vals = (ctypes.c_void_p * 1)(k_true)
        d = cf.CFDictionaryCreate(None, keys, vals, 1, key_cb, val_cb)
        a.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        a.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        return bool(a.AXIsProcessTrustedWithOptions(d))
    except Exception as e:
        return "err:%s" % e


# ---- gesture detection -------------------------------------------------------
def _handle(event):
    if not _cfg.get("enabled"):
        return
    try:
        kc = int(event.keyCode())
        flags = int(event.modifierFlags())
        # Option key DOWN (its flag is set on the transition)
        if kc in _OPTION_KEYCODES and (flags & _OPTION_FLAG):
            t = float(event.timestamp())
            dt = t - _last[0]
            _last[0] = t
            if 0.04 < dt < _DOUBLE:     # second tap of a double-tap
                _last[0] = 0.0
                if _show_fn:
                    _show_fn()
    except Exception:
        pass


def install(show_fn):
    """Install the global + local flagsChanged monitors. Main thread."""
    global _show_fn, _gmon, _lmon, _installed
    _show_fn = show_fn
    try:
        from AppKit import NSEvent

        def gh(event):
            _handle(event)

        def lh(event):
            _handle(event)
            return event   # local monitor must return the event (don't consume)

        _gmon = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(_MASK_FLAGS, gh)
        _lmon = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(_MASK_FLAGS, lh)
        _installed = True
        print("quick-access: double-Option monitors installed, ax_trusted =",
              ax_trusted(), flush=True)
    except Exception as e:
        print("quick-access: install failed:", e, flush=True)


def reconfigure():
    pass   # _handle reads _cfg live; nothing to re-register


def diagnostics():
    return {"installed": _installed, "ax_trusted": ax_trusted(),
            "gesture": "double-option", "config": get()}
