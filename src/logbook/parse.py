import gzip
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .redact import is_sensitive_path, redact

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read"}
PATH_KEYS = ("file_path", "notebook_path")
TAG_START = re.compile(r"^<[a-zA-Z][\w-]*[\s>/]")
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
COMMAND_START = ("<command-name>", "<command-message>")
COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
INTERRUPTED = "[Request interrupted"
RESULT_CAP = 8000
LONG_RESULT_CAP = 20000
LONG_RESULT_TOOLS = {"WebFetch", "WebSearch", "Agent"}
INPUT_CAP = 1500
HEADLESS_CWD = re.compile(r"^(/private)?/tmp/claude-")
INTERACTIVE_ENTRYPOINTS = {"cli", "claude-desktop"}
RECAP_SUFFIX = "(disable recaps in /config)"
SELF_TOOL_PREFIXES = ("mcp__logbook__", "mcp__corroborator__", "mcp__cc-history__")
SKIP_TOOLS = frozenset({"ToolSearch"})
SENSITIVE_PLACEHOLDER = "[not indexed: sensitive path]"
AGENT_TOOL = "Agent"
AGENT_INPUT_KEYS = ("description", "prompt")
SYNTHETIC_MODEL = "<synthetic>"
TASK_NOTIFICATION_START = "<task-notification>"
TASK_NOTIFICATION = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
TASK_ID = re.compile(r"<task-id>(.*?)</task-id>", re.DOTALL)
TOOL_USE_ID = re.compile(r"<tool-use-id>(.*?)</tool-use-id>", re.DOTALL)
TASK_RESULT = re.compile(r"<result>(.*?)</result>", re.DOTALL)


@dataclass
class ToolEvent:
    seq: int
    name: str
    input_summary: str
    result: str = ""
    truncated: bool = False
    model: str = ""


@dataclass
class Turn:
    idx: int
    ts: str
    user_text: str
    uuid: str = ""
    assistant_text: str = ""
    tools: Counter = field(default_factory=Counter)
    files: dict[str, set[str]] = field(default_factory=dict)
    events: list[ToolEvent] = field(default_factory=list)

    @property
    def tool_calls(self) -> int:
        return len(self.events)


@dataclass
class ParsedSession:
    id: str
    cwd: str = ""
    git_branch: str = ""
    ai_title: str = ""
    custom_title: str = ""
    started_at: str = ""
    ended_at: str = ""
    model: str = ""
    prs: dict[tuple[str, int], str] = field(default_factory=dict)
    recaps: dict[str, str] = field(default_factory=dict)
    sdk_entrypoint: bool = False
    turns: list[Turn] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.custom_title or self.ai_title

    @property
    def away_summary(self) -> str:
        return next(reversed(self.recaps.values()), "")

    @property
    def headless(self) -> bool:
        return self.sdk_entrypoint or HEADLESS_CWD.match(self.cwd) is not None

    @property
    def first_prompt(self) -> str:
        return self.turns[0].user_text if self.turns else ""

    @property
    def files(self) -> dict[tuple[str, str], int]:
        counts: Counter[tuple[str, str]] = Counter()
        for t in self.turns:
            for path, actions in t.files.items():
                counts.update((path, action) for action in actions)
        return counts


def read_transcript_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" and data else data


def iter_records(path: Path) -> Iterator[dict]:
    data = read_transcript_bytes(path)
    if not data:
        return
    lines = data.split(b"\n")
    if not data.endswith(b"\n"):
        lines = lines[:-1]
    for lineno, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"logbook: skipping {path}:{lineno}: {exc}", file=sys.stderr)
            continue
        if not isinstance(record, dict):
            print(
                f"logbook: skipping {path}:{lineno}: expected a JSON object, got {type(record).__name__}",
                file=sys.stderr,
            )
            continue
        yield record


def blocks_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(block.get("text", ""))
            elif kind == "image":
                parts.append("[image]")
            elif kind == "document":
                parts.append("[document]")
        return "\n".join(parts)
    return ""


def prompt_text(record: dict) -> str:
    if record.get("isMeta") or record.get("toolUseResult"):
        return ""
    if record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
        return ""
    content = record.get("message", {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return ""
    return SYSTEM_REMINDER.sub("", blocks_text(content)).strip()


def human_text(record: dict) -> str:
    text = prompt_text(record)
    if not text or text.startswith(INTERRUPTED) or TAG_START.match(text):
        return ""
    return redact(text)


def command_text(record: dict) -> str:
    text = prompt_text(record)
    name = COMMAND_NAME.search(text)
    if not text.startswith(COMMAND_START) or name is None:
        return ""
    args = COMMAND_ARGS.search(text)
    command = name.group(1).strip()
    if args is not None and args.group(1).strip():
        command = f"{command} {args.group(1).strip()}"
    return redact(command)


def tool_path(inp: dict) -> str:
    for key in PATH_KEYS:
        if isinstance(inp.get(key), str):
            return inp[key]
    return ""


def agent_summary(inp: dict) -> str:
    parts = [
        f"{key}={inp[key].strip()}" for key in AGENT_INPUT_KEYS if isinstance(inp.get(key), str) and inp[key].strip()
    ]
    return redact(" ".join(parts)[:INPUT_CAP]) if parts else input_summary(inp)


def input_summary(inp: dict) -> str:
    for key in ("command", "url", "query", "query_text", "pattern", "description", "prompt", "sql", "code"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return redact(f"{key}={value.strip()[:INPUT_CAP]}")
    path_str = tool_path(inp)
    if path_str:
        return f"path={path_str}"
    return redact(json.dumps(inp, sort_keys=True)[:INPUT_CAP])


def result_cap(tool_name: str) -> int:
    if tool_name in LONG_RESULT_TOOLS or "fetch" in tool_name.lower():
        return LONG_RESULT_CAP
    return RESULT_CAP


def result_text(record: dict, cap: int) -> tuple[str, bool]:
    content = record.get("message", {}).get("content")
    text = ""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = blocks_text(block.get("content", ""))
                break
    if not text.strip():
        raw = record.get("toolUseResult")
        text = raw if isinstance(raw, str) else json.dumps(raw) if raw else ""
    return capped(text, cap)


def capped(text: str, cap: int) -> tuple[str, bool]:
    return redact(text[:cap]), len(text) > cap


def tag_text(pattern: re.Pattern[str], body: str) -> str:
    m = pattern.search(body)
    return m.group(1).strip() if m else ""


def tool_result_use_id(rec: dict) -> str:
    content = rec.get("message", {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return block.get("tool_use_id", "")
    return ""


class TranscriptParser:
    def __init__(self, session_id: str) -> None:
        self.session = ParsedSession(id=session_id)
        self.current: Turn | None = None
        self.pending_command: Turn | None = None
        self.last_msg_id = ""
        self.pending: dict[str, ToolEvent] = {}
        self.sensitive: set[str] = set()
        self.agents: dict[str, ToolEvent] = {}
        self.unmatched: set[str] = set()

    def feed(self, rec: dict) -> None:
        kind = rec.get("type")
        if kind == "ai-title":
            self.session.ai_title = redact(rec.get("aiTitle", "")) or self.session.ai_title
            return
        if kind == "custom-title":
            self.session.custom_title = redact(rec.get("customTitle", "")) or self.session.custom_title
            return
        if kind == "pr-link":
            number = int(rec.get("prNumber", 0) or 0)
            if number:
                self.session.prs[(rec.get("prRepository", ""), number)] = rec.get("prUrl", "")
            return
        if rec.get("isSidechain"):
            return
        self.note_metadata(rec)
        if kind == "system":
            self.on_system(rec)
        elif kind == "user":
            self.on_user(rec)
        elif kind == "assistant":
            self.on_assistant(rec.get("message", {}))

    def note_metadata(self, rec: dict) -> None:
        ts = rec.get("timestamp", "")
        if ts:
            if not self.session.started_at:
                self.session.started_at = ts
            self.session.ended_at = ts
        if rec.get("cwd"):
            self.session.cwd = rec["cwd"]
        branch = rec.get("gitBranch", "")
        if branch and branch != "HEAD":
            self.session.git_branch = branch
        entrypoint = rec.get("entrypoint", "")
        if entrypoint and entrypoint not in INTERACTIVE_ENTRYPOINTS:
            self.session.sdk_entrypoint = True

    def on_system(self, rec: dict) -> None:
        if rec.get("subtype") == "away_summary":
            content = rec.get("content", "").replace(RECAP_SUFFIX, "").strip()
            if content:
                self.session.recaps[rec.get("timestamp", "")] = redact(content)

    def on_user(self, rec: dict) -> None:
        if rec.get("toolUseResult"):
            use_id = tool_result_use_id(rec)
            event = self.pending.pop(use_id, None)
            if event is None:
                return
            if use_id in self.sensitive:
                event.result = SENSITIVE_PLACEHOLDER
            else:
                event.result, event.truncated = result_text(rec, result_cap(event.name))
            self.note_launch(event, rec["toolUseResult"])
            return
        if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly"):
            return
        text = prompt_text(rec)
        if text.lstrip().startswith(TASK_NOTIFICATION_START):
            self.on_notification(text)
            self.pending_command = None
            return
        text = human_text(rec)
        if text:
            self.start_turn(self.new_turn(rec, text))
            return
        command = command_text(rec)
        self.pending_command = self.new_turn(rec, command) if command else None

    def note_launch(self, event: ToolEvent, raw: object) -> None:
        if event.name != AGENT_TOOL or not isinstance(raw, dict):
            return
        if isinstance(raw.get("resolvedModel"), str):
            event.model = raw["resolvedModel"]
        if isinstance(raw.get("agentId"), str) and raw["agentId"]:
            self.agents[raw["agentId"]] = event

    def on_notification(self, text: str) -> None:
        for body in TASK_NOTIFICATION.findall(text):
            report = tag_text(TASK_RESULT, body)
            if not report:
                continue
            task_id = tag_text(TASK_ID, body)
            key = tag_text(TOOL_USE_ID, body) or task_id
            event = self.agents.get(key) or self.agents.get(task_id)
            if event is not None:
                event.result, event.truncated = capped(report, result_cap(event.name))
            elif key not in self.unmatched:
                self.unmatched.add(key)
                print(
                    f"logbook: session {self.session.id}: task notification {key!r} carries a result "
                    "but matches no Agent",
                    file=sys.stderr,
                )

    def new_turn(self, rec: dict, user_text: str) -> Turn:
        return Turn(
            idx=len(self.session.turns), ts=rec.get("timestamp", ""), user_text=user_text, uuid=rec.get("uuid", "")
        )

    def start_turn(self, turn: Turn) -> None:
        self.current = turn
        self.pending_command = None
        self.session.turns.append(turn)
        self.last_msg_id = ""
        self.pending = {}
        self.sensitive = set()

    def on_assistant(self, message: dict) -> None:
        if self.pending_command is not None:
            self.start_turn(self.pending_command)
        turn = self.current
        if turn is None:
            return
        model = message.get("model", "")
        if model and model != SYNTHETIC_MODEL:
            self.session.model = model
        texts = []
        for block in message.get("content", []):
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                texts.append(block["text"])
            elif btype == "tool_use":
                self.on_tool_use(turn, block)
        if texts:
            joined = redact("\n".join(texts))
            msg_id = message.get("id", "")
            if msg_id and msg_id == self.last_msg_id:
                turn.assistant_text = f"{turn.assistant_text}\n{joined}".strip()
            else:
                turn.assistant_text = joined
            self.last_msg_id = msg_id

    def on_tool_use(self, turn: Turn, block: dict) -> None:
        name = block.get("name", "")
        inp = block.get("input", {}) if isinstance(block.get("input"), dict) else {}
        path_str = tool_path(inp)
        if path_str and (name in WRITE_TOOLS or name in READ_TOOLS):
            action = "write" if name in WRITE_TOOLS else "read"
            turn.files.setdefault(path_str, set()).add(action)
        if name.startswith(SELF_TOOL_PREFIXES) or name in SKIP_TOOLS:
            return
        turn.tools[name] += 1
        summary = agent_summary(inp) if name == AGENT_TOOL else input_summary(inp)
        event = ToolEvent(seq=len(turn.events), name=name, input_summary=summary)
        turn.events.append(event)
        use_id = block.get("id", "")
        self.pending[use_id] = event
        if name == AGENT_TOOL:
            self.agents[use_id] = event
        if name in READ_TOOLS and is_sensitive_path(path_str):
            self.sensitive.add(use_id)


def parse_transcript(path: Path, session_id: str) -> ParsedSession:
    parser = TranscriptParser(session_id)
    for rec in iter_records(path):
        parser.feed(rec)
    return parser.session
