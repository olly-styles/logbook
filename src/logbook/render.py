import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import query
from .address import ANCHOR_LEN
from .parse import result_cap
from .query import Filters, Hit

WS = re.compile(r"\s+")
MODEL_DATE_SUFFIX = re.compile(r"-\d{8}$")
TIME_TS_MIN_LEN = 16
MAX_MATCHED_FILES = 6
MAX_PRS_INLINE = 3
MAX_PRS_LISTED = 8
TURN_USER_CLIP = 300
TURN_CLAUDE_CLIP = 900
MAX_TOOL_KINDS = 4
EVENT_INPUT_CLIP = 160
EVENT_RESULT_CLIP = 500
TOOL_REF_CLIP = 60
SEARCH_RECAP_CLIP = 300
LISTING_RECAP_CLIP = 200
RECAP_CLIP = 300
RECAP_EDGE = 2
GREP_LINE_CLIP = 200
GREP_MAX_LINES = 60
TURN_RANGE_SEP = "\n\n----\n\n"
INDEX_TEXT_CLIP = 90
RECAP_GAP = '… {n} recaps not shown; read(id, grep="...") searches all'
TURN_GAP = "… {n} turns not shown …"
MINUTES_PER_HOUR = 60


def clip_text(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)].rstrip() + "…"


def squash(text: str) -> str:
    return WS.sub(" ", text).strip()


def clip(text: str, n: int) -> str:
    return clip_text(squash(text), n)


def clip_lines(text: str, n: int) -> str:
    return "\n".join(clip_text(line, n) for line in text.split("\n"))


def lines_len(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def short_model(model: str) -> str:
    return MODEL_DATE_SUFFIX.sub("", model.removeprefix("claude-"))


def event_label(e: sqlite3.Row) -> str:
    return f"{e['name']} {short_model(e['model'])}" if e["model"] else e["name"]


def short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


def day(ts: str) -> str:
    return ts[:10]


def hhmm(ts: str) -> str:
    return ts[11:16] if len(ts) >= TIME_TS_MIN_LEN else ""


def utcnow() -> datetime:
    return datetime.now(UTC)


def minutes_since(ts: str) -> float:
    if len(ts) < TIME_TS_MIN_LEN:
        return float("inf")
    return (utcnow() - query.parse_ts(ts)).total_seconds() / 60


def session_label(s: sqlite3.Row) -> str:
    return s["title"] or clip(s["first_prompt"], 70) or "(untitled)"


def repo_base(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def pr_tag(prs: list[sqlite3.Row]) -> str:
    if not prs:
        return ""
    shown = ", ".join(f"{repo_base(p['repo'])}#{p['number']}" for p in prs[:MAX_PRS_INLINE])
    extra = len(prs) - MAX_PRS_INLINE
    return f"PR {shown}" + (f" +{extra}" if extra > 0 else "")


def pr_lines(prs: list[sqlite3.Row]) -> list[str]:
    lines = [f"    pr: {p['repo'] or 'unknown repo'}#{p['number']} {p['url']}".rstrip() for p in prs[:MAX_PRS_LISTED]]
    extra = len(prs) - MAX_PRS_LISTED
    if extra > 0:
        lines.append(f"    +{extra} more PRs")
    return lines


def is_today(ts: str) -> bool:
    if len(ts) < TIME_TS_MIN_LEN:
        return False
    return query.parse_ts(ts).astimezone().date() == utcnow().astimezone().date()


def ago(ts: str) -> str:
    minutes = max(0, int(minutes_since(ts)))
    return f"{minutes}m ago" if minutes < MINUTES_PER_HOUR else f"{minutes // MINUTES_PER_HOUR}h ago"


def today_tag(s: sqlite3.Row) -> str:
    return f"[last active {ago(s['ended_at'])}]" if is_today(s["ended_at"]) else ""


def session_line(s: sqlite3.Row, prs: list[sqlite3.Row]) -> str:
    bits = [s["id"][:8], day(s["started_at"]), short_path(s["cwd"])]
    if s["git_branch"]:
        bits.append(f"({s['git_branch']})")
    pr = pr_tag(prs)
    if pr:
        bits.append(pr)
    bits.append(f"{s['turn_count']}t")
    if s["model"]:
        bits.append(short_model(s["model"]))
    tag = today_tag(s)
    if tag:
        bits.append(tag)
    if s["headless"]:
        bits.append("[headless]")
    if not s["transcript_present"]:
        bits.append("[transcript-deleted]")
    return "  ".join(bits) + f"  {session_label(s)}"


def files_summary(rows: list[sqlite3.Row], max_files: int) -> str:
    written = [r["path"] for r in rows if r["action"] == "write"]
    if not written:
        return ""
    names = [short_path(p) for p in written[:max_files]]
    extra = len(written) - len(names)
    return "wrote: " + ", ".join(names) + (f" +{extra}" if extra > 0 else "")


def outcome_line(s: sqlite3.Row, width: int) -> str:
    if s["summary"]:
        return f"    recap: {clip(s['summary'], width)}"
    if s["last_reply"]:
        return f"    outcome (last reply): {clip(s['last_reply'], width)}"
    return ""


def truncation_note(e: sqlite3.Row) -> str:
    if not e["truncated"]:
        return ""
    return f"[output truncated at {result_cap(e['name'])} chars when indexed; original was longer]"


def matched_files(paths: list[str]) -> str:
    matched = sorted(set(paths))
    shown = ", ".join(short_path(m) for m in matched[:MAX_MATCHED_FILES])
    extra = len(matched) - MAX_MATCHED_FILES
    return shown + (f" +{extra}" if extra > 0 else "")


def render_hit(h: Hit, conn: sqlite3.Connection, f: Filters, *, show_tools: bool) -> list[str]:
    s = h.session
    prs = query.prs_for(conn, s["id"])
    lines = [session_line(s, prs)]
    if h.tool_snippets and not show_tools:
        lines[0] += "  tool: " + ", ".join(f"t{e['idx']}#{e['seq']} {event_label(e)}" for e, _ in h.tool_snippets)
    outcome = outcome_line(s, SEARCH_RECAP_CLIP)
    if outcome:
        lines.append(outcome)
    latest = day(query.recaps_for(conn, s["id"])[-1]["ts"]) if h.recap_snippets else ""
    extra = [(ts, snip) for ts, snip in h.recap_snippets if day(ts) != latest]
    lines.extend(f"    recap {day(ts)}: {clip(snip, 200)}" for ts, snip in extra[:1])
    if f.pr:
        lines.extend(pr_lines(prs))
    if f.file:
        rows = query.files_for(conn, s["id"])
        lines.append("    files: " + matched_files([r["path"] for r in rows if f.file.lower() in r["path"].lower()]))
    lines.extend(f"    t{idx}: {clip(snip, 200)}" for idx, snip in h.snippets)
    if h.tool_snippets and show_tools:
        for e, snip in h.tool_snippets:
            flag = " (truncated)" if e["truncated"] else ""
            lines.append(f"    tool t{e['idx']}#{e['seq']} {event_label(e)}{flag}: {clip(snip, 200)}")
    return lines


def tool_only_line(h: Hit, conn: sqlite3.Connection) -> str:
    refs = ", ".join(
        f"t{e['idx']}#{e['seq']} {event_label(e)} {clip(e['input_summary'], TOOL_REF_CLIP)}" for e, _ in h.tool_snippets
    )
    return f"{session_line(h.session, query.prs_for(conn, h.session['id']))}  tool-only: {refs}"


NEXT_STEP = 'read("<session8>") or read("<session8>/t<idx>", grep=...)'


RETRY_HINT = "Try fewer or different words, or a prefix with * (e.g. pyrig*)."


def render_hits(hits: list[Hit], query_text: str, conn: sqlite3.Connection, f: Filters, *, show_tools: bool) -> str:
    if not hits:
        return f"No sessions match {query_text!r}. {RETRY_HINT}"
    tail = [] if show_tools else [h for h in hits if h.tool_only()]
    lines = [f"{len(hits)} sessions match {query_text!r} (best first); {NEXT_STEP}"]
    for h in hits[: len(hits) - len(tail)]:
        lines.extend(render_hit(h, conn, f, show_tools=show_tools))
    if tail:
        lines.append(
            f"{len(tail)} of them mention the words only in tool output, one line each; "
            'read("<session8>/t<idx>#<seq>") shows that output, include_tools=True shows snippets.'
        )
        lines.extend(tool_only_line(h, conn) for h in tail)
    return "\n".join(lines)


def render_fallback(hits: list[Hit], query_text: str, terms: list[str], conn: sqlite3.Connection) -> str:
    if not hits:
        return f"No sessions match {query_text!r} or two or more of its words. {RETRY_HINT}"
    lines = [
        f"No session matches all of {query_text!r} in one place. {len(hits)} match some of the words, one line each, "
        f"most words matched first; each line ends with the words it matched; {NEXT_STEP}"
    ]
    for h in hits:
        matched = ", ".join(sorted(h.matched_terms, key=terms.index))
        lines.append(f"{session_line(h.session, query.prs_for(conn, h.session['id']))}  matched: {matched}")
    return "\n".join(lines)


def render_recent(rows: list[sqlite3.Row], conn: sqlite3.Connection) -> str:
    if not rows:
        return "No indexed sessions match; widen since= or drop a filter."
    lines = [f"{len(rows)} most recent sessions matching the filters:"]
    for s in rows:
        lines.append(session_line(s, query.prs_for(conn, s["id"])))
        outcome = outcome_line(s, LISTING_RECAP_CLIP)
        if outcome:
            lines.append(outcome)
    return "\n".join(lines)


def collapsed(lines: list[str], edge: int, gap: str) -> list[str]:
    if len(lines) <= 2 * edge:
        return lines
    return [*lines[:edge], gap.format(n=len(lines) - 2 * edge), *lines[-edge:]]


def recap_trail(recaps: list[sqlite3.Row]) -> list[str]:
    lines = [f"recap {day(r['ts'])} {hhmm(r['ts'])}: {clip(r['content'], RECAP_CLIP)}" for r in recaps]
    return collapsed(lines, RECAP_EDGE, RECAP_GAP)


def session_header(
    s: sqlite3.Row, files: list[sqlite3.Row], prs: list[sqlite3.Row], recaps: list[sqlite3.Row]
) -> list[str]:
    tag = today_tag(s)
    written = files_summary(files, 8)
    return [
        f"session {s['id']}",
        f"{day(s['started_at'])} {hhmm(s['started_at'])} to {day(s['ended_at'])} {hhmm(s['ended_at'])}"
        + (f"  {tag}" if tag else "")
        + f"  cwd {short_path(s['cwd'])}"
        + (f"  branch {s['git_branch']}" if s["git_branch"] else ""),
        f"title: {session_label(s)}",
        f"{s['turn_count']} turns, model {s['model'] or '?'}"
        + (", headless" if s["headless"] else "")
        + ("" if s["transcript_present"] else ", transcript deleted (index only)"),
        *(line.strip() for line in pr_lines(prs)),
        *recap_trail(recaps),
        *([written] if written else []),
        resume_line(s),
    ]


def tool_counts(tools: dict[str, int], limit: int) -> str:
    ranked = sorted(tools.items(), key=lambda kv: -kv[1])[:limit]
    return ", ".join(f"{name}x{n}" for name, n in ranked)


def seq_range(n: int) -> str:
    return "#0" if n == 1 else f"#0-#{n - 1}"


def tools_tag(t: sqlite3.Row) -> str:
    tools = json.loads(t["tools_json"])
    return f" [{seq_range(t['tool_calls'])} {tool_counts(tools, MAX_TOOL_KINDS)}]" if tools else ""


def turn_block(t: sqlite3.Row) -> str:
    head = f"t{t['idx']} {t['uuid'][:ANCHOR_LEN]} {hhmm(t['ts'])}{tools_tag(t)}"
    assistant = clip(t["assistant_text"], TURN_CLAUDE_CLIP)
    return f"{head}\n  user: {clip(t['user_text'], TURN_USER_CLIP)}" + (f"\n  claude: {assistant}" if assistant else "")


def omitted_note(n: int) -> str:
    return f"{n} earlier turns omitted to fit max_chars; use turns= to select them."


def render_session(header: list[str], turns: list[sqlite3.Row], max_chars: int) -> str:
    blocks = [turn_block(t) for t in turns]
    room = max_chars - lines_len(header) - len(omitted_note(len(blocks))) - 1
    kept = blocks
    while len(kept) > 1 and sum(len(b) + 2 for b in kept) > room:
        kept = kept[1:]
    if len(kept) < len(blocks):
        header = [*header, omitted_note(len(blocks) - len(kept))]
    return "\n".join(header) + "\n\n" + "\n\n".join(kept)


def render_session_grep(
    s: sqlite3.Row, rows: list[tuple[int, str, str, str, str]], pattern: str, max_chars: int
) -> str:
    if not rows:
        return f"session {s['id'][:8]}: no lines match /{pattern}/"
    rows = [r for r in rows if r[0] >= 0] or rows
    head = f"session {s['id'][:8]}: {len(rows)} line(s) match /{pattern}/ (t<idx> <uuid8> <date> <role>: line)"
    out = [head]
    used = len(head)
    for n, (idx, uuid, ts, role, line) in enumerate(rows):
        where = f"recap {day(ts)} {hhmm(ts)}" if idx < 0 else f"t{idx} {uuid[:ANCHOR_LEN]} {day(ts)} {role}"
        width = EVENT_INPUT_CLIP if role.endswith(" input") else GREP_LINE_CLIP
        entry = f"{where}: {clip(line, width)}"
        marker = f"… {len(rows) - n} more; narrow the pattern or pass turns="
        reserve = 0 if n == len(rows) - 1 else len(marker) + 1
        if n == GREP_MAX_LINES or used + len(entry) + reserve + 1 > max_chars:
            out.append(marker)
            break
        out.append(entry)
        used += len(entry) + 1
    return "\n".join(out)


def index_line(t: sqlite3.Row) -> str:
    return f"t{t['idx']} {t['uuid'][:ANCHOR_LEN]} {hhmm(t['ts'])}{tools_tag(t)} {clip(t['user_text'], INDEX_TEXT_CLIP)}"


def fitted_index(turns: list[sqlite3.Row], room: int) -> list[str]:
    lines = [index_line(t) for t in turns]
    for edge in range((len(turns) + 1) // 2, 1, -1):
        out = collapsed(lines, edge, TURN_GAP)
        if lines_len(out) <= room:
            return out
    return collapsed(lines, 1, TURN_GAP)


def view_session(conn: sqlite3.Connection, s: sqlite3.Row, turns: str, grep: str, max_chars: int) -> str:
    rows = query.select_turns(query.turns_for(conn, s["id"]), turns)
    if grep:
        wanted = {r["idx"] for r in rows}
        matches = [m for m in query.grep_session(conn, s["id"], grep) if m[0] < 0 or m[0] in wanted]
        return render_session_grep(s, matches, grep, max_chars)
    header = session_header(
        s, query.files_for(conn, s["id"]), query.prs_for(conn, s["id"]), query.recaps_for(conn, s["id"])
    )
    if turns.strip():
        return render_session(header, rows, max_chars)
    return "\n".join([*header, "", *fitted_index(rows, max_chars - lines_len(header) - 1)])


def cite(s: sqlite3.Row, t: sqlite3.Row, suffix: str, model: str) -> str:
    bits = [s["id"][:8], t["uuid"][:ANCHOR_LEN], f"t{t['idx']}{suffix}", day(t["ts"])]
    if model:
        bits.append(short_model(model))
    return f"cite: [{' '.join(bits)}]"


def turn_header(s: sqlite3.Row, t: sqlite3.Row, events: list[sqlite3.Row]) -> str:
    tools = json.loads(t["tools_json"])
    files = json.loads(t["files_json"])
    head = (
        f"session {s['id'][:8]} turn {t['idx']} uuid {t['uuid'][:ANCHOR_LEN]} at {day(t['ts'])} {hhmm(t['ts'])}"
        f"  cwd {short_path(s['cwd'])}\n{cite(s, t, '', s['model'])} (append role)"
    )
    meta = []
    if tools:
        hidden = "" if events else " [include_tools=True to show]"
        meta.append(f"tools {seq_range(t['tool_calls'])}: " + tool_counts(tools, len(tools)) + hidden)
    if files:
        meta.append("files: " + ", ".join(f"{short_path(p)}({'/'.join(a)})" for p, a in files.items()))
    return head + ("\n" + "\n".join(meta) if meta else "")


def render_turn(s: sqlite3.Row, t: sqlite3.Row, events: list[sqlite3.Row], max_chars: int) -> str:
    fixed = turn_header(s, t, events) + "\n\nuser:\n"
    tools_head = "\n\ntool calls:\n" if events else ""
    tools_reserve = max_chars // 2 if events else 0
    remaining = max_chars - len(fixed) - len("\n\nclaude:\n") - len(tools_head) - tools_reserve
    user = t["user_text"]
    assistant = t["assistant_text"]
    if len(user) + len(assistant) > remaining:
        user = clip_text(user, max(remaining // 3, remaining - len(assistant)))
        assistant = clip_text(assistant, max(0, remaining - len(user)))
    out = fixed + user + "\n\nclaude:\n" + (assistant or "(no final message recorded)")
    if events:
        out += tools_head + render_events(events, max_chars - len(out) - len(tools_head))
    return out


def render_grep(label: str, text: str, pattern: str, max_chars: int) -> str:
    rows = query.grep_lines(text, pattern)
    if not rows:
        return ""
    out = [f"{label}: {sum(1 for _, _, m in rows if m)} matching line(s) for /{pattern}/, with context"]
    used = len(out[0])
    last = 0
    for number, line, matched in rows:
        if number != last + 1 and last:
            out.append("    …")
        marker = ">" if matched else " "
        entry = f"{marker}{number:>5}: {line}"
        if used + len(entry) > max_chars:
            out.append("    … (truncated at max_chars)")
            break
        out.append(entry)
        used += len(entry) + 1
        last = number
    return "\n".join(out)


def turn_grep_sections(
    s: sqlite3.Row, t: sqlite3.Row, events: list[sqlite3.Row], pattern: str, max_chars: int
) -> tuple[str, list[str]]:
    head = turn_header(s, t, events)
    share = max(400, (max_chars - len(head)) // (2 + 2 * len(events)))
    sections = [
        render_grep("user", t["user_text"], pattern, share),
        render_grep("claude", t["assistant_text"], pattern, share),
    ]
    for e in events:
        label = f"tool #{e['seq']} {event_label(e)}"
        section = render_grep(f"{label} {clip(e['input_summary'], 80)}", e["result"], pattern, share)
        if not section:
            section = render_grep(f"{label} input", clip_lines(e["input_summary"], EVENT_INPUT_CLIP), pattern, share)
        sections.append(section)
    return head, [part for part in sections if part]


def render_turn_grep(s: sqlite3.Row, t: sqlite3.Row, events: list[sqlite3.Row], pattern: str, max_chars: int) -> str:
    head, matched = turn_grep_sections(s, t, events, pattern, max_chars)
    if not matched:
        return head + f"\n\nno lines match /{pattern}/ in this turn" + ("" if events else " (tool output not searched)")
    return "\n\n".join([head, *matched])


def view_turn(s: sqlite3.Row, t: sqlite3.Row, events: list[sqlite3.Row], grep: str, max_chars: int) -> str:
    if grep:
        return render_turn_grep(s, t, events, grep, max_chars)
    return render_turn(s, t, events, max_chars)


def range_ref(first: int, last: int) -> str:
    return f"t{first}" if first == last else f"t{first}-{last}"


def turns_left_note(s: sqlite3.Row, ref: str, grep: str) -> str:
    args = f", grep={json.dumps(grep)}" if grep else ""
    return f'{ref} not shown to fit max_chars; read("{s["id"][:8]}/{ref}"{args})'


def view_turn_range(
    s: sqlite3.Row,
    turns: list[sqlite3.Row],
    events_of: Callable[[sqlite3.Row], list[sqlite3.Row]],
    grep: str,
    max_chars: int,
) -> tuple[str, int]:
    last = turns[-1]["idx"]
    longest_note = turns_left_note(s, f"t{last}-{last}", grep)
    blocks: list[str] = []
    used = 0
    for i, t in enumerate(turns):
        events = events_of(t)
        head, matched = turn_grep_sections(s, t, events, grep, max_chars) if grep else ("", [])
        if grep and not matched:
            continue
        reserve = len(TURN_RANGE_SEP) + len(longest_note) if i < len(turns) - 1 else 0
        budget = max_chars - used - (len(TURN_RANGE_SEP) if blocks else 0) - reserve
        if not blocks:
            block = view_turn(s, t, events, grep, max(1, budget))
        elif grep:
            block = "\n\n".join([head, *matched])
        else:
            block = view_turn(s, t, events, grep, max_chars)
        if blocks and len(block) > budget:
            return TURN_RANGE_SEP.join([*blocks, turns_left_note(s, range_ref(t["idx"], last), grep)]), len(blocks)
        blocks.append(block)
        used += len(block) + (len(TURN_RANGE_SEP) if len(blocks) > 1 else 0)
    if not blocks:
        return f"session {s['id'][:8]}: no lines match /{grep}/ in {range_ref(turns[0]['idx'], last)}", 0
    return TURN_RANGE_SEP.join(blocks), len(blocks)


def event_block(e: sqlite3.Row) -> str:
    head = f"#{e['seq']} {event_label(e)} {clip(e['input_summary'], EVENT_INPUT_CLIP)}"
    result = clip(e["result"], EVENT_RESULT_CLIP)
    note = truncation_note(e)
    return head + (f"\n    -> {result}" if result else "") + (f"\n    {note}" if note else "")


def omission_line(omitted: list[sqlite3.Row]) -> str:
    first = omitted[0]
    calls = "tool call" if len(omitted) == 1 else "tool calls"
    return (
        f"… {len(omitted)} more {calls} not shown; "
        f'read("{first["session_id"][:8]}/t{first["idx"]}#{first["seq"]}") shows one in full'
    )


def render_events(events: list[sqlite3.Row], budget: int) -> str:
    out: list[str] = []
    for n, e in enumerate(events):
        block = event_block(e)
        if out and lines_len(out) + len(block) + len(omission_line(events[n:])) > budget:
            out.append(omission_line(events[n:]))
            break
        out.append(block)
    return "\n".join(out)


def event_header(s: sqlite3.Row, t: sqlite3.Row, e: sqlite3.Row) -> str:
    suffix = f"#{e['seq']} tool:{e['name']}"
    return (
        f"session {s['id'][:8]} turn {e['idx']} tool call #{e['seq']}: {event_label(e)} at {day(t['ts'])}\n"
        f"{cite(s, t, suffix, e['model'] or s['model'])}\n"
        f"input: {e['input_summary']}"
    )


def render_event(s: sqlite3.Row, t: sqlite3.Row, e: sqlite3.Row, max_chars: int) -> str:
    head = event_header(s, t, e) + "\n\noutput:\n"
    note = truncation_note(e)
    result = e["result"]
    cap = max_chars - len(head) - (len(note) + 1 if note else 0)
    result = clip_text(result, cap)
    return head + (result or "(no output recorded)") + (f"\n{note}" if note else "")


def render_event_grep(s: sqlite3.Row, t: sqlite3.Row, e: sqlite3.Row, pattern: str, max_chars: int) -> str:
    head = event_header(s, t, e)
    note = truncation_note(e)
    body = render_grep("output", e["result"], pattern, max_chars - len(head) - len(note))
    return head + "\n\n" + (body or f"no lines match /{pattern}/ in this output") + (f"\n{note}" if note else "")


def view_event(s: sqlite3.Row, t: sqlite3.Row, e: sqlite3.Row, grep: str, max_chars: int) -> str:
    if grep:
        return render_event_grep(s, t, e, grep, max_chars)
    return render_event(s, t, e, max_chars)


def render_files(rows: list[sqlite3.Row], pattern: str, conn: sqlite3.Connection) -> str:
    if not rows:
        return f"No indexed session touched a file matching {pattern!r}."
    lines = [f"{len(rows)} sessions touched files matching {pattern!r} (newest first):"]
    for s in rows:
        lines.append(session_line(s, query.prs_for(conn, s["id"])))
        lines.append("    " + matched_files(s["matched"].split("\n")))
    return "\n".join(lines)


def resume_line(s: sqlite3.Row) -> str:
    if not s["transcript_present"]:
        return "resume: not possible, transcript deleted upstream (this indexed copy is all that remains)"
    return f"resume: claude --resume {s['id']}"
