# AI prompts and conversations

Raw transcripts of every Claude Code (Claude Opus 5.5) session used in this
project, exported from the Claude Code session logs with
[export_logs.py](export_logs.py).

```
claude-code/session-1-2026-11-04/conversation.md   building, deploying and evaluating the system
claude-code/session-1-2026-11-04/subagents/        the code-review workflows run in that session:
                                                   one transcript per review agent + the workflow scripts
claude-code/session-2-2026-11-05/conversation.md   reviewing the healing logic, clean-up, report
```

Each transcript contains, verbatim and in order: every prompt, every reply,
every tool call with its full input and every tool result.

Not included: the instructions Claude Code adds to every request on its own
(system prompt, `<system-reminder>` blocks, API metadata); reasoning blocks,
which the logs store empty; attached files (images, the report template PDF),
which appear as a one-line note. The Raspberry Pi SSH password and the hotspot
password are replaced by `[REDACTED]`.
