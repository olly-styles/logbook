import gzip
import os
import shutil
import sqlite3
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import db
from .parse import parse_transcript
from .paths import archive_dir, private_dir, projects_dir

ARCHIVE_SUFFIX = ".jsonl.gz"
GZIP_TRAILER_BYTES = 4


@dataclass
class SyncStats:
    scanned: int = 0
    parsed: int = 0
    missing: int = 0
    archived: int = 0
    failed: int = 0
    seconds: float = 0.0


@dataclass
class Source:
    session_id: str
    path: Path
    on_disk: bool
    from_archive: bool


def gzip_uncompressed_size(path: Path) -> int:
    if path.stat().st_size < GZIP_TRAILER_BYTES:
        return 0
    with path.open("rb") as f:
        f.seek(-GZIP_TRAILER_BYTES, os.SEEK_END)
        return struct.unpack("<I", f.read(GZIP_TRAILER_BYTES))[0]


def archive_matches(src: Path, dest: Path) -> bool:
    if not dest.is_file():
        return False
    st = src.stat()
    return dest.stat().st_mtime_ns == st.st_mtime_ns and gzip_uncompressed_size(dest) == st.st_size


def archive_copy(src: Path, dest: Path) -> int:
    private_dir(dest.parent)
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    st = src.stat()
    with src.open("rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    tmp.replace(dest)
    return dest.stat().st_size


def scan_projects(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {p.stem: p for p in sorted(root.glob("*/*.jsonl"))}


def scan_archive(archive: Path) -> dict[str, Path]:
    if not archive.is_dir():
        return {}
    return {p.name.removesuffix(ARCHIVE_SUFFIX): p for p in sorted(archive.glob(f"*{ARCHIVE_SUFFIX}"))}


def disk_wins(transcript: Path, archived: Path) -> bool:
    st = transcript.stat()
    return st.st_size >= gzip_uncompressed_size(archived) or st.st_mtime_ns >= archived.stat().st_mtime_ns


def discover(root: Path, archive: Path) -> list[Source]:
    disk = scan_projects(root)
    archived = scan_archive(archive)
    sources = []
    for sid in sorted(set(disk) | set(archived)):
        if sid in disk and (sid not in archived or disk_wins(disk[sid], archived[sid])):
            sources.append(Source(sid, disk[sid], on_disk=True, from_archive=False))
        else:
            sources.append(Source(sid, archived[sid], on_disk=sid in disk, from_archive=True))
    return sources


def ingest(conn: sqlite3.Connection, source: Source, archive: Path, stats: SyncStats) -> None:
    st = source.path.stat()
    prev = conn.execute(
        "SELECT source_path, size, mtime_ns FROM ingest_state WHERE session_id = ?", (source.session_id,)
    ).fetchone()
    unchanged = (
        prev is not None
        and prev["source_path"] == str(source.path)
        and prev["size"] == st.st_size
        and prev["mtime_ns"] == st.st_mtime_ns
    )
    dest = archive / f"{source.session_id}{ARCHIVE_SUFFIX}"
    if unchanged:
        if not source.on_disk:
            db.mark_transcript_missing(conn, source.session_id)
        elif not dest.is_file():
            archived_bytes = archive_copy(source.path, dest)
            stats.archived += 1
            conn.execute(
                "UPDATE ingest_state SET archived_bytes = ? WHERE session_id = ?", (archived_bytes, source.session_id)
            )
        return
    try:
        parsed = parse_transcript(source.path, source.session_id)
    except (EOFError, OSError, UnicodeDecodeError, zlib.error) as exc:
        print(f"logbook: skipping {source.path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        stats.failed += 1
        if not source.on_disk:
            db.mark_transcript_missing(conn, source.session_id)
        return
    if source.from_archive or archive_matches(source.path, dest):
        archived_bytes = dest.stat().st_size
    else:
        archived_bytes = archive_copy(source.path, dest)
        stats.archived += 1
    if parsed.turns:
        db.replace_session(conn, parsed, present=source.on_disk)
        stats.parsed += 1
    if not source.on_disk and prev is not None and prev["source_path"] != str(source.path):
        stats.missing += 1
    conn.execute(
        """INSERT OR REPLACE INTO ingest_state (session_id, source_path, size, mtime_ns, archived_bytes)
           VALUES (?, ?, ?, ?, ?)""",
        (source.session_id, str(source.path), st.st_size, st.st_mtime_ns, archived_bytes),
    )


def index_transcript(conn: sqlite3.Connection, path: Path, session_id: str) -> bool:
    if not path.is_file():
        return False
    st = path.stat()
    parsed = parse_transcript(path, session_id)
    prev = conn.execute("SELECT archived_bytes FROM ingest_state WHERE session_id = ?", (session_id,)).fetchone()
    with conn:
        if parsed.turns:
            db.replace_session(conn, parsed, present=True)
        conn.execute(
            """INSERT OR REPLACE INTO ingest_state (session_id, source_path, size, mtime_ns, archived_bytes)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, str(path), st.st_size, st.st_mtime_ns, prev["archived_bytes"] if prev else 0),
        )
    return bool(parsed.turns)


def archive_transcript(conn: sqlite3.Connection, path: Path, session_id: str) -> bool:
    dest = archive_dir() / f"{session_id}{ARCHIVE_SUFFIX}"
    if not path.is_file() or archive_matches(path, dest):
        return False
    archived_bytes = archive_copy(path, dest)
    with conn:
        conn.execute("UPDATE ingest_state SET archived_bytes = ? WHERE session_id = ?", (archived_bytes, session_id))
    return True


def sync_transcripts(conn: sqlite3.Connection, root: Path, archive: Path, stats: SyncStats) -> None:
    sources = discover(root, archive)
    seen = {s.session_id for s in sources}
    for source in sources:
        stats.scanned += 1
        with conn:
            ingest(conn, source, archive, stats)
    with conn:
        for row in conn.execute("SELECT session_id FROM ingest_state").fetchall():
            if row["session_id"] not in seen:
                db.mark_transcript_missing(conn, row["session_id"])
                conn.execute("DELETE FROM ingest_state WHERE session_id = ?", (row["session_id"],))
                stats.missing += 1


def sync(conn: sqlite3.Connection) -> SyncStats:
    started = time.monotonic()
    stats = SyncStats()
    sync_transcripts(conn, projects_dir(), archive_dir(), stats)
    stats.seconds = time.monotonic() - started
    return stats


def rebuild(conn: sqlite3.Connection) -> SyncStats:
    db.reset(conn)
    return sync(conn)


def optimize_fts(conn: sqlite3.Connection) -> None:
    with conn:
        for table in db.FTS_TABLES:
            conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
