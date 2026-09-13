import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from .paths import claude_dir

SETTINGS_INDENT = 2
MCP_NAME = "logbook"
REPO_URL = "https://github.com/olly-styles/logbook"
ASSETS = {
    "agents/recall.md": "agents/recall.md",
    "skills/recall/SKILL.md": "skills/recall/SKILL.md",
}


@dataclass
class Hook:
    event: str
    subcommand: str
    timeout: int
    status: str

    def entry(self, exe: str) -> dict[str, object]:
        return {
            "type": "command",
            "command": f"{exe} {self.subcommand}",
            "timeout": self.timeout,
            "statusMessage": self.status,
        }


HOOKS = [
    Hook("SessionStart", "hook", 30, "Indexing session history"),
    Hook("UserPromptSubmit", "prompt-hook", 5, "Indexing this session"),
    Hook("Stop", "index-hook", 10, "Indexing this session"),
    Hook("SessionEnd", "index-hook", 30, "Indexing and archiving this session"),
]


def executable() -> str:
    found = shutil.which(MCP_NAME)
    if found is None:
        raise SystemExit(
            f"{MCP_NAME} is not on PATH, so hooks and the MCP server cannot be pointed at it. Install it with "
            f"`uv tool install git+{REPO_URL}` (then `uv tool update-shell` if needed) and run `{MCP_NAME} install` "
            "again."
        )
    return str(Path(found))


def is_ours(command: object, subcommand: str) -> bool:
    if not isinstance(command, str):
        return False
    parts = command.split()
    return len(parts) == len((MCP_NAME, subcommand)) and parts[0].endswith(MCP_NAME) and parts[1] == subcommand


def merge_hooks(settings: dict, exe: str) -> bool:
    changed = False
    hooks = settings.setdefault("hooks", {})
    for hook in HOOKS:
        wanted = hook.entry(exe)
        groups = hooks.setdefault(hook.event, [])
        existing = [h for g in groups for h in g.get("hooks", []) if is_ours(h.get("command"), hook.subcommand)]
        if not existing:
            groups.append({"matcher": "", "hooks": [wanted]})
            print(f"settings.json: added {hook.event} hook `{wanted['command']}`")
            changed = True
        elif existing[0]["command"] != wanted["command"]:
            previous = existing[0]["command"]
            existing[0]["command"] = wanted["command"]
            print(f"settings.json: {hook.event} hook now runs `{wanted['command']}` (was `{previous}`)")
            changed = True
        else:
            print(f"settings.json: {hook.event} hook already present")
    return changed


def install_hooks(exe: str) -> None:
    path = claude_dir() / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if merge_hooks(settings, exe):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=SETTINGS_INDENT) + "\n", encoding="utf-8")


def symlinked(dest: Path, root: Path) -> bool:
    return any(p.is_symlink() for p in (dest, *dest.parents) if root in p.parents)


def install_assets() -> None:
    for src_name, dest_name in ASSETS.items():
        content = files("logbook").joinpath("claude", src_name).read_text(encoding="utf-8")
        dest = claude_dir() / dest_name
        if symlinked(dest, claude_dir()):
            print(f"{dest}: is or sits under a symlink, left alone")
            continue
        if dest.is_file() and dest.read_text(encoding="utf-8") == content:
            print(f"{dest}: already up to date")
            continue
        verb = "updated" if dest.is_file() else "installed"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        print(f"{dest}: {verb}")


def run_claude(claude: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([claude, "mcp", *args], capture_output=True, text=True, check=False)


def register_mcp(exe: str, run: Callable[..., subprocess.CompletedProcess[str]] = run_claude) -> None:
    add_args = ["add", "--scope", "user", MCP_NAME, "--", exe, "serve"]
    claude = shutil.which("claude")
    if claude is None:
        print(f"claude is not on PATH; register the MCP server yourself with: claude mcp {' '.join(add_args)}")
        return
    current = run(claude, "get", MCP_NAME)
    if current.returncode == 0 and exe in current.stdout:
        print(f"MCP server {MCP_NAME} already registered")
        return
    if current.returncode == 0:
        removed = run(claude, "remove", "--scope", "user", MCP_NAME)
        if removed.returncode != 0:
            raise SystemExit(f"claude mcp remove failed ({removed.returncode}): {removed.stderr.strip()}")
    added = run(claude, *add_args)
    if added.returncode != 0:
        raise SystemExit(f"claude mcp add failed ({added.returncode}): {added.stderr.strip() or added.stdout.strip()}")
    print(f"MCP server {MCP_NAME} registered at user scope -> {exe} serve")


def wire(exe: str) -> None:
    install_hooks(exe)
    install_assets()
    register_mcp(exe)
    print("Restart Claude Code to pick up the hooks and the MCP server.")
