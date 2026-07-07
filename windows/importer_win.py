"""
importer_win.py: surface existing Claude Code conversations as dormant MIST
Console tabs. Windows port of importer.py; the project dir is derived from the
configured workspace instead of a hardcoded path.
"""
import glob
import json
import os
import re

import config_win


def project_dir():
    """Claude Code stores transcripts under ~/.claude/projects/<munged cwd>,
    where the munge replaces every non-alphanumeric character with '-'."""
    ws = config_win.load_config().get("workspace") or config_win.DEFAULT_WORKSPACE
    munged = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(ws))
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", munged)


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_real_user_turn(line):
    if line.get("type") != "user":
        return False
    m = line.get("message")
    if not isinstance(m, dict):
        return False
    c = m.get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return False
    t = _text_of(c).strip()
    if not t or t.startswith("<") or t.startswith("Caveat:"):
        return False
    return True


def _summarize(path, need=2):
    n, first = 0, ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if _is_real_user_turn(o):
                    n += 1
                    if not first:
                        first = _text_of(o["message"]["content"]).strip()
                    if n >= need and first:
                        break
    except Exception:
        pass
    return n, first


def scan_recent(days=7, min_user_turns=2, limit=120):
    """List importable sessions (newest first). Metadata only."""
    import time
    cutoff = time.time() - days * 86400
    out = []
    for path in glob.glob(os.path.join(project_dir(), "*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        n, first = _summarize(path)
        if n < min_user_turns or not first:
            continue
        uuid = os.path.splitext(os.path.basename(path))[0]
        title = (first[:48] + "…") if len(first) > 48 else first
        out.append({"uuid": uuid, "path": path, "title": title,
                    "mtime": mtime, "turns": n})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


def convert(path):
    """Convert a Claude Code session jsonl into Console display events."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [json.loads(l) for l in f if l.strip()]
    except Exception:
        return []

    results = {}
    for o in lines:
        if o.get("type") == "user" and isinstance(o.get("message"), dict):
            c = o["message"].get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        txt = b.get("content")
                        if isinstance(txt, list):
                            txt = "".join(p.get("text", "") for p in txt if isinstance(p, dict))
                        results[b.get("tool_use_id")] = str(txt or "")

    events = []
    for o in lines:
        t = o.get("type")
        m = o.get("message")
        if t == "user" and _is_real_user_turn(o):
            events.append({"type": "user_text", "text": _text_of(m["content"]).strip()})
        elif t == "assistant" and isinstance(m, dict):
            blocks = []
            for b in (m.get("content") or []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    blocks.append({"kind": "text", "text": b["text"]})
                elif b.get("type") == "thinking" and b.get("thinking", "").strip():
                    blocks.append({"kind": "thinking", "text": b["thinking"]})
                elif b.get("type") == "tool_use":
                    blocks.append({"kind": "tool", "name": b.get("name", "tool"),
                                   "input": b.get("input", {}),
                                   "result": results.get(b.get("id"), "")})
            if blocks:
                events.append({"type": "mist_msg", "blocks": blocks})
    return events
