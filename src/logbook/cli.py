import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from . import db, install, prompts, qlog, query, render
from .paths import db_path, private_dir
from .query import SECONDS_PER_DAY
from .sync import SyncStats, archive_transcript, index_transcript, optimize_fts, rebuild, sync

HOOK_LIMIT = 5
HOOK_MIN_TURNS = 2
HOOK_MIN_PROMPT_CHARS = 40
MARKER_MAX_AGE_DAYS = 30


def print_stats(conn: sqlite3.Connection) -> None:
    for key, value in query.stats(conn).items():
        print(f"{key}: {value}")


def print_sync_report(stats: SyncStats) -> None:
    print(
        f"scanned {stats.scanned} transcripts, parsed {stats.parsed}, archived {stats.archived}, "
        f"newly missing {stats.missing}, failed {stats.failed}, {stats.seconds:.1f}s"
    )


def cmd_sync(_args: argparse.Namespace) -> None:
    conn = db.connect(db_path())
    print_sync_report(sync(conn))
    print_stats(conn)


def cmd_rebuild(_args: argparse.Namespace) -> None:
    conn = db.connect(db_path())
    print_sync_report(rebuild(conn))
    print_stats(conn)


def cmd_stats(_args: argparse.Namespace) -> None:
    print_stats(db.connect(db_path()))


def cmd_hook(_args: argparse.Namespace) -> None:
    conn = db.connect(db_path())
    sync(conn)
    optimize_fts(conn)
    prune_markers(private_dir(db_path().parent / "prompted"))


def index_own_transcript(conn: sqlite3.Connection, payload: dict) -> None:
    session_id = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")
    if session_id and transcript:
        index_transcript(conn, Path(transcript), session_id)


def cmd_index_hook(_args: argparse.Namespace) -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    conn = db.connect(db_path())
    index_own_transcript(conn, payload)
    if payload.get("hook_event_name") == "SessionEnd" and payload.get("session_id") and payload.get("transcript_path"):
        archive_transcript(conn, Path(payload["transcript_path"]), payload["session_id"])


def prune_markers(folder: Path) -> None:
    cutoff = time.time() - MARKER_MAX_AGE_DAYS * SECONDS_PER_DAY
    for marker in folder.iterdir():
        if marker.is_file() and marker.stat().st_mtime < cutoff:
            marker.unlink()


def worth_listing(row: sqlite3.Row) -> bool:
    return (
        row["turn_count"] >= HOOK_MIN_TURNS
        or len(row["first_prompt"]) >= HOOK_MIN_PROMPT_CHARS
        or render.is_today(row["ended_at"])
    )


def prompt_marker(session_id: str) -> Path:
    return private_dir(db_path().parent / "prompted") / session_id


def recent_rows(conn: sqlite3.Connection, filters: query.Filters) -> list[sqlite3.Row]:
    candidates = query.recent(conn, filters, HOOK_LIMIT * 3)
    return [r for r in candidates if worth_listing(r)][:HOOK_LIMIT]


def recent_lines(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[str]:
    if not rows:
        return ["No earlier sessions indexed yet."]
    lines = ["Most recent sessions, all projects (id, date, directory, branch, PR, turns as Nt, model, first topic):"]
    lines.extend("- " + render.session_line(s, query.prs_for(conn, s["id"])) for s in rows)
    return lines


def hook_context(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
    sessions = "\n".join(recent_lines(conn, rows))
    return prompts.load("prompt_hook.txt").format(total=query.stats(conn)["sessions"], sessions=sessions)


def cmd_prompt_hook(_args: argparse.Namespace) -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    conn = db.connect(db_path())
    index_own_transcript(conn, payload)
    current = payload.get("session_id", "")
    if current and prompt_marker(current).exists():
        return
    started = time.monotonic()
    rows = recent_rows(conn, query.Filters(exclude_id=current))
    context = hook_context(conn, rows)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))
    if current:
        prompt_marker(current).touch()
    qlog.record(
        "search",
        current,
        payload.get("cwd", ""),
        "",
        {"source": "prompt-hook"},
        len(rows),
        (time.monotonic() - started) * 1000,
        chars=len(context),
        listed=tuple(r["id"] for r in rows),
    )


def cmd_install(_args: argparse.Namespace) -> None:
    exe = install.executable()
    print("Indexing transcripts on disk; a first run over a large history can take a minute.")
    print_sync_report(sync(db.connect(db_path())))
    install.wire(exe)


def cmd_serve(_args: argparse.Namespace) -> None:
    from .server import main as serve_main

    serve_main()


def add_commands(sub: argparse._SubParsersAction) -> None:
    sub.add_parser("sync", help="archive and index new or changed transcripts").set_defaults(fn=cmd_sync)
    sub.add_parser("rebuild", help="drop the index and rebuild it from the archive plus disk").set_defaults(
        fn=cmd_rebuild
    )
    sub.add_parser("stats", help="index statistics").set_defaults(fn=cmd_stats)
    sub.add_parser("hook", help="SessionStart hook: sync and optimize the index, no output").set_defaults(fn=cmd_hook)
    sub.add_parser(
        "prompt-hook",
        help="UserPromptSubmit hook: index this session; on its first prompt, inject the most recent sessions",
    ).set_defaults(fn=cmd_prompt_hook)
    sub.add_parser(
        "index-hook",
        help="Stop and SessionEnd hook: index this session's transcript; on SessionEnd also archive it",
    ).set_defaults(fn=cmd_index_hook)
    sub.add_parser("serve", help="run the MCP server over stdio").set_defaults(fn=cmd_serve)
    sub.add_parser(
        "install",
        help="index existing transcripts, then wire the hooks, the recall skill and agent, and the MCP server",
    ).set_defaults(fn=cmd_install)


def main() -> None:
    p = argparse.ArgumentParser(prog="logbook", description="Index and search Claude Code session history")
    sub = p.add_subparsers(dest="cmd", required=True)
    add_commands(sub)
    args = p.parse_args()
    args.fn(args)
