"""
main_win.py: entry point for the MIST Console Windows exe.

  "MIST Console.exe"                  open the desktop app
  "MIST Console.exe" image "prompt"   generate an image (built-in mist-image)

The image subcommand exists so the bundled generator needs no separate Python
install: Claude (or the user) invokes the exe itself as a CLI.
"""
import os
import sys

# In a windowed (no-console) PyInstaller exe, stdout/stderr are None unless
# the caller redirected them (a shell capture does). Point them at devnull so
# a bare double-click invocation of the CLI path can't crash on print().
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "image":
        import mist_image_win
        mist_image_win.main(sys.argv[2:])
        return
    import desktop_win
    desktop_win.main()


if __name__ == "__main__":
    main()
