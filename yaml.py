"""Tiny YAML subset used in SyncSage's dependency-light test environment.

It supports the simple mappings/lists/scalars used by SyncSage examples. In a
normal install PyYAML satisfies the same imports with broader YAML support.
"""
from __future__ import annotations

import json
from typing import Any


def _scalar(text: str) -> Any:
    text = text.strip()
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


def safe_dump(data: Any, sort_keys: bool = False, **_: Any) -> str:
    def emit(obj, indent=0):
        sp = " " * indent
        if isinstance(obj, dict):
            items = sorted(obj.items()) if sort_keys else obj.items()
            out=[]
            for k,v in items:
                if isinstance(v, (dict, list)):
                    out.append(f"{sp}{k}:"); out.append(emit(v, indent+2))
                else: out.append(f"{sp}{k}: {v}")
            return "\n".join(out)
        if isinstance(obj, list):
            out=[]
            for v in obj:
                if isinstance(v, (dict, list)):
                    out.append(f"{sp}-"); out.append(emit(v, indent+2))
                else: out.append(f"{sp}- {v}")
            return "\n".join(out)
        return f"{sp}{obj}"
    return emit(data) + "\n"
