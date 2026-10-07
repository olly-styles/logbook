import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .address import ANCHOR_LEN, TURN_IDX

TOKEN = re.compile(r"\S+")
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RELATIVE = re.compile(r"^(\d+)([dwm])$")
ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
MAX_SNIPPETS = 2
SESSION_MAX_SNIPPETS = 12
TOOL_SCORE_PENALTY = 2.0
SESSION_LEVEL_BONUS = 1.0
CWD_BOOST = 0.75
GREP_CONTEXT = 2
OR_FALLBACK_MIN_TOKENS = 2
FTS_OPERATORS = frozenset({"OR", "AND", "NOT"})
FALLBACK_LIMIT = 5
FALLBACK_MIN_MATCHED_TERMS = 2
TOOL_TAIL_LIMIT = 3
SECONDS_PER_DAY = 86400


class NotFoundError(ValueError):
    pass


class AmbiguousError(ValueError):
    pass


def fts_parts(text: str) -> list[str]:
    tokens = TOKEN.findall(text)
    cores = [tok.rstrip("*") for tok in tokens]
    parts: list[str] = []
    for i, (tok, core) in enumerate(zip(tokens, cores, strict=True)):
        if not core:
            continue
        after_term = bool(parts) and parts[-1] not in FTS_OPERATORS
        if tok in FTS_OPERATORS and after_term and any(cores[i + 1 :]):
            parts.append(tok)
            continue
        quoted = core.replace('"', '""')
        parts.append(f'"{quoted}"*' if tok.endswith("*") else f'"{quoted}"')
    return parts


def fts_query(text: str) -> str:
    return " ".join(fts_parts(text))


def normalise_date(value: str, *, end_of_day: bool) -> str:
    value = value.strip()
    if not value:
        return ""
    rel = RELATIVE.match(value)
    if rel:
        n = int(rel.group(1))
        days = {"d": 1, "w": 7, "m": 30}[rel.group(2)] * n
        return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    if DATE_ONLY.match(value):
        return f"{value}T23:59:59" if end_of_day else f"{value}T00:00:00"
    if ISO_TIMESTAMP.match(value):
        return parse_ts(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    raise ValueError(
        f"unrecognised date {value!r}: use YYYY-MM-DD, a relative span such as 7d, 2w or 3m, "
        "or a full ISO 8601 timestamp such as 2026-09-01T10:00:00"
    )


def parse_ts(ts: str) -> datetime:
    parsed = datetime.fromisoformat(ts)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def pr_clause(pr: str) -> tuple[str, list]:
    text = pr.strip()
    if "#" in text.strip("#"):
        repo, number = text.rsplit("#", 1)
        if number.isdigit():
            return (
                "EXISTS (SELECT 1 FROM prs p WHERE p.session_id = s.id AND p.number = ? AND p.repo LIKE ?)",
                [int(number), f"%{repo.strip()}%"],
            )
    number = text.lstrip("#")
    if number.isdigit():
        return "EXISTS (SELECT 1 FROM prs p WHERE p.session_id = s.id AND p.number = ?)", [int(number)]
    return (
        "EXISTS (SELECT 1 FROM prs p WHERE p.session_id = s.id AND (p.url LIKE ? OR p.repo LIKE ?))",
        [f"%{pr}%", f"%{pr}%"],
    )


@dataclass
class Filters:
    project: str = ""
    since: str = ""
    until: str = ""
    file: str = ""
    pr: str = ""
    exclude_id: str = ""
    session_id: str = ""
    tool: str = ""
    include_headless: bool = False

    def sql(self) -> tuple[str, list]:
        since = normalise_date(self.since, end_of_day=False)
        until = normalise_date(self.until, end_of_day=True)
        parts: list[tuple[str, list]] = [
            ("s.cwd LIKE ?", [f"%{self.project}%"]) if self.project else ("", []),
            ("s.ended_at >= ?", [since]) if since else ("", []),
            ("s.started_at <= ?", [until]) if until else ("", []),
            (
                "EXISTS (SELECT 1 FROM files f WHERE f.session_id = s.id AND f.path LIKE ?)",
                [f"%{self.file}%"],
            )
            if self.file
            else ("", []),
            pr_clause(self.pr) if self.pr else ("", []),
            (
                "EXISTS (SELECT 1 FROM tool_events te WHERE te.session_id = s.id AND te.name LIKE ?)",
                [f"%{self.tool}%"],
            )
            if self.tool
            else ("", []),
            ("s.id != ?", [self.exclude_id]) if self.exclude_id else ("", []),
            ("s.id LIKE ?", [f"{self.session_id.strip()}%"]) if self.session_id else ("", []),
            ("", []) if self.include_headless else ("s.headless = 0", []),
        ]
        clauses = [clause for clause, _ in parts if clause]
        params = [p for _, ps in parts for p in ps]
        return (" AND ".join(clauses) if clauses else "1=1"), params


def under_dir(cwd: str, root: str) -> bool:
    root = root.rstrip("/")
    return bool(root) and (cwd == root or cwd.startswith(root + "/"))


@dataclass
class Hit:
    session: sqlite3.Row
    score: float
    snippets: list[tuple[int, str]] = field(default_factory=list)
    tool_snippets: list[tuple[sqlite3.Row, str]] = field(default_factory=list)
    recap_snippets: list[tuple[str, str]] = field(default_factory=list)
    matched_turns: int = 0
    matched_recaps: int = 0
    matched_terms: set[str] = field(default_factory=set)
    turn_rows: list[tuple[int, int]] = field(default_factory=list)
    tool_rows: list[sqlite3.Row] = field(default_factory=list)
    recap_rows: list[tuple[int, str]] = field(default_factory=list)
    session_rowid: int = 0

    def tool_only(self) -> bool:
        return not (self.matched_turns or self.matched_recaps or self.session_rowid)


class HitCollector:
    def __init__(self, conn: sqlite3.Connection, match: str, where: str, params: list, max_snippets: int) -> None:
        self.conn = conn
        self.match = match
        self.where = where
        self.params = [match, *params]
        self.max_snippets = max_snippets
        self.hits: dict[str, Hit] = {}

    def get(self, session_id: str, score: float) -> Hit:
        hit = self.hits.get(session_id)
        if hit is None:
            session = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            hit = Hit(session=session, score=score)
            self.hits[session_id] = hit
        return hit

    def add_turns(self) -> None:
        for row in self.conn.execute(
            f"""SELECT t.rowid, t.session_id, t.idx, bm25(turns_fts, 1.0, 0.7) AS score
                FROM turns_fts JOIN turns t ON t.rowid = turns_fts.rowid
                JOIN sessions s ON s.id = t.session_id
                WHERE turns_fts MATCH ? AND {self.where}
                ORDER BY score LIMIT 600""",
            self.params,
        ).fetchall():
            hit = self.get(row["session_id"], row["score"])
            hit.matched_turns += 1
            if len(hit.turn_rows) < self.max_snippets:
                hit.turn_rows.append((row["rowid"], row["idx"]))
            hit.score = min(hit.score, row["score"] - 0.3 * (hit.matched_turns - 1))

    def add_tools(self) -> None:
        for row in self.conn.execute(
            f"""SELECT e.*, e.rowid AS rowid, bm25(tool_fts, 1.0, 1.0) AS score
                FROM tool_fts JOIN tool_events e ON e.rowid = tool_fts.rowid
                JOIN sessions s ON s.id = e.session_id
                WHERE tool_fts MATCH ? AND {self.where}
                ORDER BY score LIMIT 400""",
            self.params,
        ).fetchall():
            hit = self.get(row["session_id"], row["score"] + TOOL_SCORE_PENALTY)
            if len(hit.tool_rows) < self.max_snippets:
                hit.tool_rows.append(row)
            hit.score = min(hit.score, row["score"] + TOOL_SCORE_PENALTY)

    def add_sessions(self) -> None:
        for row in self.conn.execute(
            f"""SELECT s.rowid, s.id, bm25(sessions_fts, 2.0, 1.0, 1.5, 0.5) AS score
                FROM sessions_fts JOIN sessions s ON s.rowid = sessions_fts.rowid
                WHERE sessions_fts MATCH ? AND {self.where}
                ORDER BY score LIMIT 200""",
            self.params,
        ).fetchall():
            hit = self.get(row["id"], row["score"])
            hit.session_rowid = row["rowid"]
            hit.score = min(hit.score, row["score"]) - SESSION_LEVEL_BONUS

    def add_recaps(self) -> None:
        for row in self.conn.execute(
            f"""SELECT r.rowid, r.session_id, r.ts, bm25(recaps_fts) AS score
                FROM recaps_fts JOIN recaps r ON r.rowid = recaps_fts.rowid
                JOIN sessions s ON s.id = r.session_id
                WHERE recaps_fts MATCH ? AND {self.where}
                ORDER BY score LIMIT 200""",
            self.params,
        ).fetchall():
            hit = self.get(row["session_id"], row["score"])
            hit.matched_recaps += 1
            if len(hit.recap_rows) < self.max_snippets:
                hit.recap_rows.append((row["rowid"], row["ts"]))
            bonus = SESSION_LEVEL_BONUS if hit.matched_recaps == 1 else 0.0
            hit.score = min(hit.score, row["score"]) - bonus

    def snippets(self, fts: str, column: int, tokens: int, rowids: list[int]) -> dict[int, str]:
        if not rowids:
            return {}
        marks = ",".join("?" * len(rowids))
        return dict(
            self.conn.execute(
                f"""SELECT rowid, snippet({fts}, {column}, '[', ']', ' … ', {tokens})
                    FROM {fts} WHERE {fts} MATCH ? AND rowid IN ({marks})""",
                [self.match, *rowids],
            ).fetchall()
        )

    def fill_snippets(self, hits: list[Hit]) -> None:
        turns = self.snippets("turns_fts", -1, 20, [rowid for h in hits for rowid, _ in h.turn_rows])
        tools = self.snippets("tool_fts", -1, 16, [row["rowid"] for h in hits for row in h.tool_rows])
        recaps = self.snippets("recaps_fts", 0, 20, [rowid for h in hits for rowid, _ in h.recap_rows])
        for h in hits:
            h.snippets = [(idx, turns[rowid]) for rowid, idx in h.turn_rows]
            h.tool_snippets = [(row, tools[row["rowid"]]) for row in h.tool_rows]
            h.recap_snippets = [(ts, recaps[rowid]) for rowid, ts in h.recap_rows]

    def ranked(self, cwd_hint: str, limit: int) -> list[Hit]:
        for hit in self.hits.values():
            if under_dir(hit.session["cwd"], cwd_hint):
                hit.score -= CWD_BOOST
        ordered = by_score_then_newest(self.hits.values())
        text = [h for h in ordered if not h.tool_only()]
        tools = [h for h in ordered if h.tool_only()]
        top = text[:limit] + tools[: TOOL_TAIL_LIMIT if text else limit]
        self.fill_snippets(top)
        return top


def search(conn: sqlite3.Connection, query: str, filters: Filters, limit: int, cwd_hint: str) -> list[Hit]:
    match = fts_query(query)
    if not match:
        return []
    where, params = filters.sql()
    max_snippets = SESSION_MAX_SNIPPETS if filters.session_id else MAX_SNIPPETS
    collector = HitCollector(conn, match, where, params, max_snippets)
    collector.add_turns()
    collector.add_tools()
    collector.add_sessions()
    collector.add_recaps()
    return collector.ranked(cwd_hint, limit)


def by_score_then_newest(hits: Iterable[Hit]) -> list[Hit]:
    newest_first = sorted(hits, key=lambda h: h.session["ended_at"] or "", reverse=True)
    return sorted(newest_first, key=lambda h: h.score)


def or_terms(text: str) -> list[str]:
    tokens = TOKEN.findall(text)
    if len(tokens) < OR_FALLBACK_MIN_TOKENS or any(part in FTS_OPERATORS for part in fts_parts(text)):
        return []
    cores = [tok.strip('"') for tok in tokens]
    cores = [core for core in cores if core.rstrip("*")]
    return cores if len(cores) >= OR_FALLBACK_MIN_TOKENS else []


def fallback_search(
    conn: sqlite3.Connection, terms: list[str], filters: Filters, limit: int, cwd_hint: str
) -> list[Hit]:
    where, params = filters.sql()
    merged: dict[str, Hit] = {}
    for term in terms:
        match = fts_query(term)
        if not match:
            continue
        collector = HitCollector(conn, match, where, params, 0)
        collector.add_turns()
        collector.add_tools()
        collector.add_sessions()
        collector.add_recaps()
        for session_id, hit in collector.hits.items():
            kept = merged.setdefault(session_id, Hit(session=hit.session, score=hit.score))
            kept.matched_terms.add(term)
            kept.score = min(kept.score, hit.score)
    hits = [hit for hit in merged.values() if len(hit.matched_terms) >= FALLBACK_MIN_MATCHED_TERMS]
    for hit in hits:
        if cwd_hint and hit.session["cwd"] and hit.session["cwd"].startswith(cwd_hint):
            hit.score -= CWD_BOOST
    hits.sort(key=lambda h: h.session["ended_at"] or "", reverse=True)
    hits.sort(key=lambda h: (-len(h.matched_terms), h.score))
    return hits[:limit]


def recent(conn: sqlite3.Connection, filters: Filters, limit: int) -> list[sqlite3.Row]:
    where, params = filters.sql()
    return conn.execute(
        f"SELECT * FROM sessions s WHERE {where} AND s.turn_count > 0 ORDER BY s.ended_at DESC LIMIT ?",
        [*params, limit],
    ).fetchall()


def resolve_session(conn: sqlite3.Connection, id_or_prefix: str) -> sqlite3.Row:
    key = id_or_prefix.strip()
    if not key:
        raise NotFoundError("empty session id")
    rows = conn.execute("SELECT * FROM sessions WHERE id LIKE ? LIMIT 5", (f"{key}%",)).fetchall()
    if not rows:
        raise NotFoundError(f"no session matching {key!r}")
    exact = [r for r in rows if r["id"] == key]
    if exact:
        return exact[0]
    if len(rows) > 1:
        raise AmbiguousError(f"{key!r} matches {len(rows)} sessions: " + ", ".join(r["id"][:12] for r in rows))
    return rows[0]


def turns_for(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY idx", (session_id,)).fetchall()


def turn_at(conn: sqlite3.Connection, session_id: str, idx: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM turns WHERE session_id = ? AND idx = ?", (session_id, idx)).fetchone()
    if row is None:
        raise NotFoundError(f"session {session_id[:8]} has no turn {idx}")
    return row


def turn_by_uuid(conn: sqlite3.Connection, session_id: str, uuid_prefix: str) -> sqlite3.Row:
    key = uuid_prefix.strip()
    if not key:
        raise NotFoundError("empty turn uuid")
    rows = conn.execute(
        "SELECT * FROM turns WHERE session_id = ? AND uuid LIKE ? LIMIT 3", (session_id, f"{key}%")
    ).fetchall()
    if not rows:
        raise NotFoundError(f"session {session_id[:8]} has no turn with uuid {key!r}")
    if len(rows) > 1:
        raise AmbiguousError(f"turn uuid {key!r} matches {len(rows)} turns in session {session_id[:8]}")
    return rows[0]


def turn_uuid_exists(conn: sqlite3.Connection, session_id: str, prefix: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM turns WHERE session_id = ? AND uuid LIKE ? LIMIT 1", (session_id, f"{prefix}%")
    ).fetchone()
    return row is not None


def turn_by_ref(conn: sqlite3.Connection, session_id: str, ref: str) -> sqlite3.Row:
    key = ref.strip()
    m = TURN_IDX.match(key)
    anchor_like = len(key) == ANCHOR_LEN and not key.startswith("t")
    if m and not (anchor_like and turn_uuid_exists(conn, session_id, key)):
        return turn_at(conn, session_id, int(m.group(1)))
    return turn_by_uuid(conn, session_id, key)


def grep_lines(text: str, pattern: str, context: int = GREP_CONTEXT) -> list[tuple[int, str, bool]]:
    rx = re.compile(pattern, re.IGNORECASE)
    lines = text.split("\n")
    matched = {i for i, line in enumerate(lines) if rx.search(line)}
    keep: set[int] = set()
    for i in matched:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    return [(i + 1, lines[i], i in matched) for i in sorted(keep)]


def grep_session(
    conn: sqlite3.Connection, session_id: str, pattern: str, context: int = 0
) -> list[tuple[int, str, str, str, str]]:
    out: list[tuple[int, str, str, str, str]] = []
    for r in recaps_for(conn, session_id):
        out.extend((-1, "", r["ts"], "recap", line) for _, line, _ in grep_lines(r["content"], pattern, context))
    for t in turns_for(conn, session_id):
        for role, text in (("user", t["user_text"]), ("claude", t["assistant_text"])):
            out.extend((t["idx"], t["uuid"], t["ts"], role, line) for _, line, _ in grep_lines(text, pattern, context))
        for e in events_for(conn, session_id, t["idx"]):
            role = f"tool:{e['name']}#{e['seq']}"
            found = [
                (t["idx"], t["uuid"], t["ts"], role, line) for _, line, _ in grep_lines(e["result"], pattern, context)
            ]
            if not found:
                found = [
                    (t["idx"], t["uuid"], t["ts"], f"{role} input", line)
                    for _, line, _ in grep_lines(e["input_summary"], pattern, context)
                ]
            out.extend(found)
    return out


def events_for(conn: sqlite3.Connection, session_id: str, idx: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tool_events WHERE session_id = ? AND idx = ? ORDER BY seq", (session_id, idx)
    ).fetchall()


def event_at(conn: sqlite3.Connection, session_id: str, idx: int, seq: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM tool_events WHERE session_id = ? AND idx = ? AND seq = ?", (session_id, idx, seq)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"session {session_id[:8]} turn {idx} has no tool call {seq}")
    return row


def calls_matching(conn: sqlite3.Connection, session_id: str, pattern: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT idx, seq, name, model FROM tool_events WHERE session_id = ? AND name LIKE ? ORDER BY idx, seq",
        (session_id, f"%{pattern}%"),
    ).fetchall()


def prs_for(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT repo, number, url FROM prs WHERE session_id = ? ORDER BY number DESC", (session_id,)
    ).fetchall()


def recaps_for(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT ts, content FROM recaps WHERE session_id = ? ORDER BY ts", (session_id,)).fetchall()


def files_for(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT path, action, n FROM files WHERE session_id = ? ORDER BY action DESC, n DESC, path", (session_id,)
    ).fetchall()


def sessions_touching(conn: sqlite3.Connection, pattern: str, filters: Filters, limit: int) -> list[sqlite3.Row]:
    where, params = filters.sql()
    return conn.execute(
        f"""SELECT s.*, group_concat(f.path || ':' || f.action, char(10)) AS matched
            FROM files f JOIN sessions s ON s.id = f.session_id
            WHERE f.path LIKE ? AND {where}
            GROUP BY s.id ORDER BY s.ended_at DESC LIMIT ?""",
        [f"%{pattern}%", *params, limit],
    ).fetchall()


def select_turns(rows: list[sqlite3.Row], spec: str) -> list[sqlite3.Row]:
    spec = spec.strip()
    if not spec:
        return rows
    if spec.startswith("last:"):
        n = int(spec[5:])
        return rows[-n:] if n > 0 else []
    if spec.startswith("first:"):
        n = int(spec[6:])
        return rows[:n]
    wanted: set[int] = set()
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            wanted.update(range(int(lo), int(hi) + 1))
        elif part:
            wanted.add(int(part))
    return [r for r in rows if r["idx"] in wanted]


def count(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "sessions": count(conn, "SELECT COUNT(*) FROM sessions"),
        "headless_sessions": count(conn, "SELECT COUNT(*) FROM sessions WHERE headless = 1"),
        "transcripts_deleted": count(conn, "SELECT COUNT(*) FROM sessions WHERE transcript_present = 0"),
        "archived_sessions": count(conn, "SELECT COUNT(*) FROM ingest_state WHERE archived_bytes > 0"),
        "archive_bytes": count(conn, "SELECT COALESCE(SUM(archived_bytes), 0) FROM ingest_state"),
        "turns": count(conn, "SELECT COUNT(*) FROM turns"),
        "sessions_with_pr": count(conn, "SELECT COUNT(DISTINCT session_id) FROM prs"),
        "prs": count(conn, "SELECT COUNT(*) FROM prs"),
        "tool_events": count(conn, "SELECT COUNT(*) FROM tool_events"),
        "truncated_tool_results": count(conn, "SELECT COUNT(*) FROM tool_events WHERE truncated = 1"),
        "sessions_with_recap": count(conn, "SELECT COUNT(*) FROM sessions WHERE summary != ''"),
        "recaps": count(conn, "SELECT COUNT(*) FROM recaps"),
    }
