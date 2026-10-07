import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import db, prompts, qlog, query, render
from .address import Address, parse_address
from .paths import db_path
from .presence import active_session_id

mcp = MCPServer("logbook", instructions=prompts.load("instructions.txt"))
_connect_lock = threading.Lock()
DEFAULT_LISTING_WINDOW = "14d"
SEARCH_DEFAULT_LIMIT = 8
READ_DEFAULT_MAX_CHARS = 8000


def conn() -> sqlite3.Connection:
    with _connect_lock:
        return db.connect(db_path())


def cwd_hint() -> str:
    return str(Path.cwd())


def filters(*, include_headless: bool = False, **kwargs: str) -> query.Filters:
    return query.Filters(exclude_id=active_session_id(), include_headless=include_headless, **kwargs)


def at_least_one(name: str, value: int) -> int:
    if value < 1:
        raise ValueError(f"{name}={value} must be at least 1")
    return value


def listing_window(since: str, pr: str, file: str) -> str:
    if since or pr or file:
        return since
    return DEFAULT_LISTING_WINDOW


def run_listing(c: sqlite3.Connection, f: query.Filters, limit: int) -> tuple[str, int]:
    if f.file:
        rows = query.sessions_touching(c, f.file, f, limit)
        return render.render_files(rows, f.file, c), len(rows)
    rows = query.recent(c, f, limit)
    return render.render_recent(rows, c), len(rows)


def run_search(
    c: sqlite3.Connection, query_text: str, f: query.Filters, limit: int, used: dict, *, include_tools: bool
) -> tuple[str, int]:
    hits = query.search(c, query_text, f, limit, cwd_hint())
    terms = [] if hits else query.or_terms(query_text)
    if not terms:
        return render.render_hits(hits, query_text, c, f, show_tools=include_tools), len(hits)
    used["or_fallback"] = " OR ".join(terms)
    weak = query.fallback_search(c, terms, f, query.FALLBACK_LIMIT, cwd_hint())
    return render.render_fallback(weak, query_text, terms, c), len(weak)


@mcp.tool()
def search(
    query_text: str = "",
    project: str = "",
    since: str = "",
    until: str = "",
    file: str = "",
    pr: str = "",
    session_id: str = "",
    limit: int = SEARCH_DEFAULT_LIMIT,
    *,
    include_tools: bool = False,
    include_headless: bool = False,
) -> str:
    """Find past sessions by full text, or list them when query_text is empty.

    Search covers prompts, replies, titles, recaps and tool inputs/outputs. Words are ANDed within one turn, recap or
    tool result; * is a prefix (pyrig*); upper-case OR, AND, NOT are operators. A hit is "<session8> <date> <cwd>
    (<branch>) PR <repo#n> <N>t <model> <title>" with "tool: t<idx>#<seq> <Tool> [<model>]" refs when tool output
    matched (Agent refs carry the subagent's model), then the latest recap or last reply, at most one earlier recap
    and up to two snippets tagged t<idx>; PR urls only with pr=, matching paths only with file=. Sessions matching
    only in tool output follow every text match, one line each, at most three after a text match; when nothing
    matched in text, up to limit; include_tools=True shows their snippets. When nothing holds all the words, up to
    five sessions matching two or more of them are listed one line each. Ranking is BM25 plus a boost for the
    current directory; among equal hits read the newest first unless the question names a time or asks where
    something was first done. The current session is excluded; headless sessions (SDK runs, /tmp/claude-* workdirs) need
    include_headless=True.

    Empty query_text lists sessions newest first, 8 rows unless limit=, over the last 14 days unless
    since=, pr= or file= is given: search(project="myrepo"), search(pr="119"), search(file="auth.py").

    Filters: project = substring of the cwd; since/until = YYYY-MM-DD, 7d/2w/3m or an ISO 8601 timestamp; file =
    substring of a path read or written; pr = PR number or substring of the PR url or repo; session_id = id or
    8-char prefix to search one session (up to 12 snippets). limit must be at least 1.

    Next: read("<session8>") for the index of turns, then read("<session8>", grep=...), read("<session8>/t<idx>"),
    read("<session8>/t<idx>#<seq>"), each step costing more. Never quote search output; snippets are FTS excerpts.
    """
    used = qlog.used(
        project=project,
        since=since,
        until=until,
        file=file,
        pr=pr,
        session_id=session_id,
        include_tools=include_tools,
        include_headless=include_headless,
        limit=limit if limit != SEARCH_DEFAULT_LIMIT else "",
    )

    def run() -> tuple[str, int]:
        with closing(conn()) as c:
            if not query_text.strip():
                f = filters(
                    project=project,
                    since=listing_window(since, pr, file),
                    until=until,
                    file=file,
                    pr=pr,
                    include_headless=include_headless,
                )
                return run_listing(c, f, at_least_one("limit", limit))
            f = filters(
                project=project,
                since=since,
                until=until,
                file=file,
                pr=pr,
                session_id=session_id,
                include_headless=include_headless,
            )
            return run_search(c, query_text, f, at_least_one("limit", limit), used, include_tools=include_tools)

    return qlog.logged("search", active_session_id(), cwd_hint(), query_text, used, run)


def resolve(c: sqlite3.Connection, addr: Address) -> tuple[sqlite3.Row, sqlite3.Row | None]:
    s = query.resolve_session(c, addr.session)
    if not addr.turn:
        return s, None
    return s, query.turn_by_ref(c, s["id"], addr.turn)


def read_range(
    c: sqlite3.Connection, addr: Address, grep: str, max_chars: int, *, include_tools: bool
) -> tuple[str, int]:
    s = query.resolve_session(c, addr.session)
    turns = query.turns_between(c, s["id"], int(addr.turn.lstrip("t")), addr.turn_end)

    def events(t: sqlite3.Row) -> list[sqlite3.Row]:
        return query.events_for(c, s["id"], t["idx"]) if include_tools else []

    return render.view_turn_range(s, turns, events, grep, max_chars)


def compose_address(address: str, seq: int) -> Address:
    addr = parse_address(address)
    if seq >= 0:
        addr.seq = seq
    if addr.seq >= 0 and not addr.turn:
        raise ValueError(f"seq={addr.seq} needs a turn: use {addr.session}/t<idx>#{addr.seq}")
    if addr.seq >= 0 and addr.turn_end >= 0:
        raise ValueError(f"seq={addr.seq} needs a single turn, not a range: use {addr.session}/{addr.turn}#{addr.seq}")
    return addr


def run_read(
    c: sqlite3.Connection, addr: Address, turns: str, grep: str, max_chars: int, *, include_tools: bool
) -> tuple[str, int]:
    if addr.turn_end >= 0:
        if turns.strip():
            raise ValueError(f"turns={turns!r} does not apply to the turn range {addr.turn}-{addr.turn_end}; drop one")
        return read_range(c, addr, grep, max_chars, include_tools=include_tools)
    s, t = resolve(c, addr)
    if t is None:
        return render.view_session(c, s, turns, grep, max_chars), 1
    if addr.seq >= 0:
        e = query.event_at(c, s["id"], t["idx"], addr.seq)
        return render.view_event(s, t, e, grep, max_chars), 1
    events = query.events_for(c, s["id"], t["idx"]) if include_tools else []
    return render.view_turn(s, t, events, grep, max_chars), 1


@mcp.tool()
def read(
    address: str,
    seq: int = -1,
    turns: str = "",
    grep: str = "",
    max_chars: int = READ_DEFAULT_MAX_CHARS,
    *,
    include_tools: bool = False,
) -> str:
    """Read one session, one turn or one tool result.

    address: "27c3625f" (session id or 8-char prefix); "27c3625f/70d158d1" or "27c3625f/t16" (turn, by stable
    8-char uuid anchor or by index); "27c3625f/t16-20" (turns 16 to 20 in one call); "27c3625f/70d158d1#3" (tool
    call 3 of that turn; seq=3 on a turn address is the same). max_chars must be at least 1.

    Session: header (dates, cwd, branch, PRs, recap trail, files written, resume command), then one line per turn
    "t<idx> <uuid8> <time> [<seq range> <tool counts>] <start of user message>", middle turns collapsed to fit
    max_chars. turns="last:5", "first:3", "2-6" or "1,4,9" returns those turns' user message and final reply,
    clipped; never quote from it. grep=<regex> lists every matching line across prompts, replies, tool results and,
    for a tool call whose result has no match, its input, as "t<idx> <uuid8> <date> <role>: <line>" (roles user,
    claude, tool:<Name>#<seq>, tool:<Name>#<seq> input; recaps only when no turn matches; at most 60 lines).

    Turn: full user message and final reply, tools and files used, and a "cite: [<session8> <uuid8> t<idx> <date>
    <model>]" line; append the role when quoting. grep=<regex> returns matching lines with two lines of context;
    grep a turn before include_tools=True, which adds every tool call with its input and a clipped result.
    A turn range gives each turn in full, in order, until max_chars, then names the turns left out; with grep= it
    keeps only the turns that match.

    Tool result (#seq): the recorded output with its own cite line (an Agent result cites the subagent's model);
    capped when indexed (8000 chars, 20000 for fetch-like tools), secrets redacted, a cut result ends "[output
    truncated ...]". grep=<regex> applies too.

    Ladder: search -> read(session) -> read(session, grep=) -> read(session/turn) -> read(session/turn#seq).
    """
    used = qlog.used(
        seq=seq if seq >= 0 else "",
        turns=turns,
        grep=grep,
        include_tools=include_tools,
        max_chars=max_chars if max_chars != READ_DEFAULT_MAX_CHARS else "",
    )

    def run() -> tuple[str, int]:
        addr = compose_address(address, seq)
        with closing(conn()) as c:
            return run_read(c, addr, turns, grep, at_least_one("max_chars", max_chars), include_tools=include_tools)

    return qlog.logged("read", active_session_id(), cwd_hint(), address.strip(), used, run)


def main() -> None:
    mcp.run(transport="stdio")
