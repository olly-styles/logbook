import json
import sqlite3
from pathlib import Path

from .parse import ParsedSession
from .paths import private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL DEFAULT '',
    git_branch TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    first_prompt TEXT NOT NULL DEFAULT '',
    last_reply TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    turn_count INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    transcript_present INTEGER NOT NULL DEFAULT 1,
    summary TEXT NOT NULL DEFAULT '',
    headless INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS sessions_cwd ON sessions(cwd);

CREATE TABLE IF NOT EXISTS prs (
    session_id TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '',
    number INTEGER NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, repo, number)
);
CREATE INDEX IF NOT EXISTS prs_number ON prs(number);

CREATE TABLE IF NOT EXISTS turns (
    session_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    uuid TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL DEFAULT '',
    user_text TEXT NOT NULL DEFAULT '',
    assistant_text TEXT NOT NULL DEFAULT '',
    tool_calls INTEGER NOT NULL DEFAULT 0,
    tools_json TEXT NOT NULL DEFAULT '{}',
    files_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, idx)
);
CREATE INDEX IF NOT EXISTS turns_uuid ON turns(uuid);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    user_text, assistant_text,
    content='turns', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, user_text, assistant_text) VALUES (new.rowid, new.user_text, new.assistant_text);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, user_text, assistant_text)
        VALUES ('delete', old.rowid, old.user_text, old.assistant_text);
END;

CREATE TABLE IF NOT EXISTS tool_events (
    session_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    input_summary TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, idx, seq)
);

CREATE VIRTUAL TABLE IF NOT EXISTS tool_fts USING fts5(
    input_summary, result,
    content='tool_events', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS tool_ai AFTER INSERT ON tool_events BEGIN
    INSERT INTO tool_fts(rowid, input_summary, result) VALUES (new.rowid, new.input_summary, new.result);
END;
CREATE TRIGGER IF NOT EXISTS tool_ad AFTER DELETE ON tool_events BEGIN
    INSERT INTO tool_fts(tool_fts, rowid, input_summary, result)
        VALUES ('delete', old.rowid, old.input_summary, old.result);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    title, first_prompt, summary, cwd,
    content='sessions', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, title, first_prompt, summary, cwd)
        VALUES (new.rowid, new.title, new.first_prompt, new.summary, new.cwd);
END;
CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, title, first_prompt, summary, cwd)
        VALUES ('delete', old.rowid, old.title, old.first_prompt, old.summary, old.cwd);
END;
CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, title, first_prompt, summary, cwd)
        VALUES ('delete', old.rowid, old.title, old.first_prompt, old.summary, old.cwd);
    INSERT INTO sessions_fts(rowid, title, first_prompt, summary, cwd)
        VALUES (new.rowid, new.title, new.first_prompt, new.summary, new.cwd);
END;

CREATE TABLE IF NOT EXISTS recaps (
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, ts)
);

CREATE VIRTUAL TABLE IF NOT EXISTS recaps_fts USING fts5(
    content,
    content='recaps', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS recaps_ai AFTER INSERT ON recaps BEGIN
    INSERT INTO recaps_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS recaps_ad AFTER DELETE ON recaps BEGIN
    INSERT INTO recaps_fts(recaps_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;

CREATE TABLE IF NOT EXISTS files (
    session_id TEXT NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, path, action)
);
CREATE INDEX IF NOT EXISTS files_path ON files(path);

CREATE TABLE IF NOT EXISTS ingest_state (
    session_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    archived_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_VERSION = "13"


FTS_TABLES = ("turns_fts", "tool_fts", "sessions_fts", "recaps_fts")


def schema_version(conn: sqlite3.Connection) -> str:
    has_meta = conn.execute("SELECT name FROM sqlite_master WHERE name = 'meta'").fetchone() is not None
    if not has_meta:
        return ""
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return row["value"] if row else ""


def drop_all(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    for name in [r["name"] for r in rows]:
        if not name.startswith("sqlite_"):
            conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.commit()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def reset(conn: sqlite3.Connection) -> None:
    drop_all(conn)
    ensure_schema(conn)


def connect(path: Path) -> sqlite3.Connection:
    private_dir(path.parent)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if schema_version(conn) != SCHEMA_VERSION:
        reset(conn)
    return conn


def replace_session(conn: sqlite3.Connection, parsed: ParsedSession, *, present: bool = True) -> None:
    conn.execute("DELETE FROM turns WHERE session_id = ?", (parsed.id,))
    conn.execute("DELETE FROM tool_events WHERE session_id = ?", (parsed.id,))
    conn.execute("DELETE FROM files WHERE session_id = ?", (parsed.id,))
    conn.execute("DELETE FROM prs WHERE session_id = ?", (parsed.id,))
    conn.execute("DELETE FROM recaps WHERE session_id = ?", (parsed.id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (parsed.id,))
    last_reply = next((t.assistant_text for t in reversed(parsed.turns) if t.assistant_text), "")
    conn.execute(
        """INSERT INTO sessions (id, cwd, git_branch, title, first_prompt, last_reply, started_at, ended_at,
           turn_count, model, transcript_present, summary, headless)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            parsed.id,
            parsed.cwd,
            parsed.git_branch,
            parsed.title,
            parsed.first_prompt,
            last_reply,
            parsed.started_at,
            parsed.ended_at,
            len(parsed.turns),
            parsed.model,
            int(present),
            parsed.away_summary,
            int(parsed.headless),
        ),
    )
    conn.executemany(
        "INSERT INTO prs (session_id, repo, number, url) VALUES (?, ?, ?, ?)",
        [(parsed.id, repo, number, url) for (repo, number), url in parsed.prs.items()],
    )
    conn.executemany(
        "INSERT INTO recaps (session_id, ts, content) VALUES (?, ?, ?)",
        [(parsed.id, ts, content) for ts, content in parsed.recaps.items()],
    )
    conn.executemany(
        """INSERT INTO turns (session_id, idx, uuid, ts, user_text, assistant_text, tool_calls, tools_json, files_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                parsed.id,
                t.idx,
                t.uuid,
                t.ts,
                t.user_text,
                t.assistant_text,
                t.tool_calls,
                json.dumps(dict(t.tools), sort_keys=True),
                json.dumps({p: sorted(a) for p, a in t.files.items()}, sort_keys=True),
            )
            for t in parsed.turns
        ],
    )
    conn.executemany(
        """INSERT INTO tool_events (session_id, idx, seq, name, input_summary, result, truncated, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (parsed.id, t.idx, e.seq, e.name, e.input_summary, e.result, int(e.truncated), e.model)
            for t in parsed.turns
            for e in t.events
        ],
    )
    conn.executemany(
        "INSERT INTO files (session_id, path, action, n) VALUES (?, ?, ?, ?)",
        [(parsed.id, path, action, n) for (path, action), n in parsed.files.items()],
    )


def mark_transcript_missing(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET transcript_present = 0 WHERE id = ?", (session_id,))
