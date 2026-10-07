---
name: recall
description: Recall what happened in past Claude Code sessions using the logbook index. Route pointer questions ("which session", "how do I resume", "which session touched file X", "the session that made PR 119") straight to the logbook MCP tools, and dispatch the recall subagent for anything that needs reading and synthesising across turns or sessions ("what did we decide", "how did we fix", "where did I book", "what did the fetched page say"). Triggers: "last time", "before", "previously", "we already", "do you remember", "find the session", "what did I decide", "how did we", "the session where".
user_invocable: true
---

# Recall

Two tools exist: `search` (find sessions) and `read` (read a session, a turn, or a tool result by address). Two
paths through them. Pick by the shape of the question, not by how hard it sounds.

## First

The `search` and `read` schemas may be deferred. If they are not loaded, call
`ToolSearch("select:mcp__logbook__search,mcp__logbook__read")` in the same turn as your first call, not as a
separate round-trip.

## Direct: pointer questions

One or two tool calls answer these. Do not spawn an agent.

- Which session did X, when, in which repo: `search("<words>", project=, since=, file=, pr=)`
- The session that made PR N: `search(pr="N")`
- Which session created or changed a file: `search(file="name.ext")`
- What happened recently in a directory: `search(project="<directory name>")`
- How do I resume it: the resume command is in the header of `read("<session8>")`

Answer from the search block (title, outcome, recap, snippets). Read a single turn only if the block is
not enough; `read("<session8>/t65-71")` reads consecutive turns in one call.

## Subagent: synthesis questions

Anything that needs reading several turns or several sessions and combining them. Spawn:

```
Agent(subagent_type="recall", description="Recall: <topic>",
      prompt="Question: <the user's question, verbatim>.
              Context: <cwd, dates, names or files the user mentioned>.
              Already known: <any search blocks you have already seen, so it does not repeat them>.
              Return the report format from your instructions.")
```

The report is a few hundred tokens: Findings newest first, each with verbatim quotes tagged
`[session8 uuid8 tN date model role]` or `[session8 uuid8 tN#seq tool:Name date model]`, a "Supersedes" note when a later
session changed an earlier answer, plus Consulted, Not found and Resume lines. The agent re-greps every quote before
returning and marks any it could not reproduce `(unverified)`. Raw hits never enter this context.

## Using the report

- Quotes marked `(unverified)` are claims; everything else was checked byte for byte. To check one yourself, turn
  its tag `[27c3625f 70d158d1 t16 2026-08-21 fable-5 claude]` into the address `27c3625f/70d158d1` and grep it:
  `read("27c3625f/70d158d1", grep="<distinctive words>")`. A few hundred tokens. The tag itself is not an address.
- Verified means the words were said, not that they were true. A reply from an older or non-Claude model (the tag
  names it) or a user statement may be wrong or stale; check against the current codebase or the world before
  acting on it, and tell the user which session and model it came from.
- To get more around a quote: `read("27c3625f/70d158d1")` for the full exchange, `read("27c3625f/70d158d1",
  grep=...)` before `include_tools=True` (tool output is large), or `read("27c3625f/70d158d1#3")` for one tool
  result.
- Do not repeat searches the agent lists under Consulted. If Not found names what you need, say so to the user
  rather than searching again with the same words.
- Relay the address and the resume command when you give the user a fact, so they can jump to it.

## When not to use either path

- The answer is in the current conversation or the current codebase (grep the repo instead).
- The user is describing new work with no reference to the past.
