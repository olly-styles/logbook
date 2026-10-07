# logbook

Claude Code starts every session blank, deletes transcripts after a retention window, and keeps what it does
remember in JSONL files that nothing searches. logbook archives every transcript on the machine before Claude
Code removes it, indexes them into a local SQLite full-text index, and gives a running session two MCP tools and a
hook so it can check what was done before without loading whole transcripts.

![On the first prompt the hook lists the most recent sessions; search() finds a session by any word in it](docs/screenshot.png)

## What a session gets

- **On its first prompt, eight lines of context.** The five most recent sessions across all projects, one line
  each: id, date, directory, branch, PRs, turn count, model and topic.
- **Two MCP tools.** `search` finds sessions by full text over prompts, replies, recaps and tool output, or lists
  them by directory, file or PR. `read` drills into one session (an index of its turns; `turns=` for
  their text), one turn or one tool result, with `grep=` to pull exact lines. Every turn has a stable 8-character
  anchor, so a quote can be cited and re-verified after reindexing. Parameters and address forms are in the tool
  descriptions.
- **A `/recall` skill and subagent** for questions that need reading across sessions. The subagent searches, reads
  and returns a few hundred tokens of verified quotes with their addresses, so raw transcripts never enter the main
  context.

## Install

Needs Python 3.12 or newer with SQLite FTS5, the `claude` CLI on PATH, and [uv](https://docs.astral.sh/uv/).

```
uv tool install git+https://github.com/olly-styles/logbook
logbook install
```

`install` indexes the transcripts already on disk, then wires four hooks into `~/.claude/settings.json`, copies the
recall skill and agent under `~/.claude/`, and registers the MCP server via `claude mcp add`. Running it again is
safe. Restart Claude Code afterwards. Upgrade with `uv tool upgrade logbook`, then `logbook install` again.

To wire by hand instead: `claude mcp add --scope user logbook -- /path/to/logbook serve`, copy
`src/logbook/claude/{skills,agents}` into `~/.claude/`, and add the hooks:

```json
{"hooks": {
  "SessionStart":     [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/logbook hook", "timeout": 30}]}],
  "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/logbook prompt-hook", "timeout": 5}]}],
  "Stop":             [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/logbook index-hook", "timeout": 10}]}],
  "SessionEnd":       [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/logbook index-hook", "timeout": 30}]}]
}}
```

## How it works

**Archive first.** Before a transcript is parsed it is gzipped to `~/.logbook/archive/`. The index is a derived
artefact: a parser fix or a schema change rebuilds it from the archive, and sessions whose transcript Claude Code
has deleted stay searchable.

**The session that writes a transcript indexes it.** The UserPromptSubmit and Stop hooks re-parse the current
session's own transcript, so a finished turn is searchable from any other session the moment it ends. The MCP
server never syncs. SessionStart runs a full scan as the catch-up for anything the pushes missed.

**Context is the budget.** The hook injects one line per session, once per session id. Tool output is searched by
default but only shown on request, and a session that matches only in tool output ranks below every session that
matches in a prompt, reply or recap and takes one line. A search hit is the session line, its latest recap and up to
two snippets; PR urls and file paths appear only when the call filters by `pr=` or `file=`. The intended path is
`search` to `read(session)` (an index of turns) to `read(session, grep=)` to `read(turn)` to `read(turn#seq)`, each
step costing more.

**Ranking is BM25 plus a boost for the current directory.** There is no recency decay. Every hit carries its date
and the tool description tells the agent to prefer the most recent of equally relevant hits. Words are ANDed within
one turn, recap or tool result and quoted phrases must appear as written (stems still match); when nothing matches, the search falls back to sessions matching two or more of the
words, one line each.

**Secrets are redacted before storage.** Known key formats, JWTs, private key blocks, authorization headers, URL
credentials and `key=value` pairs whose key names a password or token are replaced in every indexed field. `Read`
results for `.env`, key and credential files become a placeholder. Redaction is pattern-based and will miss secrets
in unfamiliar shapes. The gzipped archive is the raw transcript, kept in a directory created with mode 700.

**Nothing leaves the machine.** The index, archive and a JSONL log of every query live under `~/.logbook/`.
The running session is excluded from results and calls to logbook's own tools are not indexed.

## Development

```
uv sync
uv run pytest
uv run pre-commit run --all-files
```

The transcript format is internal to Claude Code and changes between releases. `parse.py` is the only file that
knows the record shapes. Issues and pull requests are welcome; this is a personal tool and review may be slow.
