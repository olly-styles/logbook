import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROMPT = "coupon codes with spaces are rejected at checkout again"
SEARCH = "connection pool"
NOW = datetime.now(UTC)
HOME = str(Path.home())
NEW_SESSION = "ffffffff-0000-4000-8000-000000000000"


def stamp(days_ago: float, minutes: int = 0) -> str:
    return (NOW - timedelta(days=days_ago) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def uid(*parts: str) -> str:
    return hashlib.md5("".join(parts).encode(), usedforsecurity=False).hexdigest()


def session(
    sid8: str,
    days_ago: float,
    cwd: str,
    branch: str,
    title: str,
    prompt: str,
    reply: str,
    pr: tuple[str, int] = ("", 0),
    recaps: tuple[str, ...] = (),
) -> tuple[str, list[str]]:
    sid = f"{sid8}-0000-4000-8000-000000000000"
    base = {"sessionId": sid, "cwd": cwd, "gitBranch": branch, "version": "2.1.263", "isSidechain": False}
    message = {
        "model": "claude-fable-5-1",
        "id": uid(sid, "m"),
        "role": "assistant",
        "content": [{"type": "text", "text": reply}],
        "stop_reason": "end_turn",
    }
    lines: list[dict[str, object]] = [
        {
            "type": "user",
            "uuid": uid(sid, "u"),
            "message": {"role": "user", "content": prompt},
            "timestamp": stamp(days_ago),
            **base,
        },
        {"type": "assistant", "uuid": uid(sid, "a"), "message": message, "timestamp": stamp(days_ago, 4), **base},
        {"type": "ai-title", "aiTitle": title, "sessionId": sid},
    ]
    if pr[1]:
        url = f"https://github.com/acme/{pr[0]}/pull/{pr[1]}"
        lines.append(
            {"type": "pr-link", "prRepository": f"acme/{pr[0]}", "prNumber": pr[1], "prUrl": url, "sessionId": sid}
        )
    for i, text in enumerate(recaps):
        lines.append(
            {
                "type": "system",
                "subtype": "away_summary",
                "content": f"{text} (disable recaps in /config)",
                "timestamp": stamp(days_ago, 10 + i),
                **base,
            }
        )
    return sid, [json.dumps(line) for line in lines]


SESSIONS = [
    session(
        "9d7115b4",
        0.15,
        f"{HOME}/code/shopfront",
        "main",
        "Checkout coupon field validation",
        "Coupon codes with trailing spaces are rejected at checkout, customers are complaining. Can you fix it?",
        "Trimmed the field before validation and added a test. PR 241 is open.",
        ("shopfront", 241),
    ),
    session(
        "425db905",
        1,
        f"{HOME}/code/billing-api",
        "fix/invoice-rounding",
        "Invoice totals off by one cent",
        "Invoice totals are off by one cent on some orders with mixed tax rates. Find out why and fix it.",
        "Rounding happened per line instead of on the total. Fixed in PR 95.",
        ("billing-api", 95),
    ),
    session(
        "437f5780",
        3,
        f"{HOME}/code/shopfront",
        "feat/image-cdn",
        "Move product images to a CDN",
        "Move the product images to the CDN and make sure the cache headers suit a catalogue that changes daily.",
        "Images now come from the CDN with a 30 day cache. PR 230.",
        ("shopfront", 230),
    ),
    session(
        "7e7cb681",
        6,
        f"{HOME}/dotfiles",
        "main",
        "Migrate shell config from zsh to fish",
        "Migrate my shell config from zsh to fish, keeping the aliases, prompt and PATH setup working.",
        "Ported aliases, prompt and PATH setup. Abbreviations replace the zsh aliases.",
    ),
    session(
        "c0828e03",
        8,
        f"{HOME}/code/billing-api",
        "main",
        "Postgres connection pool exhaustion",
        "Production is running out of Postgres connections under load, about an hour after each deploy.",
        "Each request was opening its own connection because the pool was created per worker after the fork. "
        "Moving pool creation into the worker startup hook fixed it; PR 91 is deployed to staging.",
        ("billing-api", 91),
        (
            "Diagnosed the connection exhaustion: pool created per worker post-fork. Fix in PR 91, deployed to "
            "staging. Next action: watch prod connection count after the deploy.",
        ),
    ),
    session(
        "3b9f2c11",
        14,
        f"{HOME}/code/billing-api",
        "main",
        "Rate limit the public API",
        "Add rate limiting to the public API so a single key cannot take down the service for everyone else.",
        "Token bucket per API key in Redis, 600 requests a minute, with a Retry-After header.",
    ),
    session(
        "8ddf8780",
        20,
        f"{HOME}/code/shopfront",
        "main",
        "Fix flaky checkout test",
        "The checkout test is flaky on CI, it fails about one run in five with no code changes.",
        "The test depended on dict ordering of the cart items. Sorted the items before comparing; 50 green runs since.",
        ("shopfront", 212),
    ),
]

STYLE = """
body{margin:0;background:#1e1f22}
#term{display:inline-block;box-sizing:border-box;width:1180px;padding:28px 26px 30px;background:#1e1f22;
color:#d9d9d6;font:15px/1.55 "SF Mono",Menlo,monospace}
#term h2{margin:0 0 6px;font:inherit;font-weight:700;color:#f2f2ef}
#term h2 span{font-weight:400;color:#7d7c78;margin-left:14px}
#term pre{margin:0 0 28px;font:inherit;white-space:pre-wrap;word-break:break-word}
#term pre:last-child{margin-bottom:0}
"""


def logbook(env: dict[str, str], *args: str, stdin: str = "") -> str:
    exe = shutil.which("logbook")
    if exe is None:
        raise FileNotFoundError("logbook is not on PATH; run this with `uv run`")
    return subprocess.run([exe, *args], env=env, check=True, capture_output=True, text=True, input=stdin).stdout


def main(out_dir: Path) -> None:
    home = Path(tempfile.mkdtemp(prefix="logbook-shot-"))
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "LOGBOOK_DB": str(home / "data" / "history.db"),
        "LOGBOOK_ARCHIVE": str(home / "data" / "archive"),
        "LOGBOOK_LOG": str(home / "data" / "queries.jsonl"),
    }
    for sid, lines in SESSIONS:
        cwd = json.loads(lines[0])["cwd"]
        path = home / "claude" / "projects" / cwd.replace("/", "-") / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
    logbook(env, "sync")
    payload = json.dumps({"prompt": PROMPT, "session_id": NEW_SESSION, "cwd": f"{HOME}/code/shopfront"})
    hook_text = json.loads(logbook(env, "prompt-hook", stdin=payload))["hookSpecificOutput"]["additionalContext"]
    os.environ.update(env)
    from logbook import server

    search_text = server.search(SEARCH)
    page = f"""<!doctype html><meta charset="utf-8"><style>{STYLE}</style>
<div id="term">
<h2>UserPromptSubmit hook<span>context injected on the first prompt of a session</span></h2>
<pre>{html.escape(hook_text)}</pre>
<h2>search("{SEARCH}")<span>MCP tool result, as returned to the agent</span></h2>
<pre>{html.escape(search_text)}</pre>
</div>"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "shot.html").write_text(page)
    print(out_dir / "shot.html")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
