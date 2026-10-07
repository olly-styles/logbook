import re
from dataclasses import dataclass

HEX8 = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){0,3}(-[0-9a-f]{12})?$", re.IGNORECASE)
TURN_IDX = re.compile(r"^t?(\d+)$")
ADDRESS = re.compile(r"^([0-9a-f-]+)(?:/([0-9a-f]+|t\d+)(?:-t?(\d+))?)?(?:#(\d+))?$", re.IGNORECASE)
ANCHOR_LEN = 8


@dataclass
class Address:
    session: str = ""
    turn: str = ""
    seq: int = -1
    turn_end: int = -1


def parse_address(text: str) -> Address:
    m = ADDRESS.match(text.strip())
    if m is None or not HEX8.match(m.group(1)):
        raise ValueError(
            f"no session id in address {text!r}; expected 27c3625f, 27c3625f/70d158d1, 27c3625f/t16, "
            "27c3625f/t16#3 or 27c3625f/t16-20"
        )
    turn = (m.group(2) or "").lower()
    seq = int(m.group(4)) if m.group(4) else -1
    if m.group(3) is None:
        return Address(m.group(1), turn, seq)
    if turn.isdigit():
        raise ValueError(f"turn range in {text!r} needs a t prefix: write {m.group(1)}/t{turn}-{m.group(3)}")
    start = TURN_IDX.match(turn)
    if start is None or not turn.startswith("t"):
        raise ValueError(f"turn range in {text!r} needs turn indexes, as in 27c3625f/t16-20")
    end = int(m.group(3))
    if end < int(start.group(1)):
        raise ValueError(f"turn range in {text!r} runs backwards; write the lower index first, as in t16-20")
    if seq >= 0:
        raise ValueError(f"#{seq} needs a single turn, not the range in {text!r}; use 27c3625f/t16#{seq}")
    return Address(m.group(1), turn, seq, end)
