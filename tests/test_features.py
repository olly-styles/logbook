import io
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
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
    transcript_c,
    user,
    write_transcript,
)

from logbook import db, presence, query, render, server
from logbook.cli import main
from logbook.paths import db_path
from logbook.redact import is_sensitive_path, redact
from logbook.sync import sync


@pytest.fixture
def with_c(claude_home: Path, conn: sqlite3.Connection) -> sqlite3.Connection:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_C}.jsonl", transcript_c())
    sync(conn)
    return conn


def test_titles_pr_and_recap(with_c: sqlite3.Connection) -> None:
    s = query.resolve_session(with_c, SID_C)
    assert s["title"] == "Ship the PR"
    prs = query.prs_for(with_c, SID_C)
    assert [(p["repo"], p["number"]) for p in prs] == [("o/proj", 119), ("o/proj", 118)]
    assert s["summary"] == "Opened PR 119 for the auth fix; next action is review."
    assert "(disable recaps" not in s["summary"]
    line = render.session_line(s, prs)
    assert "PR proj#119, proj#118" in line
    assert "Ship the PR" in line
    assert render.pr_lines(prs) == [
        "    pr: o/proj#119 https://github.com/o/proj/pull/119",
        "    pr: o/proj#118 https://github.com/o/proj/pull/118",
    ]


def test_pr_filter_finds_session_in_one_call(with_c: sqlite3.Connection) -> None:
    assert [r["id"] for r in query.recent(with_c, query.Filters(pr="119"), 10)] == [SID_C]
    assert [r["id"] for r in query.recent(with_c, query.Filters(pr="#119"), 10)] == [SID_C]
    assert [r["id"] for r in query.recent(with_c, query.Filters(pr="118"), 10)] == [SID_C]
    assert [r["id"] for r in query.recent(with_c, query.Filters(pr="o/proj"), 10)] == [SID_C]
    assert query.recent(with_c, query.Filters(pr="120"), 10) == []
    hits = query.search(with_c, "auth", query.Filters(pr="119"), 10, "")
    plain = render.render_hits(hits, "auth", with_c, query.Filters(), show_tools=False)
    assert "    pr: " not in plain
    assert "PR proj#119, proj#118" in plain
    by_pr = render.render_hits(hits, "auth", with_c, query.Filters(pr="119"), show_tools=False)
    assert "    pr: o/proj#119 https://github.com/o/proj/pull/119" in by_pr
    assert [r["id"] for r in query.recent(with_c, query.Filters(pr="o/proj#119"), 10)] == [SID_C]
    assert [r["id"] for r in query.recent(with_c, query.Filters(pr="proj#118"), 10)] == [SID_C]
    assert query.recent(with_c, query.Filters(pr="other#119"), 10) == []
    hits = query.search(with_c, "auth", query.Filters(pr="119"), 10, "")
    assert [h.session["id"] for h in hits] == [SID_C]


def test_self_tool_calls_are_not_indexed_or_counted(with_c: sqlite3.Connection) -> None:
    names = [r["name"] for r in with_c.execute("SELECT name FROM tool_events WHERE session_id = ?", (SID_C,))]
    assert "mcp__logbook__search" not in names
    assert names == ["Read", "Bash"]
    turn = query.turn_at(with_c, SID_C, 0)
    assert turn["tool_calls"] == 2
    assert json.loads(turn["tools_json"]) == {"Bash": 1, "Read": 1}
    assert render.tools_tag(turn) == " [#0-#1 Bashx1, Readx1]"
    session = query.resolve_session(with_c, SID_C)
    assert "tools #0-#1: Bashx1, Readx1" in render.turn_header(session, turn, [])
    assert query.turn_at(with_c, SID_C, 1)["user_text"].startswith("here is what search said")


def test_tool_input_matches_are_shown_and_greppable(with_c: sqlite3.Connection) -> None:
    (hit,) = query.search(with_c, "pr create", query.Filters(), 10, "")
    assert hit.session["id"] == SID_C
    event, snippet = hit.tool_snippets[0]
    assert (event["idx"], event["seq"], event["name"]) == (0, 1, "Bash")
    assert "command=gh [pr] [create]" in snippet
    rows = query.grep_session(with_c, SID_C, "gh pr create")
    assert [(idx, role, line) for idx, _, _, role, line in rows] == [(0, "tool:Bash#1 input", "command=gh pr create")]
    session = query.resolve_session(with_c, SID_C)
    turn = query.turn_at(with_c, SID_C, 0)
    events = query.events_for(with_c, SID_C, 0)
    out = render.render_turn_grep(session, turn, events, "gh pr create", 4000)
    assert "tool #1 Bash input: 1 matching line(s)" in out
    assert "tool #1 Bash command=gh pr create: 1 matching" not in out
    both = query.grep_session(with_c, SID_C, "gh pr|pull/119")
    assert [(role, line) for _, _, _, role, line in both] == [
        ("tool:Bash#1", "created https://github.com/o/proj/pull/119"),
    ]
    out = render.render_turn_grep(session, turn, events, "gh pr|pull/119", 4000)
    assert "tool #1 Bash command=gh pr create: 1 matching" in out
    assert "input" not in out


def test_grep_input_lines_are_clipped(with_agent: sqlite3.Connection) -> None:
    s = query.resolve_session(with_agent, SID_AGENT)
    rows = query.grep_session(with_agent, SID_AGENT, "quokka")
    assert [(role, line[:20]) for _, _, _, role, line in rows] == [
        ("tool:Agent#0", "Found the bandwidth "),
        ("tool:Bash#1 input", "command=echo quokka "),
    ]
    out = render.render_session_grep(s, rows, "quokka", 4000)
    input_line = next(line for line in out.split("\n") if "tool:Bash#1 input" in line)
    assert input_line.endswith("…")
    assert len(input_line.split(": ", 1)[1]) == 160
    turn = query.turn_at(with_agent, SID_AGENT, 0)
    events = query.events_for(with_agent, SID_AGENT, 0)
    grep = render.render_turn_grep(s, turn, events, "quokka", 4000)
    assert (
        "tool #0 Agent haiku-4-5 description=Recall bandwidth work prompt=find bandwidth sessions: 1 matching" in grep
    )
    assert "tool #1 Bash input: 1 matching" in grep
    bash_line = next(line for line in grep.split("\n") if line.startswith(">    1: command=echo"))
    assert bash_line.endswith("…")
    assert len(bash_line.split(": ", 1)[1]) == 160


SID_AGENT = "dddddddd-0000-0000-0000-000000000004"


def transcript_agent(sid: str = SID_AGENT) -> list[str]:
    report = "Found the bandwidth work in three sessions: quokka throttling was the fix."
    return [
        user("look up my bandwidth work", "2026-09-05T10:00:00.000Z", sid),
        assistant(
            [tool_use("Agent", use_id="a1", description="Recall bandwidth work", prompt="find bandwidth sessions")],
            "2026-09-05T10:00:01.000Z",
            sid,
            msg_id="m1",
            stop="tool_use",
            model="claude-opus-5-20260301",
        ),
        tool_result(
            "2026-09-05T10:00:02.000Z",
            sid,
            content="Async agent launched successfully.",
            use_id="a1",
            toolUseResult={"status": "async_launched", "agentId": "ag-1", "resolvedModel": "claude-haiku-4-5-20251001"},
        ),
        assistant(
            [tool_use("Bash", use_id="b1", command="echo " + "quokka " * 50)],
            "2026-09-05T10:00:03.000Z",
            sid,
            msg_id="m2",
            stop="tool_use",
            model="claude-opus-5-20260301",
        ),
        tool_result("2026-09-05T10:00:04.000Z", sid, content="ok", use_id="b1"),
        assistant([text("launched")], "2026-09-05T10:00:05.000Z", sid, msg_id="m3", model="claude-opus-5-20260301"),
        user(
            f"<task-notification>\n<task-id>ag-1</task-id>\n<tool-use-id>a1</tool-use-id>\n<result>{report}</result>\n"
            "</task-notification>",
            "2026-09-05T10:00:30.000Z",
            sid,
        ),
        assistant([text("summarised")], "2026-09-05T10:00:31.000Z", sid, msg_id="m4", model="claude-opus-5-20260301"),
    ]


@pytest.fixture
def with_agent(claude_home: Path, conn: sqlite3.Connection) -> sqlite3.Connection:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_AGENT}.jsonl", transcript_agent())
    sync(conn)
    return conn


def test_short_model_forms() -> None:
    assert render.short_model("claude-haiku-4-5-20251001") == "haiku-4-5"
    assert render.short_model("claude-fable-5-1") == "fable-5-1"
    assert render.short_model("z-ai/glm-5.2") == "z-ai/glm-5.2"
    assert render.short_model("x-ai/grok-4.3") == "x-ai/grok-4.3"
    assert render.short_model("") == ""


def test_model_on_session_lines_and_agent_refs(with_agent: sqlite3.Connection) -> None:
    s = query.resolve_session(with_agent, SID_AGENT)
    line = f"{SID_AGENT[:8]}  2026-09-05  /home/u/proj  (main)  1t  opus-5  look up my bandwidth work"
    assert render.session_line(s, []) == line
    (hit,) = query.search(with_agent, "quokka throttling", query.Filters(), 10, "")
    plain = render.render_hits([hit], "quokka throttling", with_agent, query.Filters(), show_tools=False)
    assert "t0#0 Agent haiku-4-5 description=Recall bandwidth work" in plain
    shown = render.render_hits([hit], "quokka throttling", with_agent, query.Filters(), show_tools=True)
    assert "tool t0#0 Agent haiku-4-5: " in shown
    turn = query.turn_at(with_agent, SID_AGENT, 0)
    events = query.events_for(with_agent, SID_AGENT, 0)
    assert "#0 Agent haiku-4-5 description=Recall bandwidth work prompt=find bandwidth sessions" in render.render_turn(
        s, turn, events, 4000
    )
    assert f"cite: [{SID_AGENT[:8]} {turn['uuid'][:8]} t0 2026-09-05 opus-5]" in render.render_turn(s, turn, [], 4000)
    single = render.render_event(s, turn, events[0], 2000)
    assert single.startswith(f"session {SID_AGENT[:8]} turn 0 tool call #0: Agent haiku-4-5 at 2026-09-05")
    assert f"cite: [{SID_AGENT[:8]} {turn['uuid'][:8]} t0#0 tool:Agent 2026-09-05 haiku-4-5]" in single
    assert "input: description=Recall bandwidth work prompt=find bandwidth sessions" in single
    assert "quokka throttling was the fix" in single
    assert "tool #0 Agent haiku-4-5 description=Recall" in render.render_turn_grep(s, turn, events, "quokka", 4000)


def test_sensitive_read_skipped_and_secrets_redacted(with_c: sqlite3.Connection) -> None:
    read_event = query.event_at(with_c, SID_C, 0, 0)
    assert read_event["name"] == "Read"
    assert read_event["result"] == "[not indexed: sensitive path]"
    bash_event = query.event_at(with_c, SID_C, 0, 1)
    assert "ghp_" not in bash_event["result"]
    assert "hunter22" not in bash_event["result"]
    assert "password=[REDACTED]" in bash_event["result"]
    assert "pull/119" in bash_event["result"]
    assert query.search(with_c, "hunter22", query.Filters(), 10, "") == []


def test_redact_patterns() -> None:
    assert redact("key sk-abcdefghijklmnopqrstu end") == "key [REDACTED] end"
    assert redact("postgres://u:p4ssw0rd@host/db") == "postgres://u:[REDACTED]@host/db"
    assert redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz") == "Authorization: [REDACTED]"
    assert redact("AWS_SECRET_ACCESS_KEY: abcdefgh12345678") == "AWS_SECRET_ACCESS_KEY: [REDACTED]"
    assert redact("nothing secret here") == "nothing secret here"
    assert redact('{"password": "hunter22x", "token": "abcDEF123456789"}') == (
        '{"password": "[REDACTED]", "token": "[REDACTED]"}'
    )
    assert redact("Authorization: Basic dXNlcjpwYXNzd29yZA==") == "Authorization: [REDACTED]"
    assert redact("gh auth login --with-token abcDEF123456") == "gh auth login --with-token [REDACTED]"
    assert redact("--token 12345678") == "--token 12345678"
    assert redact("sk_live_abcdefghijklmnopq") == "[REDACTED]"
    assert redact("https://hooks.slack.com/services/T000/B000/XXXXXXXX") == "[REDACTED]"
    assert redact("the api-key-based flow is 12345678") == "the api-key-based flow is 12345678"
    assert is_sensitive_path("/a/.env")
    assert is_sensitive_path("/a/.env.local")
    assert is_sensitive_path("/home/u/.ssh/id_ed25519")
    assert is_sensitive_path("certs/server.pem")
    assert not is_sensitive_path("/a/environment.py")
    assert not is_sensitive_path("/a/keyboard.py")


def test_turn_uuid_anchor_survives_reindex(conn: sqlite3.Connection, claude_home: Path) -> None:
    sync(conn)
    t1 = query.turn_at(conn, SID_A, 1)
    anchor = t1["uuid"][:8]
    assert anchor
    path = claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl"
    lines = transcript_a()
    lines.insert(3, user("an earlier prompt that shifts indices", "2026-08-01T09:59:00.000Z", SID_A))
    lines.insert(4, assistant([text("ok")], "2026-08-01T09:59:01.000Z", SID_A, msg_id="m0"))
    write_transcript(path, lines)
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    sync(conn)
    by_uuid = query.turn_by_uuid(conn, SID_A, anchor)
    assert by_uuid["idx"] == 2
    assert by_uuid["user_text"] == "thanks, also run the tests"
    with pytest.raises(query.NotFoundError):
        query.turn_by_uuid(conn, SID_A, "zzzzzzzz")


def test_grep_returns_matching_lines_with_context(with_c: sqlite3.Connection) -> None:
    s = query.resolve_session(with_c, SID_C)
    e = query.event_at(with_c, SID_C, 0, 1)
    out = render.render_event_grep(s, query.turn_at(with_c, SID_C, 0), e, "pull/119", 2000)
    assert ">    1: created https://github.com/o/proj/pull/119" in out
    assert "line four" not in out
    rows = query.grep_lines("a\nb\nc\nd\ne\nf\ng", "d", context=1)
    assert [(n, m) for n, _, m in rows] == [(3, False), (4, True), (5, False)]
    assert render.render_grep("x", "hello", "zzz", 500) == ""
    t = query.turn_at(with_c, SID_C, 0)
    assert "no lines match /zzz/ in this turn" in render.render_turn_grep(s, t, [], "zzz", 3000)
    assert "user:" not in render.render_turn_grep(s, t, [], "PR 119", 3000)
    turn_out = render.render_turn_grep(s, t, query.events_for(with_c, SID_C, 0), "PR 119", 3000)
    assert ">    1: PR 119 opened for the auth fix." in turn_out
    assert "uuid " in turn_out


def test_render_session_shows_anchor(conn: sqlite3.Connection) -> None:
    sync(conn)
    s = query.resolve_session(conn, SID_B)
    out = render.render_session(render.session_header(s, [], [], []), query.turns_for(conn, s["id"]), 3000)
    first_turn_uuid = query.turn_at(conn, SID_B, 0)["uuid"][:8]
    assert f"t0 {first_turn_uuid} 09:00" in out


def test_render_session_never_exceeds_budget_on_long_sessions(conn: sqlite3.Connection, claude_home: Path) -> None:
    lines = []
    for i in range(60):
        lines.append(user(f"question number {i} " + "x" * 400, f"2026-08-02T10:{i:02d}:00.000Z", SID_A))
        lines.append(
            assistant([text(f"answer {i} " + "y" * 400)], f"2026-08-02T10:{i:02d}:05.000Z", SID_A, msg_id=f"z{i}")
        )
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl", lines)
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    turns = query.turns_for(conn, s["id"])
    out = render.render_session(render.session_header(s, [], [], []), turns, 6000)
    assert len(out) <= 6000
    assert "earlier turns omitted" in out
    assert "t59 " in out


def test_render_turn_clips_to_a_budget_smaller_than_its_header(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(
        claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl",
        [
            user("explain " + "alpha " * 300, "2026-08-02T10:00:00.000Z", SID_A),
            assistant([text("answer " + "beta " * 300)], "2026-08-02T10:00:05.000Z", SID_A, msg_id="z1"),
        ],
    )
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    t = query.turn_at(conn, SID_A, 0)
    tiny = render.render_turn(s, t, [], 200)
    assert len(tiny) <= len(render.turn_header(s, t, [])) + 200
    assert tiny.endswith("…")


def turn_with_events(n: int) -> list[str]:
    lines = [user("run the whole suite file by file", "2026-08-02T10:00:00.000Z", SID_A)]
    for i in range(n):
        lines.append(
            assistant(
                [tool_use("Bash", use_id=f"tb{i}", command=f"uv run pytest tests/test_{i}.py -q " + "-k filler " * 20)],
                f"2026-08-02T10:00:{i:02d}.100Z",
                SID_A,
                msg_id=f"m{i}",
                stop="tool_use",
            )
        )
        lines.append(
            tool_result(
                f"2026-08-02T10:00:{i:02d}.200Z", SID_A, content=f"result {i} " + "passed " * 60, use_id=f"tb{i}"
            )
        )
    lines.append(assistant([text("All green.")], "2026-08-02T10:01:00.000Z", SID_A, msg_id="final"))
    return lines


@pytest.mark.parametrize("max_chars", [2000, server.READ_DEFAULT_MAX_CHARS])
def test_render_turn_with_many_tool_calls_stays_within_max_chars(
    conn: sqlite3.Connection, claude_home: Path, max_chars: int
) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl", turn_with_events(40))
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    t = query.turn_at(conn, SID_A, 0)
    events = query.events_for(conn, SID_A, 0)
    assert len(events) == 40
    out = render.render_turn(s, t, events, max_chars)
    omission = out.splitlines()[-1]
    assert omission.startswith("… ")
    assert "more tool calls not shown" in omission
    shown = sum(line.startswith("#") for line in out.splitlines())
    assert f'read("aaaaaaaa/t0#{shown}") shows one in full' in omission
    assert f"{40 - shown} more tool calls" in omission
    assert shown >= 2
    assert "#0 Bash command=uv run pytest tests/test_0.py" in out
    assert "-> result 0 passed" in out
    assert len(out) <= max_chars


def test_render_turn_with_fitting_tool_calls_shows_them_all(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl", turn_with_events(3))
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    t = query.turn_at(conn, SID_A, 0)
    out = render.render_turn(s, t, query.events_for(conn, SID_A, 0), server.READ_DEFAULT_MAX_CHARS)
    assert "not shown" not in out
    for i in range(3):
        assert f"#{i} Bash command=uv run pytest tests/test_{i}.py -q " in out
        assert f"-> result {i} " + "passed " * 59 + "passed" in out
    assert len(out) <= server.READ_DEFAULT_MAX_CHARS


def heavy_header_transcript(recaps: int, prs: int) -> list[str]:
    lines = [
        rec(
            type="pr-link",
            sessionId=SID_A,
            prNumber=100 + i,
            prUrl=f"https://github.com/o/proj/pull/{100 + i}",
            prRepository="o/proj",
            timestamp=f"2026-08-02T09:{i:02d}:00.000Z",
        )
        for i in range(prs)
    ]
    for i in range(recaps):
        lines.append(user(f"step {i}", f"2026-08-02T10:{i:02d}:00.000Z", SID_A))
        lines.append(assistant([text(f"done step {i}")], f"2026-08-02T10:{i:02d}:05.000Z", SID_A, msg_id=f"z{i}"))
        lines.append(
            rec(
                type="system",
                subtype="away_summary",
                content=f"Recap {i}: " + "progress on the widget dashboard " * 8,
                timestamp=f"2026-08-02T10:{i:02d}:30.000Z",
                sessionId=SID_A,
                cwd="/home/u/proj",
                gitBranch="main",
            )
        )
    return lines


def test_render_session_omits_earlier_turns_to_fit_max_chars(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl", heavy_header_transcript(25, 10))
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    turns = query.turns_for(conn, SID_A)
    recaps = query.recaps_for(conn, SID_A)
    prs = query.prs_for(conn, SID_A)
    assert (len(recaps), len(prs)) == (25, 10)
    header = render.session_header(s, query.files_for(conn, SID_A), prs, recaps)
    assert header[0] == f"session {SID_A}"
    assert header[1].startswith("2026-08-02 10:00 to 2026-08-02 10:24")
    assert header[-1] == f"resume: claude --resume {SID_A}"
    assert sum(line.startswith("pr: ") for line in header) == 8
    assert "+2 more PRs" in header
    assert '… 21 recaps not shown; read(id, grep="...") searches all' in header
    assert sum(line.startswith("recap ") for line in header) == 4
    max_chars = render.lines_len(header) + 160
    out = render.render_session(header, turns, max_chars)
    assert "24 earlier turns omitted to fit max_chars; use turns= to select them." in out
    assert len(out) <= max_chars
    assert "t24 " in out
    roomy = render.render_session(header, turns, 20000)
    assert "earlier turns omitted" not in roomy
    assert len(roomy) <= 20000
    subset = render.view_session(conn, s, "last:3", "", 20000)
    assert "earlier turns omitted" not in subset
    assert [line.split()[0] for line in subset.splitlines() if line.startswith("t2")] == ["t22", "t23", "t24"]


@pytest.mark.usefixtures("with_c")
def test_session_grep_keeps_recap_rows_when_turn_range_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    out = server.read(SID_C[:8], turns="first:1", grep="next action")
    assert "recap 2026-08-20 10:05: Opened PR 119" in out


def at(*hm: int) -> datetime:
    return datetime(2026, 8, 1, *hm, tzinfo=UTC)


def test_today_tag_follows_last_activity(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    monkeypatch.setattr(render, "utcnow", lambda: at(10, 9))
    assert "[last active 3m ago]" in render.session_line(s, [])
    monkeypatch.setattr(render, "utcnow", lambda: at(12, 0))
    assert "[last active 1h ago]" in render.session_line(s, [])
    assert render.minutes_since("") == float("inf")
    assert render.minutes_since("2026-08-01T11:30:00") == 30
    monkeypatch.setattr(render, "utcnow", lambda: datetime(2026, 8, 20, 12, 0, tzinfo=UTC))
    assert "last active" not in render.session_line(s, [])


def test_active_session_id_reads_the_parent_marker(
    claude_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    markers = claude_home / "sessions"
    markers.mkdir()
    marker = markers / f"{os.getppid()}.json"
    marker.write_text('{"sessionId": "half')
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    assert presence.active_session_id() == ""
    assert "skipping presence marker" in capsys.readouterr().err
    marker.write_text(json.dumps({"sessionId": SID_B, "status": "idle"}))
    assert presence.active_session_id() == SID_B
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    assert presence.active_session_id() == SID_A


def test_server_excludes_the_running_session(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    sync(conn)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    listing = server.search(since="3650d")
    assert "leetcode" in listing
    assert "Pyright" not in listing
    assert "No sessions match" in server.search("pyright")


def test_prompt_hook_tags_todays_sessions(
    claude_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = "dddddddd-0000-0000-0000-000000000004"
    write_transcript(
        claude_home / "projects" / "-home-u-proj" / f"{sid}.jsonl",
        [
            user("quick look", "2026-08-01T10:20:00.000Z", sid),
            assistant([text("Looking.")], "2026-08-01T10:20:05.000Z", sid, msg_id="q1"),
        ],
    )
    sync(db.connect(db_path()))
    monkeypatch.setattr("sys.argv", ["logbook", "prompt-hook"])
    payload = {"cwd": "/home/u/proj", "session_id": SID_B, "prompt": "anything"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(render, "utcnow", lambda: datetime(2026, 8, 20, 12, 0, tzinfo=UTC))
    main()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "quick look" not in ctx
    assert "[" not in ctx.split("\n", 2)[2]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**payload, "session_id": SID_A})))
    monkeypatch.setattr(render, "utcnow", lambda: at(10, 22))
    main()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "[last active 1m ago]  quick look" in ctx
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({**payload, "session_id": "eeeeeeee-0000-0000-0000-000000000005"}))
    )
    monkeypatch.setattr(render, "utcnow", lambda: at(13, 45))
    main()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "[last active 3h ago]  quick look" in ctx


def test_session_index_lists_turns_without_replies(conn: sqlite3.Connection, claude_home: Path) -> None:
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{SID_A}.jsonl", heavy_header_transcript(25, 10))
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    roomy = render.view_session(conn, s, "", "", 20000)
    header, index = roomy.split("\n\n")
    assert f"resume: claude --resume {SID_A}" in header
    assert "… 21 recaps not shown" in header
    assert "claude:" not in roomy
    assert "turns not shown" not in roomy
    assert len(index.splitlines()) == 25
    t0 = query.turn_at(conn, SID_A, 0)
    assert index.splitlines()[0].startswith(f"t0 {t0['uuid'][:8]} 10:00 ")
    assert render.clip(t0["user_text"], render.INDEX_TEXT_CLIP) in index
    budget = len(header) + 200
    tight = render.view_session(conn, s, "", "", budget)
    assert len(tight) <= budget
    assert tight.startswith(f"session {SID_A}\n")
    assert f"resume: claude --resume {SID_A}" in tight
    assert "turns not shown …" in tight
    assert "t0 " in tight
    assert "t24 " in tight
    assert "claude:" in render.view_session(conn, s, "last:2", "", 20000)


def test_session_grep_caps_lines_and_prefers_turns_over_recaps(conn: sqlite3.Connection) -> None:
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    uuid = query.turn_at(conn, SID_A, 0)["uuid"]
    recap = (-1, "", "2026-08-01T09:00:00.000Z", "recap", "recap mentions tests")
    turns = [(0, uuid, "2026-08-01T10:00:01.000Z", "claude", f"line {i} about tests") for i in range(70)]
    out = render.render_session_grep(s, [recap, *turns], "tests", 100000)
    lines = out.splitlines()
    assert lines[0].startswith("session aaaaaaaa: 70 line(s) match /tests/")
    assert "recap " not in out
    assert lines[1].endswith("claude: line 10 about tests")
    assert lines[-2].endswith("claude: line 69 about tests")
    assert lines[-1] == "… 10 more in t0; narrow the pattern or pass turns="
    assert len(lines) == 62
    only_recap = render.render_session_grep(s, [recap], "tests", 100000)
    assert "recap 2026-08-01 09:00: recap mentions tests" in only_recap


def test_capped_session_grep_keeps_newest_and_prefers_conversation_over_tools(conn: sqlite3.Connection) -> None:
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    ts = "2026-08-01T10:00:01.000Z"
    rows = []
    for idx in range(40):
        rows.append((idx, f"uuid{idx:04d}", ts, "claude", f"decision {idx} about tests"))
        rows.append((idx, f"uuid{idx:04d}", ts, "tool:Bash#0", f"output {idx} about tests"))
    out = render.render_session_grep(s, rows, "tests", 100000)
    lines = out.splitlines()
    assert len(lines) == 62
    assert sum("claude: decision" in line for line in lines) == 40
    kept_tools = [line for line in lines if "tool:Bash#0" in line]
    assert len(kept_tools) == 20
    assert kept_tools[0].endswith("output 20 about tests")
    assert kept_tools[-1].endswith("output 39 about tests")
    assert lines[1].endswith("claude: decision 0 about tests")
    assert lines[-1] == "… 20 more tool lines in t0-t19; narrow the pattern or pass turns="
    expected = [r[4] for r in rows if r[3] == "claude" or int(r[4].split()[1]) >= 20]
    assert [line.split(": ", 1)[1] for line in lines[1:-1]] == expected


def test_session_grep_capped_by_max_chars_keeps_newest(conn: sqlite3.Connection) -> None:
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    rows = [(idx, f"uuid{idx:04d}", "2026-08-01T10:00:01.000Z", "user", f"note {idx} tests") for idx in range(30)]
    out = render.render_session_grep(s, rows, "tests", 600)
    assert len(out) <= 600
    lines = out.splitlines()
    assert lines[-2].endswith("user: note 29 tests")
    cut = 30 - (len(lines) - 2)
    assert lines[-1] == f"… {cut} more in t0-t{cut - 1}; narrow the pattern or pass turns="
    tools = [
        (idx, f"uuid{idx:04d}", "2026-08-01T10:00:01.000Z", "tool:Bash#0", f"out {idx} tests") for idx in range(30)
    ]
    for limit in range(300, 700, 7):
        tight = render.render_session_grep(s, tools, "tests", limit)
        assert len(tight) <= limit
        assert " more tool lines in t0-t" in tight.splitlines()[-1]


def test_session_grep_marks_cut_recaps(conn: sqlite3.Connection) -> None:
    sync(conn)
    s = query.resolve_session(conn, SID_A)
    recaps = [(-1, "", f"2026-08-01T09:{m:02d}:00.000Z", "recap", f"recap {m} tests") for m in range(65)]
    out = render.render_session_grep(s, recaps, "tests", 100000)
    lines = out.splitlines()
    assert lines[1].endswith("recap 5 tests")
    assert lines[-1] == "… 5 more in recaps; narrow the pattern or pass turns="
