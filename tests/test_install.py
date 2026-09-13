import io
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from conftest import SID_A, SID_B, write_transcript

from logbook import db, install
from logbook.sync import sync

EXE = "/opt/bin/logbook"


def completed(code: int, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")


def test_merge_hooks_adds_updates_and_is_idempotent() -> None:
    settings: dict = {
        "hooks": {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "afplay x"}]}]}
    }
    assert install.merge_hooks(settings, EXE)
    assert settings["hooks"]["SessionStart"][1]["hooks"][0]["command"] == f"{EXE} hook"
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "afplay x"
    assert settings["hooks"]["UserPromptSubmit"][0]["hooks"][0] == {
        "type": "command",
        "command": f"{EXE} prompt-hook",
        "timeout": 5,
        "statusMessage": "Indexing this session",
    }
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == f"{EXE} index-hook"
    assert settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == f"{EXE} index-hook"
    assert settings["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 30
    assert not install.merge_hooks(settings, EXE)
    assert install.merge_hooks(settings, "/elsewhere/logbook")
    assert settings["hooks"]["SessionStart"][1]["hooks"][0]["command"] == "/elsewhere/logbook hook"
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1
    assert len(settings["hooks"]["Stop"]) == 1
    assert not install.is_ours("/x/logbook serve", "hook")
    assert not install.is_ours("logbook-viewer hook", "hook")


def test_install_hooks_and_assets_write_into_claude_dir(claude_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install.install_hooks(EXE)
    written = json.loads((claude_home / "settings.json").read_text())
    assert written["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == f"{EXE} prompt-hook"
    install.install_assets()
    agent = claude_home / "agents" / "recall.md"
    skill = claude_home / "skills" / "recall" / "SKILL.md"
    assert agent.read_text().startswith("---\nname: recall\n")
    assert "model:" not in agent.read_text().split("---")[1]
    assert skill.read_text().startswith("---\nname: recall\n")
    (claude_home / "agents" / "recall.md").write_text("edited")
    skill.unlink()
    skill.symlink_to(agent)
    capsys.readouterr()
    install.install_assets()
    assert agent.read_text().startswith("---")
    assert skill.is_symlink()
    notes = capsys.readouterr().out.splitlines()
    assert [line.split(": ")[-1] for line in notes] == ["updated", "is or sits under a symlink, left alone"]


def test_install_assets_handles_a_symlinked_skill_directory(
    claude_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "checkout" / "recall"
    checkout.mkdir(parents=True)
    (checkout / "SKILL.md").write_text("dev copy")
    skills = claude_home / "skills"
    skills.mkdir()
    (skills / "recall").symlink_to(checkout)
    install.install_assets()
    assert (skills / "recall" / "SKILL.md").read_text() == "dev copy"
    assert capsys.readouterr().out.splitlines()[-1].endswith("left alone")


def test_register_mcp_skips_replaces_or_adds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(_claude: str, *args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "get":
            return completed(0, f"Command: {EXE}") if state == "same" else completed(0 if state == "other" else 1)
        return completed(0)

    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    state = "same"
    install.register_mcp(EXE, run=fake)
    assert calls == [("get", "logbook")]
    calls.clear()
    state = "other"
    install.register_mcp(EXE, run=fake)
    assert [c[0] for c in calls] == ["get", "remove", "add"]
    assert calls[2] == ("add", "--scope", "user", "logbook", "--", EXE, "serve")
    calls.clear()
    state = "missing"
    install.register_mcp(EXE, run=fake)
    assert [c[0] for c in calls] == ["get", "add"]
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    capsys.readouterr()
    install.register_mcp(EXE, run=fake)
    assert capsys.readouterr().out.startswith("claude is not on PATH")
    with pytest.raises(SystemExit, match="not on PATH"):
        install.executable()


def test_connect_does_not_write_when_schema_is_current(claude_home: Path) -> None:
    path = claude_home.parent / "db" / "history.db"
    writer = db.connect(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO meta (key, value) VALUES ('probe', '1')")
    started = time.monotonic()
    reader = sqlite3.connect(path, timeout=1)
    reader.row_factory = sqlite3.Row
    assert db.schema_version(reader) == db.SCHEMA_VERSION
    second = db.connect(path)
    assert time.monotonic() - started < 1
    assert second.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    writer.rollback()


def test_sync_keeps_other_transcripts_when_one_is_broken(claude_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn = db.connect(claude_home.parent / "db" / "history.db")
    broken = claude_home / "projects" / "-home-u-zzz" / "ffffffff-0000-0000-0000-00000000000f.jsonl"
    write_transcript(broken, [])
    broken.write_text("{not json\n")
    stats = sync(conn)
    assert stats.scanned == 3
    assert stats.parsed == 2
    assert stats.failed == 0
    assert f"logbook: skipping {broken}:1: " in capsys.readouterr().err
    ids = {r["id"] for r in db.connect(claude_home.parent / "db" / "history.db").execute("SELECT id FROM sessions")}
    assert ids == {SID_A, SID_B}


def test_prompt_hook_marks_the_session_only_after_output(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from logbook import cli

    def broken(*_args: object) -> list[sqlite3.Row]:
        raise sqlite3.OperationalError("database is locked")

    sync(conn)
    payload = {"cwd": "/home/u", "session_id": SID_B, "prompt": "anything"}
    original = cli.recent_rows
    monkeypatch.setattr(cli, "recent_rows", broken)
    monkeypatch.setattr("sys.argv", ["logbook", "prompt-hook"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(sqlite3.OperationalError):
        cli.main()
    assert not cli.prompt_marker(SID_B).exists()
    monkeypatch.setattr(cli, "recent_rows", original)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    cli.main()
    assert capsys.readouterr().out.startswith("{")
    assert cli.prompt_marker(SID_B).exists()
