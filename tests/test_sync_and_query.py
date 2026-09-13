import io
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from conftest import SID_A, SID_B, assistant, text, transcript_a, user, write_transcript

from logbook import db, query, render
from logbook.sync import archive_transcript, index_transcript, optimize_fts, sync


def test_sync_indexes_transcripts(conn: sqlite3.Connection) -> None:
    stats = sync(conn)
    assert stats.parsed == 2
    st = query.stats(conn)
    assert st["sessions"] == 2
    assert st["tool_events"] == 5
    a = query.resolve_session(conn, "aaaaaaaa")
    assert a["turn_count"] == 2
    assert a["last_reply"] == "All 12 tests pass."


def test_optimize_fts_merges_segments_without_changing_results(conn: sqlite3.Connection, claude_home: Path) -> None:
    path = claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl"
    for i in range(3):
        write_transcript(path, [*transcript_a(), user(f"and step {i}", f"2026-08-01T11:0{i}:00.000Z", SID_A)])
        os.utime(path, (time.time() + i, time.time() + i))
        sync(conn)
    before = [h.session["id"] for h in query.search(conn, "widget", query.Filters(), 10, "")]
    assert segment_count(conn, "turns_fts") > 1
    optimize_fts(conn)
    assert all(segment_count(conn, t) <= 1 for t in db.FTS_TABLES)
    assert [h.session["id"] for h in query.search(conn, "widget", query.Filters(), 10, "")] == before


def segment_count(conn: sqlite3.Connection, fts: str) -> int:
    return conn.execute(f"SELECT count(DISTINCT segid) FROM {fts}_idx").fetchone()[0]


def test_sync_is_incremental_and_reparses_changed_files(conn: sqlite3.Connection, claude_home: Path) -> None:
    sync(conn)
    stats = sync(conn)
    assert stats.parsed == 0
    path = claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl"
    lines = [
        *transcript_a(),
        user("one more thing: add a Makefile", "2026-08-01T11:00:00.000Z", SID_A),
        assistant([text("Makefile added.")], "2026-08-01T11:00:01.000Z", SID_A, msg_id="m9"),
    ]
    write_transcript(path, lines)
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    stats = sync(conn)
    assert stats.parsed == 1
    assert query.resolve_session(conn, SID_A)["turn_count"] == 3


def test_sync_skips_malformed_lines_and_reports_them(
    conn: sqlite3.Connection, claude_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl"
    lines = transcript_a()
    lines.insert(5, '{"type": "user", "message": {"role": "user", "content": "cut off mid')
    lines.insert(9, "null")
    write_transcript(path, lines)
    stats = sync(conn)
    err = capsys.readouterr().err
    assert stats.parsed == 2
    assert stats.failed == 0
    assert stats.archived == 2
    a = query.resolve_session(conn, SID_A)
    assert a["turn_count"] == 2
    assert a["last_reply"] == "All 12 tests pass."
    assert query.resolve_session(conn, SID_B)["turn_count"] == 2
    assert f"logbook: skipping {path}:6: " in err
    assert f"logbook: skipping {path}:10: expected a JSON object, got NoneType" in err
    assert index_transcript(conn, path, SID_A)
    assert f"{path}:6:" in capsys.readouterr().err


def test_index_transcript_indexes_one_file_without_archiving(conn: sqlite3.Connection, claude_home: Path) -> None:
    path = claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl"
    archive = claude_home.parent / "archive" / f"{SID_A}.jsonl.gz"
    assert index_transcript(conn, path, SID_A)
    assert query.resolve_session(conn, SID_A)["turn_count"] == 2
    assert not archive.exists()
    assert query.stats(conn)["sessions"] == 1
    assert not index_transcript(conn, path.with_name("missing.jsonl"), "missing")
    assert archive_transcript(conn, path, SID_A)
    assert archive.exists()
    assert not archive_transcript(conn, path, SID_A)
    assert conn.execute("SELECT archived_bytes FROM ingest_state WHERE session_id = ?", (SID_A,)).fetchone()[0] > 0
    assert sync(conn).parsed == 1


def test_index_hook_indexes_own_session_and_archives_on_session_end(
    conn: sqlite3.Connection, claude_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from logbook.cli import main

    path = claude_home / "projects" / "-home-u-leetcode" / f"{SID_B}.jsonl"
    archive = claude_home.parent / "archive" / f"{SID_B}.jsonl.gz"
    for event in ("Stop", "SessionEnd"):
        payload = {"session_id": SID_B, "transcript_path": str(path), "hook_event_name": event}
        monkeypatch.setattr("sys.argv", ["logbook", "index-hook"])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        main()
        assert capsys.readouterr().out == ""
        assert query.resolve_session(conn, SID_B)["turn_count"] == 2
        assert archive.exists() == (event == "SessionEnd")
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    main()
    assert capsys.readouterr().out == ""


def test_prompt_hook_indexes_own_session_before_answering(
    conn: sqlite3.Connection, claude_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl"
    payload = {"cwd": "/home/u", "session_id": SID_A, "transcript_path": str(path), "prompt": "hello"}
    assert query.stats(conn)["sessions"] == 0
    run_prompt_hook(monkeypatch, capsys, payload)
    assert query.resolve_session(conn, SID_A)["turn_count"] == 2
    assert query.stats(conn)["sessions"] == 1


def test_deleted_transcript_keeps_index_entry(conn: sqlite3.Connection, claude_home: Path) -> None:
    sync(conn)
    (claude_home / "projects" / "-home-u-leetcode" / f"{SID_B}.jsonl").unlink()
    stats = sync(conn)
    assert stats.missing == 1
    s = query.resolve_session(conn, SID_B)
    assert s["transcript_present"] == 0
    assert query.turns_for(conn, s["id"])


def test_search_ranks_and_filters(conn: sqlite3.Connection) -> None:
    sync(conn)
    hits = query.search(conn, "pyright", query.Filters(), 10, "")
    assert [h.session["id"] for h in hits] == [SID_A]
    assert hits[0].snippets
    assert hits[0].snippets[0][0] == 0
    assert query.search(conn, "pyright", query.Filters(project="leetcode"), 10, "") == []
    assert query.search(conn, "resume button", query.Filters(file="timer.tsx"), 10, "")[0].session["id"] == SID_B
    assert query.search(conn, "pyright", query.Filters(since="2026-08-05"), 10, "") == []
    assert query.search(conn, "pyright", query.Filters(until="2026-08-05"), 10, "")[0].session["id"] == SID_A


def test_active_session_is_excluded(conn: sqlite3.Connection) -> None:
    sync(conn)
    assert query.search(conn, "pyright", query.Filters(exclude_id=SID_A), 10, "") == []
    assert [r["id"] for r in query.recent(conn, query.Filters(exclude_id=SID_B), 10)] == [SID_A]


def test_tool_output_matches_are_found_but_hidden_by_default(conn: sqlite3.Connection) -> None:
    sync(conn)
    hits = query.search(conn, "zebrafish", query.Filters(), 10, "")
    assert [h.session["id"] for h in hits] == [SID_A]
    assert hits[0].snippets == []
    event, _ = hits[0].tool_snippets[0]
    assert (event["idx"], event["seq"], event["name"]) == (1, 0, "Bash")
    assert hits[0].tool_only()
    hidden = render.render_hits(hits, "zebrafish", conn, query.Filters(), show_tools=False)
    assert "1 of them mention the words only in tool output, one line each" in hidden
    assert hidden.splitlines()[-1].endswith("  Pyright migration  tool-only: t1#0 Bash command=uv run pytest")
    assert "match(es) in tool output" not in hidden
    shown = render.render_hits(hits, "zebrafish", conn, query.Filters(), show_tools=True)
    assert "tool-only" not in shown
    assert "tool t1#0 Bash:" in shown
    assert "[zebrafish]" in shown


def test_outcome_line(conn: sqlite3.Connection) -> None:
    sync(conn)
    hits = query.search(conn, "timer", query.Filters(), 10, "")
    out = render.render_hits(hits, "timer", conn, query.Filters(), show_tools=False)
    assert "outcome (last reply): Confirmed, the Start New button is shipped." in out


def test_search_prefers_current_directory(conn: sqlite3.Connection) -> None:
    sync(conn)
    hits = query.search(conn, "tests OR button", query.Filters(), 10, "/home/u/leetcode")
    assert hits[0].session["id"] == SID_B


def test_fts_query_sanitises_punctuation() -> None:
    assert query.fts_query('feat/selfhost-3stage "quoted" pyrig*') == '"feat/selfhost-3stage" """quoted""" "pyrig"*'
    assert query.fts_query("a OR b") == '"a" OR "b"'


def test_fts_query_operators_only_upper_case_between_terms() -> None:
    assert query.fts_query("apples OR") == '"apples" "OR"'
    assert query.fts_query("NOT foo") == '"NOT" "foo"'
    assert query.fts_query("or") == '"or"'
    assert query.fts_query("foo AND") == '"foo" "AND"'
    assert query.fts_query("cats or dogs") == '"cats" "or" "dogs"'
    assert query.fts_query("cats OR dogs") == '"cats" OR "dogs"'
    assert query.fts_query("a OR AND b") == '"a" OR "AND" "b"'
    assert query.fts_query("a OR *") == '"a" "OR"'


@pytest.mark.parametrize("text", ["apples OR", "NOT foo", "or", "foo AND", "OR OR", "a OR NOT b", "* OR"])
def test_search_accepts_operator_words_anywhere(conn: sqlite3.Connection, text: str) -> None:
    sync(conn)
    query.search(conn, text, query.Filters(), 10, "")


def test_search_treats_lower_case_operator_words_as_words(conn: sqlite3.Connection) -> None:
    sync(conn)
    assert [h.session["id"] for h in query.search(conn, "timer resume", query.Filters(), 10, "")] == [SID_B]
    assert query.search(conn, "timer not resume", query.Filters(), 10, "") == []
    assert query.search(conn, "timer or pyright", query.Filters(), 10, "") == []
    ids = {h.session["id"] for h in query.search(conn, "timer OR pyright", query.Filters(), 10, "")}
    assert ids == {SID_A, SID_B}


def test_normalise_date_accepts_documented_forms_only() -> None:
    assert query.normalise_date("", end_of_day=False) == ""
    assert query.normalise_date("2026-08-05", end_of_day=False) == "2026-08-05T00:00:00"
    assert query.normalise_date("2026-08-05", end_of_day=True) == "2026-08-05T23:59:59"
    assert query.normalise_date("2026-09-01T10:00:00", end_of_day=True) == "2026-09-01T10:00:00"
    assert query.normalise_date("2026-08-20T09:00:00.000Z", end_of_day=False) == "2026-08-20T09:00:00"
    assert query.normalise_date("2026-08-20T09:00:00+02:00", end_of_day=False) == "2026-08-20T07:00:00"
    assert query.normalise_date("2026-08-20T09:00:00", end_of_day=False) <= "2026-08-20T09:00:00.000Z"
    relative = query.normalise_date("7d", end_of_day=False)
    assert query.ISO_TIMESTAMP.match(relative)
    assert relative < query.normalise_date("6d", end_of_day=False)
    for bad in ("yesterday", "last week", "2026/09/01", "7 days", "7", "2026-09-01T10:00", "2026-09-01 10:00:00"):
        with pytest.raises(ValueError, match=f"unrecognised date {bad!r}.*YYYY-MM-DD.*7d, 2w or 3m.*ISO 8601"):
            query.normalise_date(bad, end_of_day=False)


def test_resolve_prefix_errors(conn: sqlite3.Connection, claude_home: Path) -> None:
    sync(conn)
    with pytest.raises(query.NotFoundError):
        query.resolve_session(conn, "zzzz")
    write_transcript(
        claude_home / "projects" / "-home-u-proj" / "aaaaaaaa-9999-0000-0000-000000000009.jsonl",
        transcript_a("aaaaaaaa-9999-0000-0000-000000000009"),
    )
    sync(conn)
    with pytest.raises(query.AmbiguousError):
        query.resolve_session(conn, "aaaaaaaa")
    assert query.resolve_session(conn, SID_A)["id"] == SID_A


def test_select_turns_specs(conn: sqlite3.Connection) -> None:
    sync(conn)
    rows = query.turns_for(conn, SID_A)
    assert [r["idx"] for r in query.select_turns(rows, "last:1")] == [1]
    assert [r["idx"] for r in query.select_turns(rows, "first:1")] == [0]
    assert [r["idx"] for r in query.select_turns(rows, "0-1")] == [0, 1]
    assert [r["idx"] for r in query.select_turns(rows, "1")] == [1]


def test_render_session_respects_budget(conn: sqlite3.Connection) -> None:
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    header = render.session_header(s, query.files_for(conn, s["id"]), [], [])
    out = render.render_session(header, query.turns_for(conn, s["id"]), 900)
    assert len(out) <= 900
    assert "10:00 [#0-#1 Editx1, Readx1]" in out
    assert "claude: Done: pyright configured" in out


def test_render_turn_with_and_without_tools(conn: sqlite3.Connection) -> None:
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    t = query.turn_at(conn, s["id"], 1)
    plain = render.render_turn(s, t, [], 2000)
    assert "uuid " in plain
    assert f"cite: [aaaaaaaa {t['uuid'][:8]} t1 2026-08-01 fable-5-1]" in plain
    assert "\n\nuser:\n" in plain
    assert "\n\nclaude:\n" in plain
    assert "tools #0: Bashx1 [include_tools=True to show]" in plain
    assert "zebrafish" not in plain
    with_tools = render.render_turn(s, t, query.events_for(conn, s["id"], 1), 2000)
    assert "#0 Bash command=uv run pytest" in with_tools
    assert "zebrafish" in with_tools
    assert len(with_tools) <= 2100
    single = render.render_event(s, t, query.event_at(conn, s["id"], 1, 0), 500)
    assert single.startswith("session aaaaaaaa turn 1 tool call #0: Bash at 2026-08-01")
    assert f"cite: [aaaaaaaa {t['uuid'][:8]} t1#0 tool:Bash 2026-08-01 fable-5-1]" in single
    assert "12 passed" in single
    assert "truncated" not in single
    with pytest.raises(query.NotFoundError):
        query.event_at(conn, s["id"], 1, 7)


def test_files_lookup(conn: sqlite3.Connection) -> None:
    sync(conn)
    rows = query.sessions_touching(conn, "timer.tsx", query.Filters(), 10)
    assert [r["id"] for r in rows] == [SID_B]
    assert "timer.tsx:write" in rows[0]["matched"]


def run_prompt_hook(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict) -> str:
    from logbook.cli import main

    monkeypatch.setattr("sys.argv", ["logbook", "prompt-hook"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    main()
    return capsys.readouterr().out


def test_prompt_hook_injects_once_and_excludes_current(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sync(conn)
    payload = {"cwd": "/home/u", "session_id": SID_B, "prompt": "please migrate this repo to pyright"}
    out = json.loads(run_prompt_hook(monkeypatch, capsys, payload))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert ctx.startswith("logbook: 2 past Claude Code sessions are indexed")
    assert ctx.split("\n")[1].startswith("Most recent sessions, all projects (id, date, directory")
    assert ctx.split("\n")[2].startswith(
        f"- {SID_A[:8]}  2026-08-01  /home/u/proj  (main)  2t  fable-5-1  Pyright migration"
    )
    assert "leetcode" not in ctx
    assert ctx.endswith("earlier attempts may be abandoned or wrong.")
    assert "{" not in ctx
    assert len(ctx) < 600
    logged = json.loads(Path(os.environ["LOGBOOK_LOG"]).read_text().splitlines()[-1])
    assert logged["filters"] == {"source": "prompt-hook"}
    assert logged["query"] == ""
    assert logged["hits"] == 1
    assert logged["listed"] == [SID_A]
    assert logged["chars"] == len(ctx)
    assert run_prompt_hook(monkeypatch, capsys, payload) == ""
    assert run_prompt_hook(monkeypatch, capsys, {**payload, "session_id": "ffffffff-0000-0000-0000-000000000009"}) != ""


@pytest.mark.usefixtures("claude_home")
def test_session_start_hook_only_syncs(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from logbook.cli import main

    monkeypatch.setattr("sys.argv", ["logbook", "hook"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": "/home/u", "session_id": SID_B})))
    main()
    assert capsys.readouterr().out == ""


def test_or_terms_skips_only_genuine_operator_queries() -> None:
    assert query.or_terms("pyright zebrafish") == ["pyright", "zebrafish"]
    assert query.or_terms("pyright") == []
    assert query.or_terms("pyright OR mypy") == []
    assert query.or_terms("pyrig* zebrafish") == ["pyrig*", "zebrafish"]
    assert query.or_terms("why not use pyright") == ["why", "not", "use", "pyright"]
    assert query.or_terms("cats or dogs") == ["cats", "or", "dogs"]
    assert query.or_terms("apples OR") == ["apples", "OR"]
    assert query.or_terms("cats OR dogs") == []


def test_session_start_hook_prunes_old_markers(
    claude_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from logbook.cli import MARKER_MAX_AGE_DAYS, SECONDS_PER_DAY, main

    folder = claude_home.parent / "db" / "prompted"
    folder.mkdir(parents=True)
    old = folder / "old-session"
    fresh = folder / "fresh-session"
    old.touch()
    fresh.touch()
    stale = time.time() - (MARKER_MAX_AGE_DAYS + 1) * SECONDS_PER_DAY
    os.utime(old, (stale, stale))
    monkeypatch.setattr("sys.argv", ["logbook", "hook"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    main()
    assert capsys.readouterr().out == ""
    assert not old.exists()
    assert fresh.exists()
