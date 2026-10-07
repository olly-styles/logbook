import gzip
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import (
    SID_A,
    SID_B,
    SID_C,
    assistant,
    rec,
    text,
    tool_result,
    tool_use,
    transcript_a,
    transcript_b,
    transcript_c,
    user,
    write_transcript,
)

from logbook import db, prompts, query, render, server
from logbook.cli import main
from logbook.parse import parse_transcript
from logbook.paths import archive_dir, db_path
from logbook.redact import redact
from logbook.sync import sync

SID_R = "dddddddd-0000-0000-0000-000000000004"
SID_H = "eeeeeeee-0000-0000-0000-000000000005"
SID_S = "ffffffff-0000-0000-0000-000000000006"
SID_T = "99999999-0000-0000-0000-000000000007"


def recap(content: str, ts: str, sid: str, cwd: str = "/home/u/proj") -> str:
    return rec(
        type="system",
        subtype="away_summary",
        content=f"{content} (disable recaps in /config)",
        timestamp=ts,
        sessionId=sid,
        cwd=cwd,
        gitBranch="main",
    )


def with_entrypoint(line: str, entrypoint: str) -> str:
    d = json.loads(line)
    d["entrypoint"] = entrypoint
    return json.dumps(d)


def transcript_recaps(n: int, sid: str = SID_R, early_day: str = "2026-09-01") -> list[str]:
    lines = [user("build the widget dashboard", "2026-09-01T09:00:00.000Z", sid)]
    for i in range(n):
        lines.append(recap(f"Recap number {i} about the widget stage {i}.", f"{early_day}T09:{i:02d}:30.000Z", sid))
        lines.append(user(f"question {i} about widget", f"{early_day}T09:{i:02d}:40.000Z", sid))
        lines.append(
            assistant([text(f"answer {i} on the widget")], f"{early_day}T09:{i:02d}:45.000Z", sid, msg_id=f"r{i}")
        )
    lines.append(recap("Final recap: the widget dashboard shipped. " + "detail " * 60, "2026-09-01T10:00:00.000Z", sid))
    return lines


def transcript_headless_cwd(sid: str) -> list[str]:
    cwd = "/private/tmp/claude-501/scratch"
    return [
        user("headless scratch prompt about zebras", "2026-09-02T09:00:00.000Z", sid, cwd),
        assistant([text("zebras done")], "2026-09-02T09:00:05.000Z", sid, msg_id="h1", cwd=cwd),
    ]


def transcript_sdk(sid: str) -> list[str]:
    return [
        with_entrypoint(user("sdk prompt about zebras", "2026-09-02T10:00:00.000Z", sid), "sdk-cli"),
        assistant([text("zebras via sdk")], "2026-09-02T10:00:05.000Z", sid, msg_id="s1"),
    ]


def transcript_truncation(sid: str = SID_T) -> list[str]:
    long_bash = "bash output line with quokka\n" + "filler " * 2000
    long_fetch = "fetched page mentions quokka\n" + "filler " * 2000
    return [
        user("check the quokka page", "2026-09-03T09:00:00.000Z", sid),
        assistant(
            [tool_use("Bash", use_id="tb", command="cat big.txt")],
            "2026-09-03T09:00:01.000Z",
            sid,
            msg_id="q1",
            stop="tool_use",
        ),
        tool_result("2026-09-03T09:00:02.000Z", sid, content=long_bash, use_id="tb"),
        assistant(
            [tool_use("WebFetch", use_id="tf", url="https://example.com/quokka")],
            "2026-09-03T09:00:03.000Z",
            sid,
            msg_id="q2",
            stop="tool_use",
        ),
        tool_result("2026-09-03T09:00:04.000Z", sid, content=long_fetch, use_id="tf"),
        assistant([text("The quokka page is long.")], "2026-09-03T09:00:05.000Z", sid, msg_id="q3"),
    ]


def transcript_tool_echo(sid: str, word: str) -> list[str]:
    return [
        user("show me the file", "2026-09-04T09:00:00.000Z", sid),
        assistant(
            [tool_use("Bash", use_id="te", command="cat notes.txt")],
            "2026-09-04T09:00:01.000Z",
            sid,
            msg_id="e1",
            stop="tool_use",
        ),
        tool_result("2026-09-04T09:00:02.000Z", sid, content=word, use_id="te"),
        assistant([text("Shown above.")], "2026-09-04T09:00:03.000Z", sid, msg_id="e2"),
    ]


def archive_of(sid: str) -> Path:
    return archive_dir() / f"{sid}.jsonl.gz"


def test_sync_archives_before_indexing(conn: sqlite3.Connection, claude_home: Path) -> None:
    stats = sync(conn)
    assert stats.archived == 2
    gz = archive_of(SID_A)
    assert gz.is_file()
    assert not list(gz.parent.glob("*.tmp"))
    original = (claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl").read_bytes()
    assert gzip.decompress(gz.read_bytes()) == original
    st = query.stats(conn)
    assert st["archived_sessions"] == 2
    assert st["archive_bytes"] == sum(archive_of(s).stat().st_size for s in (SID_A, SID_B))
    assert sync(conn).archived == 0


def test_parse_reads_gzip_transcripts(tmp_path: Path) -> None:
    plain = tmp_path / f"{SID_A}.jsonl"
    write_transcript(plain, transcript_a())
    gz = tmp_path / f"{SID_A}.jsonl.gz"
    gz.write_bytes(gzip.compress(plain.read_bytes()))
    from_gz = parse_transcript(gz, SID_A)
    from_plain = parse_transcript(plain, SID_A)
    assert [t.user_text for t in from_gz.turns] == [t.user_text for t in from_plain.turns]
    assert from_gz.turns[1].events[0].result == from_plain.turns[1].events[0].result


@pytest.mark.parametrize("payload", [b"not gzip at all", b"\x1f\x8b\x08\x00" + b"\xff" * 40])
def test_sync_reports_unreadable_archive_and_continues(
    conn: sqlite3.Connection, claude_home: Path, capsys: pytest.CaptureFixture[str], payload: bytes
) -> None:
    sync(conn)
    (claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl").unlink()
    gz = archive_of(SID_A)
    gz.write_bytes(payload)
    path_b = claude_home / "projects" / "-home-u-leetcode" / f"{SID_B}.jsonl"
    write_transcript(path_b, [*transcript_b(), user("and one more", "2026-08-10T09:20:00.000Z", SID_B)])
    capsys.readouterr()
    stats = sync(conn)
    assert stats.scanned == 2
    assert stats.failed == 1
    assert stats.parsed == 1
    assert stats.missing == 0
    assert f"logbook: skipping {gz}: " in capsys.readouterr().err
    row = conn.execute("SELECT source_path FROM ingest_state WHERE session_id = ?", (SID_A,)).fetchone()
    assert row["source_path"].endswith(f"{SID_A}.jsonl")
    a = query.resolve_session(conn, SID_A)
    assert a["turn_count"] == 2
    assert a["transcript_present"] == 0
    assert query.resolve_session(conn, SID_B)["turn_count"] == 3


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_unreadable_transcript_is_reported_and_not_archived(
    conn: sqlite3.Connection, claude_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl"
    path.chmod(0)
    stats = sync(conn)
    path.chmod(0o644)
    assert stats.failed == 1
    assert stats.parsed == 1
    assert stats.archived == 1
    assert not archive_of(SID_A).exists()
    assert not list(archive_of(SID_A).parent.glob("*.tmp"))
    assert archive_of(SID_B).is_file()
    assert conn.execute("SELECT count(*) FROM ingest_state WHERE session_id = ?", (SID_A,)).fetchone()[0] == 0
    with pytest.raises(query.NotFoundError):
        query.resolve_session(conn, SID_A)
    assert f"logbook: skipping {path}: PermissionError" in capsys.readouterr().err
    stats = sync(conn)
    assert stats.parsed == 1
    assert stats.failed == 0
    assert archive_of(SID_A).is_file()


def test_deleted_transcript_survives_from_archive(conn: sqlite3.Connection, claude_home: Path) -> None:
    sync(conn)
    (claude_home / "projects" / "-home-u-leetcode" / f"{SID_B}.jsonl").unlink()
    stats = sync(conn)
    assert stats.missing == 1
    s = query.resolve_session(conn, SID_B)
    assert s["transcript_present"] == 0
    assert len(query.turns_for(conn, SID_B)) == 2
    assert sync(conn).missing == 0
    assert query.resolve_session(conn, SID_B)["transcript_present"] == 0


def test_schema_bump_rebuilds_from_archive_and_disk(
    conn: sqlite3.Connection, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync(conn)
    (claude_home / "projects" / "-home-u-leetcode" / f"{SID_B}.jsonl").unlink()
    sync(conn)
    conn.close()
    monkeypatch.setattr(db, "SCHEMA_VERSION", "999")
    fresh = db.connect(db_path())
    assert query.stats(fresh)["sessions"] == 0
    stats = sync(fresh)
    assert stats.parsed == 2
    assert stats.archived == 0
    b = query.resolve_session(fresh, SID_B)
    assert b["transcript_present"] == 0
    assert query.turn_at(fresh, SID_B, 0)["user_text"].startswith("Why does the leetcode")
    assert query.event_at(fresh, SID_B, 0, 0)["name"] == "Agent"
    a = query.resolve_session(fresh, SID_A)
    assert a["transcript_present"] == 1


def test_rebuild_command_recreates_index(
    conn: sqlite3.Connection, claude_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sync(conn)
    (claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl").unlink()
    conn.close()
    monkeypatch.setattr("sys.argv", ["logbook", "rebuild"])
    main()
    out = capsys.readouterr().out
    assert "sessions: 2" in out
    assert "transcripts_deleted: 1" in out


def test_recap_trail_indexed_and_rendered(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_R}.jsonl", transcript_recaps(10))
    sync(conn)
    s = query.resolve_session(conn, SID_R)
    recaps = query.recaps_for(conn, SID_R)
    assert len(recaps) == 11
    assert s["summary"].startswith("Final recap: the widget dashboard shipped.")
    assert "(disable recaps" not in recaps[0]["content"]
    assert query.stats(conn)["recaps"] == 11
    trail = render.recap_trail(recaps)
    assert len(trail) == 5
    assert trail[0] == "recap 2026-09-01 09:00: Recap number 0 about the widget stage 0."
    assert trail[2] == '… 7 recaps not shown; read(id, grep="...") searches all'
    assert trail[-1].startswith("recap 2026-09-01 10:00: Final recap")
    assert trail[-1].endswith("…")
    assert len(trail[-1]) <= len("recap 2026-09-01 10:00: ") + render.RECAP_CLIP
    header = render.session_header(s, [], [], recaps)
    assert sum(line.startswith("recap ") for line in header) == 4
    assert len(render.recap_trail(recaps[:4])) == 4
    assert "… " not in "\n".join(render.recap_trail(recaps[:4]))


def test_search_matches_earlier_recaps_and_clips_latest(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_R}.jsonl", transcript_recaps(3))
    sync(conn)
    hits = query.search(conn, "stage", query.Filters(), 10, "")
    assert [h.session["id"] for h in hits] == [SID_R]
    assert hits[0].matched_recaps == 3
    assert len(hits[0].recap_snippets) == 2
    out = render.render_hits(hits, "stage", conn, query.Filters(), show_tools=False)
    assert "    recap 2026-09-01: " not in out
    assert "[stage]" not in out
    write_transcript(
        claude_home / "projects" / "-home-u-proj" / f"{SID_R}.jsonl", transcript_recaps(3, early_day="2026-08-31")
    )
    sync(conn)
    hits = query.search(conn, "stage", query.Filters(), 10, "")
    out = render.render_hits(hits, "stage", conn, query.Filters(), show_tools=False)
    assert sum(line.startswith("    recap 2026-08-31: ") for line in out.splitlines()) == 1
    assert "[stage]" in out
    latest = query.resolve_session(conn, SID_R)["summary"]
    assert len(latest) > render.SEARCH_RECAP_CLIP
    shown = next(line for line in out.splitlines() if line.startswith("    recap: "))
    assert shown == f"    recap: {render.clip(latest, render.SEARCH_RECAP_CLIP)}"
    assert shown.endswith("…")
    row = query.resolve_session(conn, SID_R)
    assert render.clip(latest, render.RECAP_CLIP) in "\n".join(
        render.session_header(row, [], [], query.recaps_for(conn, SID_R))
    )
    listing = render.render_recent(query.recent(conn, query.Filters(), 5), conn, query.Filters())
    assert f"    recap: {render.clip(latest, render.LISTING_RECAP_CLIP)}" in listing


def test_headless_sessions_flagged_and_excluded(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(
        claude_home / "projects" / "-private-tmp-claude-501" / f"{SID_H}.jsonl", transcript_headless_cwd(SID_H)
    )
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_S}.jsonl", transcript_sdk(SID_S))
    sync(conn)
    assert query.resolve_session(conn, SID_H)["headless"] == 1
    assert query.resolve_session(conn, SID_S)["headless"] == 1
    assert query.resolve_session(conn, SID_A)["headless"] == 0
    assert query.stats(conn)["headless_sessions"] == 2
    assert query.search(conn, "zebras", query.Filters(), 10, "") == []
    shown = query.search(conn, "zebras", query.Filters(include_headless=True), 10, "")
    assert {h.session["id"] for h in shown} == {SID_H, SID_S}
    assert {r["id"] for r in query.recent(conn, query.Filters(), 10)} == {SID_A, SID_B}
    assert len(query.recent(conn, query.Filters(include_headless=True), 10)) == 4
    assert "[headless]" in render.session_line(query.resolve_session(conn, SID_S), [])


def test_tool_only_hits_rank_after_text_hits_and_render_one_line(conn: sqlite3.Connection, claude_home: Path) -> None:
    echo_ids = [f"{i}{i}{i}{i}{i}{i}{i}{i}-0000-0000-0000-00000000001{i}" for i in range(1, 5)]
    for sid in echo_ids:
        write_transcript(
            claude_home / "projects" / "-home-u-proj" / f"{sid}.jsonl", transcript_tool_echo(sid, "button")
        )
    sync(conn)
    hits = query.search(conn, "button", query.Filters(), 10, "")
    assert hits[0].session["id"] == SID_B
    assert not hits[0].tool_only()
    assert len(hits) == 1 + query.TOOL_TAIL_LIMIT
    assert all(h.tool_only() for h in hits[1:])
    assert len(query.search(conn, "button", query.Filters(), 1, "")) == 1 + query.TOOL_TAIL_LIMIT
    assert len(query.search(conn, "cat", query.Filters(), 2, "")) == 2
    only_tools = query.search(conn, "cat", query.Filters(), 10, "")
    assert len(only_tools) == len(echo_ids) > query.TOOL_TAIL_LIMIT
    assert all(h.tool_only() for h in only_tools)
    out = render.render_hits(hits, "button", conn, query.Filters(), show_tools=False)
    lines = out.splitlines()
    assert lines[0].startswith(f"{1 + query.TOOL_TAIL_LIMIT} sessions match 'button'")
    assert any(line.startswith("    t") and "[button]" in line for line in lines)
    header = f"{query.TOOL_TAIL_LIMIT} of them mention the words only in tool output, one line each; "
    assert lines[-1 - query.TOOL_TAIL_LIMIT].startswith(header)
    assert all(line.endswith("  show me the file  tool-only: t0#0 Bash command=cat notes.txt") for line in lines[-3:])
    shown = render.render_hits(hits, "button", conn, query.Filters(), show_tools=True)
    assert shown.count("    tool t0#0 Bash: [button]") == query.TOOL_TAIL_LIMIT
    assert "tool-only" not in shown


def test_tool_results_capped_per_tool_and_flagged(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_T}.jsonl", transcript_truncation())
    sync(conn)
    bash = query.event_at(conn, SID_T, 0, 0)
    fetch = query.event_at(conn, SID_T, 0, 1)
    assert bash["truncated"] == 1
    assert len(bash["result"]) == 8000
    assert fetch["truncated"] == 0
    assert len(fetch["result"]) > 8000
    assert query.stats(conn)["truncated_tool_results"] == 1
    s = query.resolve_session(conn, SID_T)
    t = query.turn_at(conn, SID_T, 0)
    note = "[output truncated at 8000 chars when indexed; original was longer]"
    single = render.render_event(s, t, bash, 3000)
    assert single.endswith(note)
    assert len(single) <= 3000
    assert note not in render.render_event(s, t, fetch, 3000)
    assert note in render.render_event_grep(s, t, bash, "quokka", 3000)
    assert note in render.render_turn(s, t, query.events_for(conn, SID_T, 0), 6000)
    hits = query.search(conn, "quokka", query.Filters(), 10, "")
    out = render.render_hits(hits, "quokka", conn, query.Filters(), show_tools=True)
    assert "tool t0#0 Bash (truncated):" in out
    assert "tool t0#1 WebFetch:" in out


def test_redact_key_value_false_positives() -> None:
    assert redact("total_tokens: 15000000") == "total_tokens: 15000000"
    assert redact("auth tokens: refresh failed") == "auth tokens: refresh failed"
    assert redact("api_key=abc123XYZ789") == "api_key=[REDACTED]"
    assert redact("password: hunter2xyz9") == "password: [REDACTED]"
    assert redact("password: shortpw") == "password: shortpw"
    assert redact("token=abcdefghijkl") == "token=abcdefghijkl"


def test_pr_listing_ignores_the_days_window(
    conn: sqlite3.Connection, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_C}.jsonl", transcript_c())
    sync(conn)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    assert "Ship the PR" not in server.search()
    out = server.search(pr="119")
    assert "PR proj#119, proj#118" in out
    assert "Ship the PR" in out


def test_search_orders_by_relevance(conn: sqlite3.Connection, claude_home: Path) -> None:
    weak_sid = "11111111-0000-0000-0000-000000000011"
    strong_sid = "22222222-0000-0000-0000-000000000022"
    for sid, ts, prompt in (
        (weak_sid, "2026-08-25T10:00:00.000Z", "tell me about the narwhal migration"),
        (strong_sid, "2025-09-01T10:00:00.000Z", "tell me about the narwhal migration and the narwhal schema"),
    ):
        write_transcript(
            claude_home / "projects" / "-home-u-proj" / f"{sid}.jsonl",
            [user(prompt, ts, sid), assistant([text("ok")], ts, sid, msg_id="n")],
        )
    sync(conn)
    hits = query.search(conn, "narwhal", query.Filters(), 10, "")
    assert [h.session["id"] for h in hits] == [strong_sid, weak_sid]
    assert hits[0].score < hits[1].score


def test_search_breaks_score_ties_newest_first(conn: sqlite3.Connection, claude_home: Path) -> None:
    old_sid = "11111111-0000-0000-0000-000000000011"
    new_sid = "22222222-0000-0000-0000-000000000022"
    for sid, ts in ((old_sid, "2025-09-01T10:00:00.000Z"), (new_sid, "2026-08-25T10:00:00.000Z")):
        write_transcript(
            claude_home / "projects" / "-home-u-proj" / f"{sid}.jsonl",
            [user("tell me about the narwhal migration", ts, sid), assistant([text("ok")], ts, sid, msg_id="n")],
        )
    sync(conn)
    hits = query.search(conn, "narwhal", query.Filters(), 10, "")
    assert [h.session["id"] for h in hits] == [new_sid, old_sid]
    assert hits[0].score == hits[1].score


def test_cwd_boost_stops_at_path_boundaries(conn: sqlite3.Connection, claude_home: Path) -> None:
    inside = ("33333333-0000-0000-0000-000000000033", "/home/u/my_proj/sub")
    sibling = ("44444444-0000-0000-0000-000000000044", "/home/u/my_proj-old")
    for sid, cwd in (inside, sibling):
        ts = "2026-08-20T10:00:00.000Z"
        write_transcript(
            claude_home / "projects" / f"-{cwd[1:].replace('/', '-')}" / f"{sid}.jsonl",
            [
                user("tell me about the narwhal migration", ts, sid, cwd),
                assistant([text("ok")], ts, sid, msg_id="n", cwd=cwd),
            ],
        )
    sync(conn)
    boosted = query.search(conn, "narwhal", query.Filters(), 10, "/home/u/my_proj/")
    assert [h.session["id"] for h in boosted] == [inside[0], sibling[0]]
    assert boosted[0].score < boosted[1].score
    assert query.under_dir("/home/u/my_proj", "/home/u/my_proj") is True
    assert query.under_dir("/home/u/my_proj-old", "/home/u/my_proj") is False
    assert query.under_dir("/home/u/my_proj", "") is False


def test_search_within_session_returns_more_snippets(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_R}.jsonl", transcript_recaps(6))
    sync(conn)
    hits = query.search(conn, "widget", query.Filters(session_id="dddddddd"), 10, "")
    assert [h.session["id"] for h in hits] == [SID_R]
    assert len(hits[0].snippets) == 7
    assert len(query.search(conn, "widget", query.Filters(), 10, "")[0].snippets) == 2
    assert query.search(conn, "widget", query.Filters(session_id="aaaaaaaa"), 10, "") == []


def test_grep_session_lists_roles_and_addresses(conn: sqlite3.Connection) -> None:
    sync(conn)
    rows = query.grep_session(conn, SID_A, "tests|passed")
    assert [(idx, role) for idx, _, _, role, _ in rows] == [(1, "user"), (1, "claude"), (1, "tool:Bash#0")]
    assert rows[0][1] == query.turn_at(conn, SID_A, 1)["uuid"]
    assert rows[2][4].startswith("===== 12 passed")
    assert query.grep_session(conn, SID_A, "pyright") == [
        (
            0,
            query.turn_at(conn, SID_A, 0)["uuid"],
            "2026-08-01T10:00:01.000Z",
            "user",
            "Migrate the project from mypy to pyright please",
        ),
        (
            0,
            query.turn_at(conn, SID_A, 0)["uuid"],
            "2026-08-01T10:00:01.000Z",
            "claude",
            "Done: pyright configured in pyproject.toml.",
        ),
    ]
    s = query.resolve_session(conn, SID_A)
    out = render.render_session_grep(s, rows, "tests|passed", 6000)
    uuid8 = rows[0][1][:8]
    assert f"t1 {uuid8} 2026-08-01 user: thanks, also run the tests" in out
    assert f"t1 {uuid8} 2026-08-01 tool:Bash#0: ===== 12 passed" in out
    assert "no lines match" in render.render_session_grep(s, [], "zzz", 6000)
    capped = render.render_session_grep(s, rows, "tests|passed", 160)
    assert len(capped) <= 160
    assert capped.endswith("… 3 more in t1; narrow the pattern or pass turns=")


def test_instructions_load_from_package_data() -> None:
    instructions = prompts.load("instructions.txt")
    assert instructions.startswith("Index of past Claude Code sessions")
    assert server.mcp.instructions == instructions
    assert len(instructions) <= 2048, "Claude Code shows at most 2048 chars of an MCP server's instructions"
    assert "Use them unprompted" in instructions
    assert "/recall skill" in instructions
    assert "{total}" in prompts.load("prompt_hook.txt")


def test_prompt_hook_lists_recent_sessions_without_recaps(
    conn: sqlite3.Connection, claude_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_R}.jsonl", transcript_recaps(3))
    sync(conn)
    assert query.resolve_session(conn, SID_R)["summary"].startswith("Final recap")
    monkeypatch.setattr("sys.argv", ["logbook", "prompt-hook"])
    payload = {"cwd": "/nowhere", "session_id": "", "prompt": "another widget dashboard please"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    main()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    lines = ctx.split("\n")
    assert f"- {SID_R[:8]}  2026-09-01  /home/u/proj  (main)  4t  fable-5-1  build the widget dashboard" in lines
    assert "recap" not in ctx.lower()
    assert "widget stage" not in ctx
    assert all(line.startswith(("- ", "Most recent", "No earlier", "Context, not")) for line in lines[2:])
