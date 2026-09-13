import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from logbook import db


def rec(**kw: object) -> str:
    return json.dumps(kw)


def user(
    text: str,
    ts: str,
    sid: str,
    cwd: str = "/home/u/proj",
    branch: str = "main",
    *,
    meta: bool = False,
    content: list[dict[str, object]] | None = None,
    **flags: object,
) -> str:
    d = {
        "type": "user",
        "uuid": hashlib.md5(f"{text}{ts}{sid}".encode(), usedforsecurity=False).hexdigest(),
        "message": {"role": "user", "content": text if content is None else content},
        "timestamp": ts,
        "sessionId": sid,
        "cwd": cwd,
        "gitBranch": branch,
        "version": "2.1.263",
        "isSidechain": False,
    }
    if meta:
        d["isMeta"] = True
    d.update(flags)
    return rec(**d)


def tool_result(
    ts: str,
    sid: str,
    cwd: str = "/home/u/proj",
    branch: str = "main",
    content: str = "ok",
    use_id: str = "toolu_1",
    **overrides: object,
) -> str:
    d = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use_id, "content": content}]},
        "toolUseResult": {"ok": True},
        "timestamp": ts,
        "sessionId": sid,
        "cwd": cwd,
        "gitBranch": branch,
        "uuid": f"u-{ts}",
    }
    d.update(overrides)
    return rec(**d)


def assistant(
    blocks: list[dict[str, object]],
    ts: str,
    sid: str,
    msg_id: str = "m1",
    model: str = "claude-fable-5-1",
    stop: str = "end_turn",
    cwd: str = "/home/u/proj",
    branch: str = "main",
) -> str:
    return rec(
        type="assistant",
        message={"model": model, "id": msg_id, "role": "assistant", "content": blocks, "stop_reason": stop},
        timestamp=ts,
        sessionId=sid,
        cwd=cwd,
        gitBranch=branch,
        isSidechain=False,
    )


def text(t: str) -> dict[str, object]:
    return {"type": "text", "text": t}


def tool_use(name: str, use_id: str = "toolu_1", **inp: object) -> dict[str, object]:
    return {"type": "tool_use", "id": use_id, "name": name, "input": inp}


def write_transcript(path: Path, lines: list[str], *, trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    path.write_text(body + ("\n" if trailing_newline else ""))


SID_A = "aaaaaaaa-0000-0000-0000-000000000001"
SID_B = "bbbbbbbb-0000-0000-0000-000000000002"
SID_C = "cccccccc-0000-0000-0000-000000000003"


def transcript_a(sid: str = SID_A) -> list[str]:
    return [
        rec(type="mode", mode="normal", sessionId=sid),
        user("<command-name>/clear</command-name>", "2026-08-01T10:00:00.000Z", sid),
        user("caveat", "2026-08-01T10:00:00.100Z", sid, meta=True),
        user("Migrate the project from mypy to pyright please", "2026-08-01T10:00:01.000Z", sid),
        assistant(
            [text("I'll look at the config first.")], "2026-08-01T10:00:05.000Z", sid, msg_id="m1", stop="tool_use"
        ),
        assistant(
            [tool_use("Read", file_path="/home/u/proj/pyproject.toml")],
            "2026-08-01T10:00:06.000Z",
            sid,
            msg_id="m1",
            stop="tool_use",
        ),
        tool_result("2026-08-01T10:00:07.000Z", sid),
        assistant(
            [
                tool_use(
                    "Edit", file_path="/home/u/proj/pyproject.toml", old_string="a", new_string="b", replace_all=False
                )
            ],
            "2026-08-01T10:00:08.000Z",
            sid,
            msg_id="m2",
            stop="tool_use",
        ),
        tool_result("2026-08-01T10:00:09.000Z", sid),
        assistant([text("Done: pyright configured in pyproject.toml.")], "2026-08-01T10:00:10.000Z", sid, msg_id="m3"),
        assistant([text("Strict mode is on.")], "2026-08-01T10:00:11.000Z", sid, msg_id="m3"),
        rec(type="ai-title", aiTitle="Pyright migration", sessionId=sid),
        user("thanks, also run the tests", "2026-08-01T10:05:00.000Z", sid),
        assistant(
            [tool_use("Bash", use_id="toolu_bash", command="uv run pytest", description="run tests")],
            "2026-08-01T10:05:01.000Z",
            sid,
            msg_id="m4",
            stop="tool_use",
        ),
        tool_result(
            "2026-08-01T10:05:02.000Z",
            sid,
            content="===== 12 passed, 0 failed, warnings: zebrafish deprecation =====",
            use_id="toolu_bash",
        ),
        assistant([text("All 12 tests pass.")], "2026-08-01T10:05:03.000Z", sid, msg_id="m5"),
    ]


def transcript_b(sid: str = SID_B) -> list[str]:
    cwd = "/home/u/leetcode"
    where = {"cwd": cwd, "branch": "feat/timer"}
    return [
        user("Why does the leetcode timer button say resume?", "2026-08-10T09:00:00.000Z", sid, cwd, "feat/timer"),
        assistant(
            [tool_use("Agent", description="look", prompt="find timer code", subagent_type="lean")],
            "2026-08-10T09:00:01.000Z",
            sid,
            msg_id="n1",
            stop="tool_use",
            **where,
        ),
        tool_result("2026-08-10T09:00:02.000Z", sid, **where),
        assistant(
            [tool_use("Write", file_path="/home/u/leetcode/src/timer.tsx", content="x")],
            "2026-08-10T09:00:03.000Z",
            sid,
            msg_id="n2",
            stop="tool_use",
            **where,
        ),
        tool_result("2026-08-10T09:00:04.000Z", sid, **where),
        assistant(
            [text("Added a Start New button next to Resume in timer.tsx.")],
            "2026-08-10T09:00:05.000Z",
            sid,
            msg_id="n3",
            **where,
        ),
        user("perfect, go with that one. booked!!", "2026-08-10T09:10:00.000Z", sid, cwd, "feat/timer"),
        assistant(
            [text("Confirmed, the Start New button is shipped.")], "2026-08-10T09:10:05.000Z", sid, msg_id="n4", **where
        ),
    ]


def transcript_c() -> list[str]:
    cwd = "/home/u/proj"
    branch = "feat/pr"
    where = {"cwd": cwd, "branch": branch}
    bash_output = (
        "created https://github.com/o/proj/pull/119\n"
        "using token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n"
        "password=hunter22\n"
        "line four\n"
        "line five"
    )
    echo_prompt = (
        "here is what search said: 2 sessions match 'auth' (best first). "
        "Use session(id, turns=...) or turn(id, idx) for detail"
    )
    return [
        rec(type="custom-title", customTitle="Ship the PR", sessionId=SID_C),
        rec(type="ai-title", aiTitle="Generated title", sessionId=SID_C),
        rec(
            type="pr-link",
            sessionId=SID_C,
            prNumber=118,
            prUrl="https://github.com/o/proj/pull/118",
            prRepository="o/proj",
            timestamp="2026-08-20T09:00:00.000Z",
        ),
        rec(
            type="pr-link",
            sessionId=SID_C,
            prNumber=119,
            prUrl="https://github.com/o/proj/pull/119",
            prRepository="o/proj",
            timestamp="2026-08-20T10:00:00.000Z",
        ),
        rec(
            type="pr-link",
            sessionId=SID_C,
            prNumber=119,
            prUrl="https://github.com/o/proj/pull/119",
            prRepository="o/proj",
            timestamp="2026-08-20T10:00:30.000Z",
        ),
        user("open a PR for the auth fix", "2026-08-20T10:00:01.000Z", SID_C, cwd, branch),
        assistant(
            [tool_use("mcp__logbook__search", use_id="t_self", query_text="auth fix")],
            "2026-08-20T10:00:02.000Z",
            SID_C,
            msg_id="c1",
            stop="tool_use",
            **where,
        ),
        tool_result(
            "2026-08-20T10:00:03.000Z",
            SID_C,
            content="1 sessions match 'auth fix' (best first).",
            use_id="t_self",
            **where,
        ),
        assistant(
            [tool_use("Read", use_id="t_env", file_path="/home/u/proj/.env")],
            "2026-08-20T10:00:04.000Z",
            SID_C,
            msg_id="c2",
            stop="tool_use",
            **where,
        ),
        tool_result(
            "2026-08-20T10:00:05.000Z",
            SID_C,
            content="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
            use_id="t_env",
            **where,
        ),
        assistant(
            [tool_use("Bash", use_id="t_bash", command="gh pr create")],
            "2026-08-20T10:00:06.000Z",
            SID_C,
            msg_id="c3",
            stop="tool_use",
            **where,
        ),
        tool_result(
            "2026-08-20T10:00:07.000Z",
            SID_C,
            content=bash_output,
            use_id="t_bash",
            **where,
        ),
        assistant([text("PR 119 opened for the auth fix.")], "2026-08-20T10:00:08.000Z", SID_C, msg_id="c4", **where),
        rec(
            type="system",
            subtype="away_summary",
            content="Opened PR 119 for the auth fix; next action is review. (disable recaps in /config)",
            timestamp="2026-08-20T10:05:00.000Z",
            sessionId=SID_C,
            cwd="/home/u/proj",
            gitBranch="feat/pr",
        ),
        user(echo_prompt, "2026-08-20T10:06:00.000Z", SID_C, cwd, branch),
        assistant([text("Noted the echo.")], "2026-08-20T10:06:01.000Z", SID_C, msg_id="c5", **where),
    ]


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude"
    projects = home / "projects"
    write_transcript(projects / "-home-u-proj" / f"{SID_A}.jsonl", transcript_a())
    write_transcript(projects / "-home-u-leetcode" / f"{SID_B}.jsonl", transcript_b())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("LOGBOOK_DB", str(tmp_path / "db" / "history.db"))
    monkeypatch.setenv("LOGBOOK_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setenv("LOGBOOK_LOG", str(tmp_path / "log" / "queries.jsonl"))
    return home


@pytest.fixture
def conn(claude_home: Path) -> sqlite3.Connection:
    return db.connect(claude_home.parent / "db" / "history.db")
