import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .paths import private_dir

ERROR_CLIP = 200


def log_path() -> Path:
    return Path(os.environ.get("LOGBOOK_LOG", Path.home() / ".logbook" / "queries.jsonl"))


def record(
    tool: str,
    session: str,
    cwd: str,
    query: str,
    filters: dict,
    hits: int,
    ms: float,
    chars: int = 0,
    error: str = "",
    listed: tuple[str, ...] = (),
) -> None:
    row: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": tool,
        "session": session,
        "cwd": cwd,
        "query": query,
        "filters": filters,
        "hits": hits,
        "ms": round(ms),
        "chars": chars,
    }
    if error:
        row["error"] = error[:ERROR_CLIP]
    if listed:
        row["listed"] = list(listed)
    path = log_path()
    private_dir(path.parent)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def logged(tool: str, session: str, cwd: str, query: str, filters: dict, fn: Callable[[], tuple[str, int]]) -> str:
    started = time.monotonic()
    try:
        text, hits = fn()
    except Exception as exc:
        elapsed = (time.monotonic() - started) * 1000
        record(tool, session, cwd, query, filters, 0, elapsed, error=f"{type(exc).__name__}: {exc}")
        raise
    record(tool, session, cwd, query, filters, hits, (time.monotonic() - started) * 1000, chars=len(text))
    return text


def used(**kwargs: object) -> dict:
    return {k: v for k, v in kwargs.items() if v not in ("", None) and v is not False}
