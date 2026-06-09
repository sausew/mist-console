"""
importer.py — surface existing Claude Code conversations (the harness project's
~/.claude/projects/.../<session>.jsonl files) as dormant MIST Console tabs.

scan_recent() lists importable sessions (metadata only — cheap, for boot).
convert() turns one session's jsonl into Console display events (lazy, on open).
Imported tabs are resumable: claude_session_id = the session UUID.
"""
import glob
import json
import os

PROJECT_DIR = os.path.expanduser(
    "~/.claude/projects/-Users-alexhedtke-Documents-Exobrain-harness")


def _text_of(content):
    """Pull plain text out of a message.content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_real_user_turn(line):
    """A genuine user-typed message (not a tool_result, meta, or command echo)."""
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
    """Return (n_user_turns_capped, first_user_text). Stops reading once it has
    the first message and `need` user turns, so large files aren't fully read."""
    n, first = 0, ""
    try:
        with open(path) as f:
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
    for path in glob.glob(os.path.join(PROJECT_DIR, "*.jsonl")):
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
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
    except Exception:
        return []

    # tool_use_id -> result text (results arrive as user tool_result blocks)
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
