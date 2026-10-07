import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from conftest import SID_A, SID_B, assistant, rec, text, user, write_transcript

from logbook import db, query, server
from logbook.address import parse_address
from logbook.paths import db_path
from logbook.qlog import log_path
from logbook.sync import sync


def test_parse_address_forms() -> None:
    a = parse_address("27c3625f")
    assert (a.session, a.turn, a.seq) == ("27c3625f", "", -1)
    a = parse_address(" 27c3625f/70d158d1#3 ")
    assert (a.session, a.turn, a.seq) == ("27c3625f", "70d158d1", 3)
    a = parse_address("27c3625f/T16")
    assert (a.turn, a.seq) == ("t16", -1)
    a = parse_address("27c3625f-b1ec-449d-8e91-09e7b7390575/t4")
    assert a.session == "27c3625f-b1ec-449d-8e91-09e7b7390575"
    assert a.turn == "t4"
    for bad in (
        "t16 claude",
        "[27c3625f 70d158d1 t16 2026-08-21 claude]",
        "cc://27c3625f/t1",
        "27c3625f t4",
        "zzzzzzzz",
    ):
        with pytest.raises(ValueError, match="no session id"):
            parse_address(bad)


@pytest.fixture
def indexed(conn: sqlite3.Connection, claude_home: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    sync(conn)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    monkeypatch.chdir(claude_home)
    return conn


@pytest.mark.usefixtures("indexed")
def test_search_text_and_listings() -> None:
    out = server.search("pyright")
    assert "aaaaaaaa" in out
    assert "bbbbbbbb" not in out
    assert "    pr: " not in out
    assert "wrote:" not in out
    assert "    files: /home/u/proj/pyproject.toml\n" in server.search("pyright", file="pyproject")
    listing = server.search(since="365d")
    assert "most recent sessions" in listing
    assert "aaaaaaaa" in listing
    assert "bbbbbbbb" in listing
    by_file = server.search(file="timer.tsx")
    assert "touched files matching 'timer.tsx'" in by_file
    assert "bbbbbbbb" in by_file
    assert "aaaaaaaa" not in by_file
    empty_default_window = server.search()
    assert "No indexed sessions" in empty_default_window


def test_tool_filter(indexed: sqlite3.Connection) -> None:
    by_tool = server.search(tool="bash")
    assert "aaaaaaaa" in by_tool
    assert "bbbbbbbb" not in by_tool
    assert "    calls: t1#0 Bash\n" in by_tool + "\n"
    indexed.executemany(
        "INSERT INTO tool_events (session_id, idx, seq, name) VALUES (?, 1, ?, 'Grep')",
        [(SID_A, seq) for seq in range(10, 18)],
    )
    indexed.commit()
    assert "    calls: t1#10 Grep, t1#11 Grep, t1#12 Grep, t1#13 Grep, t1#14 Grep, t1#15 Grep +2" in server.search(
        tool="grep"
    )
    assert "bbbbbbbb" in server.search(tool="Agent")
    assert "No indexed sessions" in server.search(tool="WebFetch")
    with_text = server.search("pyright", tool="Edit")
    assert "aaaaaaaa" in with_text
    assert "    calls: t0#1 Edit" in with_text
    assert "aaaaaaaa" not in server.search("pyright", tool="Write")
    assert "No indexed sessions" in server.search(tool="   ")
    fallback = server.search("pyright zebrafish", tool="Bash")
    assert "match some of the words" in fallback
    assert "    calls: t1#0 Bash" in fallback
    assert "    calls: t1#0 Bash" in server.search(file="pyproject", tool="Bash")


def test_read_levels(indexed: sqlite3.Connection) -> None:
    session_view = server.read(SID_A[:8])
    assert "resume: claude --resume " + SID_A in session_view
    assert "t0 " in session_view
    assert "Migrate the project from mypy to pyright please" in session_view
    assert "claude:" not in session_view
    turn_uuid = query.turn_at(indexed, SID_A, 1)["uuid"][:8]
    turn_view = server.read(f"{SID_A[:8]}/{turn_uuid}")
    assert "cite: [aaaaaaaa " + turn_uuid + " t1 2026-08-01 fable-5-1]" in turn_view
    assert "thanks, also run the tests" in turn_view
    by_index = server.read(f"{SID_A[:8]}/t1")
    assert by_index == turn_view
    grep_view = server.read(f"aaaaaaaa/{turn_uuid}", grep="12 tests")
    assert "> " in grep_view
    assert "All 12 tests pass." in grep_view
    tool_view = server.read(f"{SID_A[:8]}/{turn_uuid}#0")
    assert "tool call #0: Bash" in tool_view
    assert "12 passed" in tool_view
    tool_grep = server.read(f"{SID_A[:8]}/t1", seq=0, grep="zebrafish")
    assert "zebrafish" in tool_grep
    session_grep = server.read(SID_A[:8], grep="pyright")
    assert "t0 " in session_grep
    assert "user:" in session_grep


@pytest.mark.usefixtures("indexed")
def test_read_errors() -> None:
    with pytest.raises(ValueError, match="no session id"):
        server.read("zzzzzzzz")
    with pytest.raises(query.NotFoundError):
        server.read(f"{SID_B[:8]}/t9")


def read_log() -> list[dict]:
    return [json.loads(line) for line in log_path().read_text().splitlines() if line.strip()]


@pytest.mark.usefixtures("indexed")
def test_query_log_records_search_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cccccccc-0000-0000-0000-000000000003")
    server.search("pyright", project="proj", include_tools=True)
    server.search(since="365d")
    server.read(SID_A[:8], grep="pyright")
    server.read(f"{SID_A[:8]}/t1", seq=0)
    rows = read_log()
    assert [r["tool"] for r in rows] == ["search", "search", "read", "read"]
    assert all(r["session"] == "cccccccc-0000-0000-0000-000000000003" for r in rows)
    assert all(r["ts"].startswith("20") and r["cwd"] and r["ms"] >= 0 for r in rows)
    text_search, listing, session_grep, tool_read = rows
    assert text_search["query"] == "pyright"
    assert text_search["filters"] == {"project": "proj", "include_tools": True}
    assert text_search["hits"] == 1
    assert listing["query"] == ""
    assert listing["filters"] == {"since": "365d"}
    assert listing["hits"] == 2
    assert session_grep["query"] == SID_A[:8]
    assert session_grep["filters"] == {"grep": "pyright"}
    assert session_grep["hits"] == 1
    assert tool_read["query"] == f"{SID_A[:8]}/t1"
    assert tool_read["filters"] == {"seq": 0}
    assert "error" not in tool_read


@pytest.mark.usefixtures("indexed")
def test_query_log_records_failed_reads() -> None:
    with pytest.raises(query.NotFoundError):
        server.read(f"{SID_B[:8]}/t9")
    with pytest.raises(ValueError, match="no session id in address ''"):
        server.read("")
    rows = read_log()
    assert [r["hits"] for r in rows] == [0, 0]
    assert rows[0]["query"] == f"{SID_B[:8]}/t9"
    assert rows[0]["error"].startswith("NotFoundError: session bbbbbbbb has no turn 9")
    assert rows[1]["query"] == ""
    assert rows[1]["error"].startswith("ValueError: no session id in address ''")


@pytest.mark.usefixtures("indexed")
def test_bad_since_is_an_error_not_an_empty_listing() -> None:
    with pytest.raises(ValueError, match="unrecognised date 'yesterday'"):
        server.search(since="yesterday")
    with pytest.raises(ValueError, match="unrecognised date '2026/09/01'"):
        server.search("pyright", until="2026/09/01")
    rows = read_log()
    assert [r["hits"] for r in rows] == [0, 0]
    assert rows[0]["filters"] == {"since": "yesterday"}
    assert rows[0]["error"].startswith("ValueError: unrecognised date 'yesterday'")
    assert rows[1]["error"].startswith("ValueError: unrecognised date '2026/09/01'")


def test_all_digit_turn_anchor_resolves_as_uuid(conn: sqlite3.Connection, claude_home: Path) -> None:
    sid = "dddddddd-0000-0000-0000-000000000004"
    lines = [
        json.loads(
            rec(
                type="user",
                message={"role": "user", "content": "first"},
                timestamp="2026-09-01T10:00:00.000Z",
                sessionId=sid,
                cwd="/home/u/proj",
                uuid="11111111-aaaa-4000-8000-000000000001",
            )
        ),
        json.loads(assistant([text("one")], "2026-09-01T10:00:01.000Z", sid, msg_id="a1")),
        json.loads(
            rec(
                type="user",
                message={"role": "user", "content": "second"},
                timestamp="2026-09-01T10:01:00.000Z",
                sessionId=sid,
                cwd="/home/u/proj",
                uuid="44730569-bbbb-4000-8000-000000000002",
            )
        ),
        json.loads(assistant([text("two")], "2026-09-01T10:01:01.000Z", sid, msg_id="a2")),
    ]
    write_transcript(claude_home / "projects" / "-home-u-proj" / f"{sid}.jsonl", [json.dumps(x) for x in lines])
    sync(conn)
    assert query.turn_by_ref(conn, sid, "44730569")["idx"] == 1
    assert query.turn_by_ref(conn, sid, "11111111")["idx"] == 0
    assert query.turn_by_ref(conn, sid, "1")["idx"] == 1
    assert query.turn_by_ref(conn, sid, "t0")["idx"] == 0
    with pytest.raises(query.NotFoundError):
        query.turn_by_ref(conn, sid, "99999999")


@pytest.mark.usefixtures("indexed")
def test_search_falls_back_to_or_and_says_so() -> None:
    out = server.search("pyright zebrafish quokka")
    assert out.startswith(
        "No session matches all of 'pyright zebrafish quokka' in one place. 1 match some of the words"
    )
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("aaaaaaaa  2026-08-01")
    assert lines[1].endswith("matched: pyright, zebrafish")
    assert "recap:" not in out
    assert "pr:" not in out
    row = json.loads(log_path().read_text().splitlines()[-1])
    assert row["filters"]["or_fallback"] == "pyright OR zebrafish OR quokka"
    assert row["hits"] == 1
    ranked = server.search("leetcode pyright zebrafish").splitlines()
    assert len(ranked) == 2
    assert ranked[1].startswith("aaaaaaaa")
    assert ranked[1].endswith("matched: pyright, zebrafish")
    miss = server.search("quokka wombat")
    assert miss.startswith("No sessions match 'quokka wombat' or two or more of its words.")
    assert server.search("leetcode wombat").startswith("No sessions match 'leetcode wombat' or two or more")
    assert server.search("pyright OR wombat").startswith("1 sessions match")
    phrase = server.search('"mypy to pyright" zebrafish quokka').splitlines()
    assert phrase[1].endswith('matched: "mypy to pyright", zebrafish')
    assert server.search('"pyright mypy" zebrafish quokka').startswith("No sessions match")


@pytest.mark.usefixtures("indexed")
def test_search_gives_identical_results_under_concurrent_calls() -> None:
    expected = server.search("pyright", limit=3)
    threads = 6
    calls_per_thread = 5
    gate = threading.Barrier(threads)

    def worker() -> list[str]:
        gate.wait()
        return [server.search("pyright", limit=3) for _ in range(calls_per_thread)]

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = [r for f in [pool.submit(worker) for _ in range(threads)] for r in f.result()]
    assert len(results) == threads * calls_per_thread
    assert all(r == expected for r in results)


@pytest.mark.usefixtures("claude_home")
def test_racing_first_connects_on_fresh_db_do_not_error() -> None:
    assert not db_path().exists()
    threads = 6
    gate = threading.Barrier(threads)

    def worker() -> str:
        gate.wait()
        with closing(server.conn()) as c:
            return db.schema_version(c)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        versions = [f.result() for f in [pool.submit(worker) for _ in range(threads)]]
    assert versions == [db.SCHEMA_VERSION] * threads


def test_listing_honours_explicit_limit_and_defaults_to_eight(conn: sqlite3.Connection, claude_home: Path) -> None:
    for i in range(3, 20):
        sid = f"cccccccc-0000-0000-0000-{i:012d}"
        lines = [user(f"padding prompt {i}", f"2026-08-{(i % 28) + 1:02d}T10:00:00.000Z", sid)]
        write_transcript(claude_home / "projects" / "-home-u-proj" / f"{sid}.jsonl", lines)
    sync(conn)
    assert server.search(since="365d", limit=3).startswith("3 most recent sessions")
    assert server.search(since="365d").startswith(f"{server.SEARCH_DEFAULT_LIMIT} most recent sessions")


@pytest.mark.usefixtures("indexed")
@pytest.mark.parametrize("limit", [0, -1])
def test_search_and_listing_reject_limit_below_one(limit: int) -> None:
    with pytest.raises(ValueError, match=f"limit={limit} must be at least 1"):
        server.search("pyright", limit=limit)
    with pytest.raises(ValueError, match=f"limit={limit} must be at least 1"):
        server.search(since="365d", limit=limit)
    rows = read_log()
    assert [r["hits"] for r in rows] == [0, 0]
    assert all(r["error"] == f"ValueError: limit={limit} must be at least 1" for r in rows)


@pytest.mark.usefixtures("indexed")
def test_read_rejects_seq_without_turn_and_max_chars_below_one() -> None:
    with pytest.raises(ValueError, match=f"seq=3 needs a turn: use {SID_A[:8]}/t<idx>#3"):
        server.read(SID_A[:8], seq=3)
    with pytest.raises(ValueError, match="max_chars=0 must be at least 1"):
        server.read(SID_A[:8], max_chars=0)
