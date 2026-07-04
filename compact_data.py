#!/usr/bin/env python3
"""One-time compaction of data/*.jsonl through bridge._slim_event.

Historical session files were written before _record() slimmed events, so they
carry full tool_use_result payloads (see the note above _slim_event — data/
reached 21 GB). This rewrites each file with the same slimming the bridge now
applies at write time.

Safety:
- Files with a live open handle (active Console sessions) are skipped; rerun
  after a restart to catch them.
- Each file is rewritten to a .tmp sibling and atomically os.replace()d, so a
  crash mid-run never corrupts a transcript.
- Unparseable lines are kept verbatim.

Run:  python3 compact_data.py          (add --dry-run to only report sizes)
"""
import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import _slim_event, DATA_DIR  # noqa: E402


def paths_with_open_handles():
    try:
        out = subprocess.run(["lsof", "-Fn", "+D", DATA_DIR],
                             capture_output=True, text=True, timeout=60).stdout
        return {line[1:] for line in out.splitlines() if line.startswith("n")}
    except Exception:
        return set()  # lsof unavailable: fall through, atomic replace still safe


def compact(path):
    tmp = path + ".tmp"
    before = os.path.getsize(path)
    with open(path, "r") as src, open(tmp, "w") as dst:
        for line in src:
            try:
                dst.write(json.dumps(_slim_event(json.loads(line))) + "\n")
            except Exception:
                dst.write(line if line.endswith("\n") else line + "\n")
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, path)
    return before, os.path.getsize(path)


def main():
    dry = "--dry-run" in sys.argv
    open_now = paths_with_open_handles()
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.jsonl")),
                   key=os.path.getsize, reverse=True)
    total_before = total_after = 0
    skipped = []
    for path in files:
        size = os.path.getsize(path)
        if path in open_now:
            skipped.append(path)
            print(f"SKIP (open handle): {os.path.basename(path)} {size/1e6:.0f}MB")
            continue
        if dry:
            print(f"would compact: {os.path.basename(path)} {size/1e6:.0f}MB")
            continue
        before, after = compact(path)
        total_before += before
        total_after += after
        if before - after > 1_000_000:
            print(f"{os.path.basename(path)}: {before/1e6:.0f}MB -> {after/1e6:.0f}MB")
    print(f"\nTOTAL: {total_before/1e9:.2f}GB -> {total_after/1e9:.2f}GB "
          f"(saved {(total_before-total_after)/1e9:.2f}GB); {len(skipped)} skipped")
    if skipped:
        print("Rerun after a Console restart to compact the skipped active files.")


if __name__ == "__main__":
    main()
