import json
import os
import sys
from pathlib import Path

from .paths import claude_dir


def active_session_id() -> str:
    from_env = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if from_env:
        return from_env
    marker = claude_dir() / "sessions" / f"{os.getppid()}.json"
    return read_marker(marker).get("sessionId", "") if marker.is_file() else ""


def read_marker(marker: Path) -> dict[str, str]:
    try:
        return json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"logbook: skipping presence marker {marker}: {exc}", file=sys.stderr)
        return {}
