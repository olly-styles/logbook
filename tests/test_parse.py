import json
from pathlib import Path

import pytest
from conftest import (
    SID_A,
    SID_B,
    assistant,
    rec,
    text,
    tool_result,
    tool_use,
    transcript_a,
    transcript_b,
    user,
    write_transcript,
)

from logbook.parse import parse_transcript


def test_turns_are_human_prompt_plus_final_reply(tmp_path: Path) -> None:
    path = tmp_path / f"{SID_A}.jsonl"
    write_transcript(path, transcript_a())
    s = parse_transcript(path, SID_A)
    assert s.title == "Pyright migration"
    assert s.ai_title == "Pyright migration"
    assert s.custom_title == ""
    assert s.cwd == "/home/u/proj"
    assert s.git_branch == "main"
    assert s.model == "claude-fable-5-1"
    assert s.started_at.startswith("2026-08-01T10:00:00")
    assert s.ended_at == "2026-08-01T10:05:03.000Z"
    assert [t.user_text for t in s.turns] == [
        "Migrate the project from mypy to pyright please",
        "thanks, also run the tests",
    ]
    assert s.turns[0].assistant_text == "Done: pyright configured in pyproject.toml.\nStrict mode is on."
    assert s.turns[1].assistant_text == "All 12 tests pass."
    assert s.turns[0].tools == {"Read": 1, "Edit": 1}
    assert s.turns[0].files == {"/home/u/proj/pyproject.toml": {"read", "write"}}
    assert s.turns[1].tools == {"Bash": 1}
    assert s.files == {("/home/u/proj/pyproject.toml", "read"): 1, ("/home/u/proj/pyproject.toml", "write"): 1}


def test_tool_events_pair_inputs_with_results(tmp_path: Path) -> None:
    path = tmp_path / f"{SID_A}.jsonl"
    write_transcript(path, transcript_a())
    s = parse_transcript(path, SID_A)
    read, edit = s.turns[0].events
    assert (read.seq, read.name, read.input_summary) == (0, "Read", "path=/home/u/proj/pyproject.toml")
    assert read.result == "ok"
    assert edit.name == "Edit"
    assert edit.result == "ok"
    (bash,) = s.turns[1].events
    assert bash.input_summary == "command=uv run pytest"
    assert bash.result.startswith("===== 12 passed")


def test_subagents_and_write_tool(tmp_path: Path) -> None:
    path = tmp_path / f"{SID_B}.jsonl"
    write_transcript(path, transcript_b())
    s = parse_transcript(path, SID_B)
    assert s.git_branch == "feat/timer"
    assert s.turns[0].files == {"/home/u/leetcode/src/timer.tsx": {"write"}}
    assert s.first_prompt.startswith("Why does the leetcode")
    assert len(s.turns) == 2


def test_partial_last_line_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / f"{SID_A}.jsonl"
    lines = [*transcript_a(), '{"type":"assistant","message":{"content":[{"type":"text","te']
    write_transcript(path, lines, trailing_newline=False)
    s = parse_transcript(path, SID_A)
    assert len(s.turns) == 2


def test_system_reminders_and_images(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_transcript(
        path,
        [
            user(
                "",
                "2026-08-01T10:00:01.000Z",
                "x",
                content=[
                    {"type": "text", "text": "look at this <system-reminder>hidden</system-reminder> screenshot"},
                    {"type": "image", "source": {}},
                ],
            ),
            user("[Request interrupted by user]", "2026-08-01T10:00:02.000Z", "x"),
        ],
    )
    s = parse_transcript(path, "x")
    assert [t.user_text for t in s.turns] == ["look at this  screenshot\n[image]"]


def test_every_indexed_text_field_is_redacted(tmp_path: Path) -> None:
    sid = SID_A
    path = tmp_path / f"{sid}.jsonl"
    write_transcript(
        path,
        [
            rec(type="custom-title", customTitle="Rotate token=abcDEF123456", sessionId=sid),
            user("deploy with password=hunter22x please", "2026-08-01T10:00:00.000Z", sid),
            assistant(
                [
                    text("Using sk-abcdefghijklmnopqrstu now."),
                    tool_use(
                        "Bash", use_id="t1", command="curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz' x"
                    ),
                ],
                "2026-08-01T10:00:01.000Z",
                sid,
                msg_id="m1",
                stop="tool_use",
            ),
            rec(
                type="system",
                subtype="away_summary",
                content="Set api_key=q1w2e3r4t5y6 in prod. Next: verify.",
                timestamp="2026-08-01T10:00:02.000Z",
                sessionId=sid,
            ),
        ],
    )
    s = parse_transcript(path, sid)
    assert s.title == "Rotate token=[REDACTED]"
    assert s.turns[0].user_text == "deploy with password=[REDACTED] please"
    assert s.turns[0].assistant_text == "Using [REDACTED] now."
    assert s.turns[0].events[0].input_summary == "command=curl -H 'Authorization: [REDACTED]' x"
    assert s.away_summary == "Set api_key=[REDACTED] in prod. Next: verify."


COMPACT_SUMMARY = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier work in detail."
)


def compact_summary(ts: str, sid: str) -> str:
    return user(COMPACT_SUMMARY, ts, sid, isCompactSummary=True, isVisibleInTranscriptOnly=True)


def compact_boundary(ts: str, sid: str) -> str:
    return rec(type="system", subtype="compact_boundary", timestamp=ts, sessionId=sid, cwd="/home/u/proj")


def test_compaction_summary_is_not_a_turn(tmp_path: Path) -> None:
    sid = "compact"
    path = tmp_path / f"{sid}.jsonl"
    write_transcript(
        path,
        [
            user("fix the flaky test", "2026-08-01T10:00:00.000Z", sid),
            assistant([text("Fixed the flaky test.")], "2026-08-01T10:00:05.000Z", sid, msg_id="m1"),
            compact_boundary("2026-08-01T10:01:00.000Z", sid),
            compact_summary("2026-08-01T10:01:01.000Z", sid),
            assistant([text("Continuing from the summary.")], "2026-08-01T10:01:05.000Z", sid, msg_id="m2"),
        ],
    )
    s = parse_transcript(path, sid)
    assert len(s.turns) == 1
    assert s.turns[0].user_text == "fix the flaky test"
    assert s.turns[0].assistant_text == "Continuing from the summary."
    assert all(COMPACT_SUMMARY not in t.user_text for t in s.turns)


def test_auto_compaction_mid_turn_keeps_later_tool_calls_on_the_turn(tmp_path: Path) -> None:
    sid = "autocompact"
    path = tmp_path / f"{sid}.jsonl"
    write_transcript(
        path,
        [
            user("fix the flaky test", "2026-08-01T10:00:00.000Z", sid),
            assistant(
                [tool_use("Bash", use_id="t_bash", command="uv run pytest")],
                "2026-08-01T10:00:01.000Z",
                sid,
                msg_id="m1",
                stop="tool_use",
            ),
            tool_result("2026-08-01T10:00:02.000Z", sid, content="1 failed", use_id="t_bash"),
            compact_boundary("2026-08-01T10:01:00.000Z", sid),
            compact_summary("2026-08-01T10:01:01.000Z", sid),
            assistant(
                [tool_use("Edit", use_id="t_edit", file_path="/home/u/proj/t.py", old_string="a", new_string="b")],
                "2026-08-01T10:01:02.000Z",
                sid,
                msg_id="m2",
                stop="tool_use",
            ),
            tool_result("2026-08-01T10:01:03.000Z", sid, use_id="t_edit"),
            assistant([text("Fixed by pinning the seed.")], "2026-08-01T10:01:05.000Z", sid, msg_id="m3"),
        ],
    )
    s = parse_transcript(path, sid)
    assert len(s.turns) == 1
    turn = s.turns[0]
    assert turn.assistant_text == "Fixed by pinning the seed."
    assert turn.tools == {"Bash": 1, "Edit": 1}
    assert turn.files == {"/home/u/proj/t.py": {"write"}}
    assert [e.result for e in turn.events] == ["1 failed", "ok"]


def test_resumed_file_starting_with_summary_keeps_real_first_prompt(tmp_path: Path) -> None:
    sid = "resumed"
    path = tmp_path / f"{sid}.jsonl"
    write_transcript(
        path,
        [
            compact_boundary("2026-08-01T10:01:00.000Z", sid),
            compact_summary("2026-08-01T10:01:01.000Z", sid),
            assistant([text("Continuing from the summary.")], "2026-08-01T10:01:05.000Z", sid, msg_id="m1"),
            user("now add the changelog entry", "2026-08-01T10:02:00.000Z", sid),
            assistant([text("Changelog entry added.")], "2026-08-01T10:02:05.000Z", sid, msg_id="m2"),
        ],
    )
    s = parse_transcript(path, sid)
    assert s.first_prompt == "now add the changelog entry"
    assert [t.assistant_text for t in s.turns] == ["Changelog entry added."]


COMMIT_COMMAND = (
    "<command-name>/commit</command-name>\n<command-message>commit</command-message>\n<command-args></command-args>"
)


def test_slash_command_reply_is_its_own_turn(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_transcript(
        path,
        [
            user("first prompt", "2026-08-01T10:00:00.000Z", "x"),
            assistant([text("reply")], "2026-08-01T10:00:01.000Z", "x", msg_id="m1"),
            user(COMMIT_COMMAND, "2026-08-01T10:01:00.000Z", "x"),
            assistant(
                [
                    text("committed the changes"),
                    tool_use("Bash", use_id="t1", command="git add -A"),
                    tool_use("Bash", use_id="t2", command="git commit"),
                    tool_use("Bash", use_id="t3", command="git log -1"),
                ],
                "2026-08-01T10:01:05.000Z",
                "x",
                msg_id="m2",
            ),
        ],
    )
    s = parse_transcript(path, "x")
    first, command = s.turns
    assert (first.user_text, first.assistant_text, first.tool_calls) == ("first prompt", "reply", 0)
    assert (command.idx, command.user_text, command.assistant_text) == (1, "/commit", "committed the changes")
    assert command.tool_calls == 3
    assert command.ts == "2026-08-01T10:01:00.000Z"
    assert command.uuid == json.loads(user(COMMIT_COMMAND, "2026-08-01T10:01:00.000Z", "x"))["uuid"]


def test_command_args_and_skill_shape(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_transcript(
        path,
        [
            user(
                "<command-name>/effort</command-name>\n<command-message>effort</command-message>\n"
                "<command-args>high please</command-args>",
                "2026-08-01T10:00:00.000Z",
                "x",
            ),
            assistant([text("effort set")], "2026-08-01T10:00:01.000Z", "x", msg_id="m1"),
            user(
                "<command-message>recall</command-message>\n<command-name>/recall</command-name>",
                "2026-08-01T10:01:00.000Z",
                "x",
            ),
            user("skill body injected here", "2026-08-01T10:01:00.100Z", "x", meta=True),
            assistant([text("recalled")], "2026-08-01T10:01:01.000Z", "x", msg_id="m2"),
        ],
    )
    s = parse_transcript(path, "x")
    assert [(t.user_text, t.assistant_text) for t in s.turns] == [
        ("/effort high please", "effort set"),
        ("/recall", "recalled"),
    ]


def test_local_command_output_cancels_the_command_turn(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_transcript(
        path,
        [
            user("<command-name>/clear</command-name>", "2026-08-01T10:00:00.000Z", "x"),
            user("caveat", "2026-08-01T10:00:00.100Z", "x", meta=True),
            user("<local-command-stdout>(no content)</local-command-stdout>", "2026-08-01T10:00:00.200Z", "x"),
            user("real prompt", "2026-08-01T10:00:01.000Z", "x"),
            assistant([text("reply")], "2026-08-01T10:00:02.000Z", "x", msg_id="m1"),
            user(
                "<command-name>/effort</command-name>\n<command-args>high</command-args>",
                "2026-08-01T10:00:03.000Z",
                "x",
            ),
            user("<local-command-stdout>Set effort</local-command-stdout>", "2026-08-01T10:00:03.100Z", "x"),
            assistant([text("still working on the prompt")], "2026-08-01T10:00:04.000Z", "x", msg_id="m2"),
            user("<command-name>/model</command-name>", "2026-08-01T10:00:05.000Z", "x"),
            user("<task-notification>agent done</task-notification>", "2026-08-01T10:00:05.100Z", "x"),
            assistant([text("agent result folded in")], "2026-08-01T10:00:06.000Z", "x", msg_id="m3"),
            user("<command-name>/compact</command-name>", "2026-08-01T10:00:06.500Z", "x"),
            user("[Request interrupted by user]", "2026-08-01T10:00:06.600Z", "x"),
            assistant([text("stopped")], "2026-08-01T10:00:06.700Z", "x", msg_id="m3b"),
            user("next prompt", "2026-08-01T10:00:07.000Z", "x"),
            assistant([text("second reply")], "2026-08-01T10:00:08.000Z", "x", msg_id="m4"),
            user("<command-name>/exit</command-name>", "2026-08-01T10:00:09.000Z", "x"),
        ],
    )
    s = parse_transcript(path, "x")
    assert [(t.idx, t.user_text, t.assistant_text) for t in s.turns] == [
        (0, "real prompt", "stopped"),
        (1, "next prompt", "second reply"),
    ]
    assert s.first_prompt == "real prompt"


def test_task_notification_keeps_the_open_turn(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_transcript(
        path,
        [
            user("run the agent", "2026-08-01T10:00:00.000Z", "x"),
            assistant(
                [text("agent started"), tool_use("Agent", use_id="a1", prompt="go")],
                "2026-08-01T10:00:01.000Z",
                "x",
                msg_id="m1",
            ),
            user("<task-notification>agent a1 finished</task-notification>", "2026-08-01T10:00:30.000Z", "x"),
            assistant(
                [text("agent finished: all good"), tool_use("Read", use_id="r1", file_path="/home/u/proj/out.txt")],
                "2026-08-01T10:00:31.000Z",
                "x",
                msg_id="m2",
            ),
            user("thanks", "2026-08-01T10:01:00.000Z", "x"),
            assistant([text("welcome")], "2026-08-01T10:01:01.000Z", "x", msg_id="m3"),
        ],
    )
    s = parse_transcript(path, "x")
    assert [(t.user_text, t.assistant_text, t.tool_calls) for t in s.turns] == [
        ("run the agent", "agent finished: all good", 2),
        ("thanks", "welcome", 0),
    ]


def test_compaction_summary_keeps_the_open_turn(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    summary = json.loads(user("This session is being continued from a previous one.", "2026-08-01T10:00:03.000Z", "x"))
    write_transcript(
        path,
        [
            user("refactor the parser", "2026-08-01T10:00:00.000Z", "x"),
            assistant([text("starting")], "2026-08-01T10:00:01.000Z", "x", msg_id="m1"),
            assistant(
                [tool_use("Edit", use_id="e1", file_path="/home/u/proj/parse.py", old_string="a", new_string="b")],
                "2026-08-01T10:00:02.000Z",
                "x",
                msg_id="m2",
                stop="tool_use",
            ),
            rec(**summary, isCompactSummary=True, isVisibleInTranscriptOnly=True),
            tool_result("2026-08-01T10:00:04.000Z", "x", use_id="e1"),
            assistant([text("parser refactored")], "2026-08-01T10:00:05.000Z", "x", msg_id="m3"),
        ],
    )
    s = parse_transcript(path, "x")
    (turn,) = s.turns
    assert (turn.user_text, turn.assistant_text, turn.tool_calls) == ("refactor the parser", "parser refactored", 1)
    assert turn.files == {"/home/u/proj/parse.py": {"write"}}
    assert turn.events[0].result == "ok"


def test_toolsearch_and_self_tools_are_skipped_and_uncounted(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_transcript(
        path,
        [
            user("look something up", "2026-08-01T10:00:00.000Z", "x"),
            assistant(
                [
                    tool_use("ToolSearch", use_id="s1", query="select:mcp__logbook__search"),
                    tool_use("mcp__logbook__search", use_id="s2", query_text="canvas_post"),
                    tool_use("Bash", use_id="b1", command="ls"),
                    tool_use("Read", use_id="r1", file_path="/home/u/proj/a.py"),
                ],
                "2026-08-01T10:00:01.000Z",
                "x",
                msg_id="m1",
                stop="tool_use",
            ),
            tool_result("2026-08-01T10:00:02.000Z", "x", content='{"matches": ["mcp__logbook__search"]}', use_id="s1"),
            tool_result("2026-08-01T10:00:03.000Z", "x", content="2 sessions match 'canvas_post'", use_id="s2"),
            tool_result("2026-08-01T10:00:04.000Z", "x", content="a.py", use_id="b1"),
            tool_result("2026-08-01T10:00:05.000Z", "x", content="print(1)", use_id="r1"),
            assistant([text("done")], "2026-08-01T10:00:06.000Z", "x", msg_id="m2"),
        ],
    )
    s = parse_transcript(path, "x")
    (turn,) = s.turns
    assert [(e.seq, e.name, e.result) for e in turn.events] == [(0, "Bash", "a.py"), (1, "Read", "print(1)")]
    assert turn.tool_calls == 2
    assert turn.tools == {"Bash": 1, "Read": 1}
    assert turn.files == {"/home/u/proj/a.py": {"read"}}


STUB = "Async agent launched successfully. (This tool result is internal metadata.)\nagentId: a1 (internal ID)"


def launch(ts: str, sid: str, use_id: str, agent_id: str, model: str, description: str) -> str:
    return tool_result(
        ts,
        sid,
        content=STUB,
        use_id=use_id,
        toolUseResult={
            "status": "async_launched",
            "agentId": agent_id,
            "resolvedModel": model,
            "prompt": "find bandwidth sessions",
            "isAsync": True,
            "description": description,
        },
    )


def notification(result: str, use_id: str = "", task_id: str = "t") -> str:
    body = f"<task-notification>\n<task-id>{task_id}</task-id>\n"
    body += f"<tool-use-id>{use_id}</tool-use-id>\n" if use_id else ""
    body += '<status>completed</status>\n<summary>Agent "x" finished</summary>\n'
    return body + (f"<result>{result}</result>\n" if result else "") + "</task-notification>"


def test_async_agent_report_replaces_the_launch_stub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "x.jsonl"
    long_report = "word " * 5000
    write_transcript(
        path,
        [
            user("look up my bandwidth work", "2026-08-01T10:00:00.000Z", "x"),
            assistant(
                [
                    tool_use(
                        "Agent",
                        use_id="a1",
                        description="Recall Olly's bandwidth work",
                        prompt="find bandwidth sessions",
                        subagent_type="recall",
                    ),
                    tool_use("Agent", use_id="a2", description="Second survey", prompt="survey memory papers"),
                ],
                "2026-08-01T10:00:01.000Z",
                "x",
                msg_id="m1",
                stop="tool_use",
            ),
            launch("2026-08-01T10:00:02.000Z", "x", "a1", "agent-one", "claude-haiku-4-5-20251001", "Recall"),
            launch("2026-08-01T10:00:03.000Z", "x", "a2", "agent-two", "claude-fable-5-1", "Second survey"),
            assistant([text("both launched")], "2026-08-01T10:00:04.000Z", "x", msg_id="m2"),
            user("meanwhile fix the tests", "2026-08-01T10:01:00.000Z", "x"),
            assistant([text("on it")], "2026-08-01T10:01:01.000Z", "x", msg_id="m3"),
            user(
                notification("Found three sessions about bandwidth.", use_id="a1")
                + "\n"
                + notification(long_report, task_id="agent-two")
                + "\n"
                + notification("nobody launched me", use_id="orphan-id")
                + "\n"
                + notification("", use_id="bash-9"),
                "2026-08-01T10:01:30.000Z",
                "x",
            ),
            assistant([text("reports folded in")], "2026-08-01T10:01:31.000Z", "x", msg_id="m4"),
        ],
    )
    s = parse_transcript(path, "x")
    assert [(t.user_text, t.assistant_text) for t in s.turns] == [
        ("look up my bandwidth work", "both launched"),
        ("meanwhile fix the tests", "reports folded in"),
    ]
    first, second = s.turns[0].events
    assert first.input_summary == "description=Recall Olly's bandwidth work prompt=find bandwidth sessions"
    assert first.model == "claude-haiku-4-5-20251001"
    assert (first.result, first.truncated) == ("Found three sessions about bandwidth.", False)
    assert second.input_summary == "description=Second survey prompt=survey memory papers"
    assert second.model == "claude-fable-5-1"
    assert second.result == long_report[:20000]
    assert second.truncated
    assert s.turns[1].events == []
    err = capsys.readouterr().err.splitlines()
    assert err == ["logbook: session x: task notification 'orphan-id' carries a result but matches no Agent"]


def test_sync_agent_result_keeps_its_content_and_gains_the_model(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_transcript(
        path,
        [
            user("run a quick check", "2026-08-01T10:00:00.000Z", "x"),
            assistant(
                [tool_use("Agent", use_id="a1", description="Check", prompt="check it")],
                "2026-08-01T10:00:01.000Z",
                "x",
                msg_id="m1",
                stop="tool_use",
            ),
            tool_result(
                "2026-08-01T10:00:02.000Z",
                "x",
                content="All checks pass.",
                use_id="a1",
                toolUseResult={"status": "completed", "agentId": "sync-one", "resolvedModel": "claude-fable-5"},
            ),
            assistant([text("done")], "2026-08-01T10:00:03.000Z", "x", msg_id="m2"),
        ],
    )
    s = parse_transcript(path, "x")
    (event,) = s.turns[0].events
    assert (event.input_summary, event.model, event.result) == (
        "description=Check prompt=check it",
        "claude-fable-5",
        "All checks pass.",
    )


def test_synthetic_model_does_not_replace_the_real_one(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    write_transcript(
        path,
        [
            user("hello", "2026-08-01T10:00:00.000Z", "x"),
            assistant([text("hi")], "2026-08-01T10:00:01.000Z", "x", msg_id="m1", model="claude-fable-5-1"),
            assistant([text("API error")], "2026-08-01T10:00:02.000Z", "x", msg_id="m2", model="<synthetic>"),
            assistant([text("retrying")], "2026-08-01T10:00:03.000Z", "x", msg_id="m3", model=""),
        ],
    )
    assert parse_transcript(path, "x").model == "claude-fable-5-1"
