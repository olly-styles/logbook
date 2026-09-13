import os
from pathlib import Path


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def projects_dir() -> Path:
    return claude_dir() / "projects"


def db_path() -> Path:
    return Path(os.environ.get("LOGBOOK_DB", Path.home() / ".logbook" / "history.db"))


def archive_dir() -> Path:
    return Path(os.environ.get("LOGBOOK_ARCHIVE", Path.home() / ".logbook" / "archive"))


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path
