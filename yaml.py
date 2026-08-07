"""Tiny YAML subset used in pheasant's dependency-light test environment.

It supports the simple mappings/lists/scalars used by pheasant examples. In a
normal install PyYAML satisfies the same imports with broader YAML support.
"""
from __future__ import annotations

import json
from typing import Any


def _strip_comment(text: str) -> str:
    """Drop a trailing ``# ...`` comment, ignoring ``#`` inside quotes.

    ``safe_load`` already skips whole-line comments, but never trailing ones,
    so every annotated value in ``pheasant.example.yaml`` used to round-trip
    with its comment glued on: ``max_files`` came back as the *string*
    ``"50000        # matching files, ..."`` instead of the int ``50000``, and
    — worse — ``follow_symlinks: false  # ...`` became a non-empty string,
    which is **truthy**, silently inverting a safety default under this parser.
    PyYAML strips these, so not stripping them made the two parsers disagree
    about the shipped reference config.

    A ``#`` only starts a comment when it follows whitespace (or opens the
    value), matching PyYAML: ``red#1`` stays ``red#1``.
    """
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
    return text


def _scalar(text: str) -> Any:
    text = _strip_comment(text).strip()
    if text in {"", "null", "~"}: return None if text in {"null", "~"} else ""
    if text in {"true", "True"}: return True
    if text in {"false", "False"}: return False
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    if text.startswith("[") or text.startswith("{"):
        try: return json.loads(text.replace("'", '"'))
        except Exception: return text
    try: return int(text)
    except ValueError: pass
    try: return float(text)
    except ValueError: return text


def safe_load(stream: str) -> Any:
    lines = []
    for raw in stream.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or raw.strip() == "---":
            continue
        lines.append(raw.rstrip())
    idx = 0

    def parse_block(indent: int):
        nonlocal idx
        container = None
        while idx < len(lines):
            line = lines[idx]
            cur = len(line) - len(line.lstrip(" "))
            if cur < indent: break
            if cur > indent:
                break
            text = line.strip()
            if text.startswith("- "):
                if container is None: container = []
                item = text[2:]
                if ":" in item and not item.startswith(('"', "'")):
                    key, val = item.split(":", 1)
                    d = {key.strip(): _scalar(val.strip()) if val.strip() else None}
                    idx += 1
                    if not val.strip() and idx < len(lines):
                        d[key.strip()] = parse_block(indent + 2)
                    while idx < len(lines):
                        nxt = lines[idx]
                        ni = len(nxt) - len(nxt.lstrip(" "))
                        if ni != indent + 2 or nxt.strip().startswith("- "): break
                        k, v = nxt.strip().split(":", 1)
                        idx += 1
                        d[k.strip()] = _scalar(v.strip()) if v.strip() else parse_block(indent + 4)
                    container.append(d)
                else:
                    container.append(_scalar(item)); idx += 1
            else:
                if container is None: container = {}
                key, val = text.split(":", 1)
                idx += 1
                container[key.strip()] = _scalar(val.strip()) if val.strip() else parse_block(indent + 2)
        return container
    return parse_block(0) or {}


def safe_load_all(stream: str):
    yield safe_load(stream)


def _needs_quotes(text: str) -> bool:
    """True when a bare scalar would not round-trip through this parser.

    A list item containing ``:`` is read back as a mapping (see parse_block),
    and a leading indicator character changes the node type — so URLs,
    ``host:port`` pairs and ``*`` glob patterns all have to be quoted. PyYAML
    accepts the quoted form identically, so the emitted document means the
    same thing under either parser.
    """
    if text == "":
        return True
    return ":" in text or text[0] in "*&!%@`[{|>-?#,'\"" or text != text.strip()


def _emit_scalar(value: Any) -> str:
    if value is True: return "true"
    if value is False: return "false"
    if value is None: return "null"
    if isinstance(value, (int, float)): return str(value)
    text = str(value)
    if _needs_quotes(text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def safe_dump(data: Any, sort_keys: bool = False, **_: Any) -> str:
    def emit(obj, indent=0):
        sp = " " * indent
        if isinstance(obj, dict):
            items = sorted(obj.items()) if sort_keys else obj.items()
            out=[]
            for k,v in items:
                if isinstance(v, (dict, list)):
                    out.append(f"{sp}{k}:"); out.append(emit(v, indent+2))
                else: out.append(f"{sp}{k}: {_emit_scalar(v)}")
            return "\n".join(out)
        if isinstance(obj, list):
            out=[]
            for v in obj:
                if isinstance(v, (dict, list)):
                    out.append(f"{sp}-"); out.append(emit(v, indent+2))
                else: out.append(f"{sp}- {_emit_scalar(v)}")
            return "\n".join(out)
        return f"{sp}{_emit_scalar(obj)}"
    return emit(data) + "\n"
