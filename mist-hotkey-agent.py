# /// script
# dependencies = ["pyobjc-framework-Cocoa"]
# ///
"""
mist-hotkey-agent.py — a tiny always-on background process that owns the MIST
quick-access gesture (double-tap Option), independent of whether MIST herself is
running. On the gesture:
  - if MIST is up  → POST /show-quick (summon the glowing overlay)
  - if MIST is down → launch her, wait for her to bind, then summon the overlay

Runs as a LaunchAgent (RunAtLoad + KeepAlive), windowless (accessory app), so it
survives MIST being closed. Reuses quickaccess.py for the gesture detection.
"""
import json
import os
import subprocess
import threading
import time
import urllib.request

import quickaccess  # shared gesture detection + config

MIST_APP = os.path.expanduser("~/Desktop/Apps/MIST Console.app")
BASE = "http://127.0.0.1:5014"
# marker that tells MIST to start with her main window hidden (quiet quick-access
# launch) — only the glowing overlay should appear, not the console window.
QUIET_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".quiet-launch")


def _mist_up():
    try:
        urllib.request.urlopen(BASE + "/", timeout=0.6)
        return True
    except Exception:
        return False


def _frontmost_app():
    """The app that's frontmost right now — for the URL attachment. NSWorkspace
    needs no permission, and at gesture time the browser is still frontmost."""
    try:
        from AppKit import NSWorkspace
        a = NSWorkspace.sharedWorkspace().frontmostApplication()
        return a.localizedName() if a is not None else None
    except Exception:
        return None


def _post_quick(app):
    try:
        req = urllib.request.Request(
            BASE + "/show-quick", data=json.dumps({"app": app}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=3)
        return getattr(r, "status", 200) == 200
    except Exception:
        return False


def _handle():
    app = _frontmost_app()                      # capture before anything steals focus
    if not _mist_up():
        try:
            open(QUIET_MARKER, "w").close()    # launch with the console window hidden
        except Exception:
            pass
        subprocess.Popen(["open", MIST_APP])   # MIST is closed → open her
        for _ in range(50):                     # wait up to ~25s for her to bind
            if _mist_up():
                break
            time.sleep(0.5)
    for _ in range(25):                         # retry until show_quick is registered
        if _post_quick(app):
            return
        time.sleep(0.4)


def on_hotkey():
    threading.Thread(target=_handle, daemon=True).start()


def _reload_loop():
    # pick up enable/disable changes made in MIST's settings
    while True:
        time.sleep(10)
        quickaccess.load()


_status_item = None
_menu_target = None
LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "mist-logo.png")


def _setup_status_item():
    """A menu bar icon (the MIST diamond) whose menu adjusts the quick-access
    settings. Lives in the agent so it's present even when MIST is closed."""
    from AppKit import (NSStatusBar, NSVariableStatusItemLength, NSImage,
                        NSMenu, NSMenuItem)
    from Foundation import NSObject, NSMakeSize
    global _status_item, _menu_target

    ON, OFF = 1, 0   # NSControlStateValueOn / Off

    class _MenuTarget(NSObject):
        def toggleEnabled_(self, sender):
            cfg = quickaccess.get()
            cfg["enabled"] = not cfg.get("enabled", True)
            quickaccess.save(cfg)
            sender.setState_(ON if cfg["enabled"] else OFF)

        def openMist_(self, sender):
            subprocess.Popen(["open", MIST_APP])

        def grantAx_(self, sender):
            quickaccess.request_permission()

    _menu_target = _MenuTarget.alloc().init()

    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    button = item.button()
    img = NSImage.alloc().initWithContentsOfFile_(LOGO)
    if img is not None:
        img.setSize_(NSMakeSize(20, 16))   # ~menu-bar height, keeps the diamond's aspect
        img.setTemplate_(True)             # render in the menu-bar tint (crisp in light+dark)
        button.setImage_(img)
    else:
        button.setTitle_("✦")          # ✦ fallback
    button.setToolTip_("MIST quick access")

    menu = NSMenu.alloc().init()

    def _item(title, sel, target=_menu_target, enabled=True, state=None):
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
        if sel:
            mi.setTarget_(target)
        mi.setEnabled_(enabled)
        if state is not None:
            mi.setState_(state)
        return mi

    menu.addItem_(_item("MIST Quick Access", None, enabled=False))
    menu.addItem_(_item("Enabled", "toggleEnabled:",
                        state=ON if quickaccess.get().get("enabled", True) else OFF))
    menu.addItem_(_item("Gesture:  Double-tap ⌥ Option", None, enabled=False))
    menu.addItem_(NSMenuItem.separatorItem())
    menu.addItem_(_item("Grant Accessibility…", "grantAx:"))
    menu.addItem_(_item("Open MIST Console", "openMist:"))
    item.setMenu_(menu)
    _status_item = item
    print("mist-hotkey-agent: status item installed", flush=True)


def main():
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no dock icon

    quickaccess.load()
    if quickaccess.ax_trusted() is not True:
        quickaccess.request_permission()       # register + prompt for Accessibility
    quickaccess.install(on_hotkey)
    try:
        _setup_status_item()
    except Exception as e:
        print("mist-hotkey-agent: status item failed:", e, flush=True)
    threading.Thread(target=_reload_loop, daemon=True).start()

    print("mist-hotkey-agent: running, ax_trusted =", quickaccess.ax_trusted(), flush=True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
