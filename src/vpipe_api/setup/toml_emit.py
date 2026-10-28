"""A TOML writer for the subset our config files use (the stdlib only reads TOML).

Tables, arrays of tables, and str / int / float / bool / list[str] / list[int] values. Anything else
raises ``TypeError`` rather than writing a file ``tomllib`` would read differently.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _string(value: str) -> str:
    out = []
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _string(key)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _value(value: Any) -> str:
    if isinstance(value, bool):  # before int: bool is an int
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"cannot write {value} to TOML")
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(_string(item) for item in value) + "]"
    if isinstance(value, list) and all(_is_int(item) for item in value):
        return "[" + ", ".join(str(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, Mapping) for v in value)


def _table(data: Mapping[str, Any], path: Sequence[str], lines: list[str]) -> None:
    for key, value in data.items():
        if not isinstance(value, Mapping) and not _is_table_array(value):
            lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in data.items():
        if isinstance(value, Mapping):
            header = ".".join(_key(part) for part in (*path, key))
            lines.extend(["", f"[{header}]"])
            _table(value, (*path, key), lines)
    for key, value in data.items():
        if _is_table_array(value):
            header = ".".join(_key(part) for part in (*path, key))
            for item in value:
                if any(isinstance(v, Mapping) or _is_table_array(v) for v in item.values()):
                    raise TypeError(f"nested tables inside [[{header}]] are not supported")
                lines.extend(["", f"[[{header}]]"])
                _table(item, (*path, key), lines)


def dumps(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    _table(data, (), lines)
    return "\n".join(lines).lstrip("\n") + "\n"
