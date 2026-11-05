#!/usr/bin/env python3
"""Export Claude Code session logs (.jsonl) to readable Markdown, verbatim.

    python3 ai/export_logs.py <claude-projects-dir> <out-dir>

Kept, word for word: every user prompt, every assistant message, every tool call
(with its full input) and every tool result, in order, with timestamps.
Not kept: text the Claude Code harness injects that is not part of the
conversation (<system-reminder> blocks, the system prompt, API metadata) and
thinking blocks (stored empty in the logs). Attached files (images, PDFs) are
replaced by a one-line note. Passwords are replaced by [REDACTED].
"""

import json
import os
import re
import sys
from pathlib import Path

# Strings to redact, comma-separated, from the environment (never written in this file):
#   KSH_REDACT="secret1,secret2" python3 ai/export_logs.py ...
SECRETS = [rf"(?<![0-9A-Za-z]){re.escape(s)}(?![0-9A-Za-z])"
           for s in os.environ.get("KSH_REDACT", "").split(",") if s]
# Only blocks the harness injects: the tags stand on their own lines. A mention of
# the tag inside the conversation (e.g. in code) is left alone.
REMINDER = re.compile(r"^<system-reminder>\n.*?\n</system-reminder>[ \t]*\n?", re.S | re.M)


def clean(text):
    text = REMINDER.sub("", text)
    for s in SECRETS:
        text = re.sub(s, "[REDACTED]", text)
    return text


def fence(text, lang=""):
    longest = max((len(m) for m in re.findall(r"`{3,}|~{3,}", text)), default=2)
    f = "~" * max(3, longest + 1)
    return f"{f}{lang}\n{text}\n{f}"


def blocks(content):
    if isinstance(content, str):
        yield {"type": "text", "text": content}
    else:
        yield from content


def result_text(b):
    c = b.get("content")
    if isinstance(c, str):
        return c
    parts = []
    for x in c or []:
        if x.get("type") == "text":
            parts.append(x["text"])
        else:
            parts.append(f"[{x.get('type')} returned by the tool: not included]")
    return "\n".join(parts)


def export(src, dst, title):
    out = [f"# {title}", "", f"Source log: `{src.name}`", ""]
    n = 0
    for line in src.open(encoding="utf-8"):
        o = json.loads(line)
        role = o.get("type")
        if role not in ("user", "assistant"):
            continue
        ts = (o.get("timestamp") or "")[:19].replace("T", " ")
        for b in blocks(o["message"]["content"]):
            t = b.get("type")
            if t == "thinking" or t == "redacted_thinking":
                continue
            if t == "text":
                text = clean(b["text"]).strip()
                if not text:
                    continue
                if role == "user":
                    who = "User (injected by Claude Code)" if o.get("isMeta") else "User"
                    out += [f"## {who} — {ts}", "", text, ""]
                else:
                    out += [f"### Claude — {ts}", "", text, ""]
            elif t == "tool_use":
                inp = clean(json.dumps(b.get("input"), indent=2, ensure_ascii=False))
                out += [f"**Tool call: {b.get('name')}** — {ts}", "", fence(inp, "json"), ""]
            elif t == "tool_result":
                res = clean(result_text(b)).rstrip()
                tag = " (error)" if b.get("is_error") else ""
                out += [f"<details><summary>Tool result{tag}</summary>", "", fence(res), "", "</details>", ""]
            elif t in ("image", "document"):
                name = (b.get("title") or b.get("source", {}).get("media_type") or t)
                out += [f"*[{t} attached by the user: {name} — file not included]*", ""]
            n += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out), encoding="utf-8")
    return n


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    sessions = sorted(src.glob("*.jsonl"),
                      key=lambda p: json.loads(next(l for l in p.open(encoding="utf-8")
                                                    if '"timestamp"' in l)).get("timestamp", ""))
    for i, s in enumerate(sessions, 1):
        first = next(json.loads(l)["timestamp"] for l in s.open(encoding="utf-8") if '"timestamp"' in l)
        day = first[:10]
        base = dst / f"session-{i}-{day}"
        n = export(s, base / "conversation.md", f"Claude Code session {i} ({day}) — main conversation")
        print(f"{s.name}: {n} entries -> {base / 'conversation.md'}")
        wf_root = src / s.stem / "subagents" / "workflows"
        for wf in sorted(wf_root.glob("wf_*")) if wf_root.exists() else []:
            for a in sorted(wf.glob("agent-*.jsonl")):
                export(a, base / "subagents" / wf.name / f"{a.stem}.md",
                       f"Session {i}, workflow {wf.name}: sub-agent {a.stem}")
            print(f"  {wf.name}: {len(list(wf.glob('agent-*.jsonl')))} sub-agents")
        for script in sorted((src / s.stem / "workflows" / "scripts").glob("*.js")) \
                if (src / s.stem / "workflows" / "scripts").exists() else []:
            (base / "subagents").mkdir(parents=True, exist_ok=True)
            (base / "subagents" / script.name).write_text(clean(script.read_text(encoding="utf-8")),
                                                          encoding="utf-8")


if __name__ == "__main__":
    main()
