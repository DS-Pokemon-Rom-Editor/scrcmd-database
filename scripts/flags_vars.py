#!/usr/bin/env python3
"""Parse decomp flags/vars into v2 ``flags`` and ``vars`` sections (name -> id)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

HGSS_EXCLUDED_FLAG_PREFIXES = ("FLAG_ACTION_",)

HGSS_VAR_META_NAMES = frozenset(
    {
        "VAR_BASE",
        "TEMP_VAR_BASE",
        "VAR_OBJ_GFX_BASE",
        "SPECIAL_VAR_BASE",
        "VARS_START",
        "VARS_END",
        "SPECIAL_VARS_START",
        "SPECIAL_VARS_END",
    }
)

C_DEFINE_RE = re.compile(
    r"^\s*#define\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+?)\s*(?://.*)?$"
)
C_INT_RE = re.compile(r"^(?:0x[0-9A-Fa-f]+|\d+)$")
C_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

REPO_ROOT = Path(__file__).resolve().parents[1]
METANG_SCRIPT = REPO_ROOT / "metang" / "metang.py"


def parse_metang_enum(content: str) -> list[tuple[str, int]]:
    """Parse pokeplatinum ``vars_flags.txt`` via the metang submodule."""
    if not METANG_SCRIPT.is_file():
        raise RuntimeError(
            "metang submodule not found; run: git submodule update --init metang"
        )

    result = subprocess.run(
        [sys.executable, str(METANG_SCRIPT), "enum", "-L", "json", "-"],
        input=content,
        capture_output=True,
        text=True,
        cwd=METANG_SCRIPT.parent,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "metang failed"
        raise RuntimeError(message)

    return list(json.loads(result.stdout).items())


def _resolve_define_rhs(rhs: str, values: dict[str, int]) -> int:
    """Resolve a ``#define`` RHS: literal, alias, or ``A + B - C``."""
    rhs = rhs.strip()

    if C_INT_RE.fullmatch(rhs):
        return int(rhs, 0)

    if C_IDENT_RE.fullmatch(rhs):
        return values[rhs]

    if rhs.startswith("(") and rhs.endswith(")"):
        inner = rhs[1:-1].strip()
        if "+" not in inner and "-" not in inner:
            return _resolve_define_rhs(inner, values)
        rhs = inner

    total = 0
    sign = 1
    for part in re.split(r"\s*([+-])\s*", rhs):
        if part == "+":
            sign = 1
        elif part == "-":
            sign = -1
        elif part:
            token = part.strip()
            if C_IDENT_RE.fullmatch(token):
                total += sign * values[token]
            else:
                total += sign * int(token, 0)
    return total


def parse_c_header_symbols(content: str) -> dict[str, int]:
    """Parse ``#define`` constants from a C header."""
    defines: dict[str, str] = {}
    for line in content.splitlines():
        match = C_DEFINE_RE.match(line)
        if match:
            defines[match.group(1)] = match.group(2).strip()

    values: dict[str, int] = {}
    pending = dict(defines)
    for _ in range(len(pending) + 1):
        if not pending:
            break
        progress = False
        for name in list(pending):
            try:
                values[name] = _resolve_define_rhs(pending[name], values)
            except (KeyError, ValueError):
                continue
            del pending[name]
            progress = True
        if not progress:
            break

    return values


def _db_section(symbols: dict[str, int]) -> dict[str, dict[str, int]]:
    return {
        name: {"id": symbols[name]}
        for name in sorted(symbols.keys(), key=lambda n: (symbols[n], n))
    }


def merge_symbol_section(
    existing: dict[str, dict], new: dict[str, dict[str, int]]
) -> dict[str, dict]:
    """Merge decomp symbols; preserve non-``id`` fields; order keys by id.

    When a name disappears from the decomp but its id matches a new name, the
    old entry is treated as a rename: any extra metadata (non-``id`` fields) is
    migrated to the new name and the old name is dropped.  This handles the
    common pattern of ``VAR_UNK_0x...`` / ``FLAG_UNK_0x...`` entries gaining
    a proper name upstream without leaving stale duplicates in the database.
    """
    merged: dict[str, dict] = {name: dict(entry) for name, entry in new.items()}

    # Reverse map so we can detect renames by id.
    id_to_new_name: dict[int, str] = {
        int(entry["id"]): name
        for name, entry in merged.items()
        if "id" in entry
    }

    for name, entry in existing.items():
        if name in merged:
            # Same name still present: carry over any extra metadata.
            for key, value in entry.items():
                if key != "id":
                    merged[name][key] = value
        elif "id" in entry and (entry_id := int(entry["id"])) in id_to_new_name:
            # This name was removed from the decomp but its id still exists under
            # a new name — it was renamed.  Transfer metadata to the new name
            # without clobbering anything already set there.
            new_name = id_to_new_name[entry_id]
            for key, value in entry.items():
                if key != "id" and key not in merged[new_name]:
                    merged[new_name][key] = value
        else:
            # DB-only entry with no counterpart in the decomp: keep it.
            merged[name] = dict(entry)

    by_id = {name: int(entry["id"]) for name, entry in merged.items() if "id" in entry}
    return {
        name: merged[name]
        for name in sorted(by_id.keys(), key=lambda n: (by_id[n], n))
    }


def parse_platinum_vars_flags(
    content: str,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    flags: dict[str, int] = {}
    vars_: dict[str, int] = {}

    for name, value in parse_metang_enum(content):
        if name.startswith("FLAG_"):
            flags[name] = value
        elif (
            name.startswith("VAR_")
            or name.startswith("VARS_")
            or name.startswith("FR_VAR_")
            or "_VARS_START" in name
            or "_VARS_END" in name
        ):
            vars_[name] = value

    return _db_section(flags), _db_section(vars_)


def parse_hgss_flags_header(content: str) -> dict[str, dict[str, int]]:
    flags = {
        name: value
        for name, value in parse_c_header_symbols(content).items()
        if not name.startswith(HGSS_EXCLUDED_FLAG_PREFIXES)
        and (
            name.startswith("FLAG_")
            or name.startswith("NUM_")
            or name.endswith("_FLAG_BASE")
        )
    }
    return _db_section(flags)


def parse_hgss_vars_header(content: str) -> dict[str, dict[str, int]]:
    vars_ = {
        name: value
        for name, value in parse_c_header_symbols(content).items()
        if name.startswith("VAR_")
        or name.startswith("NUM_")
        or name.startswith("VARS_")
        or name in HGSS_VAR_META_NAMES
        or name.endswith("_VAR_BASE")
    }
    return _db_section(vars_)


def apply_symbol_section(db: dict, key: str, new: dict[str, dict[str, int]]) -> bool:
    merged = merge_symbol_section(db.get(key, {}), new)
    existing = db.get(key)
    if existing == merged and list((existing or {}).keys()) == list(merged.keys()):
        return False
    db[key] = merged
    return True


def sync_flags_vars(
    db: dict,
    sources: dict[str, str],
    version: str,
    fetch: Callable[[str], str | None],
) -> bool:
    """Refresh ``flags`` and ``vars`` sections from decomp symbol sources."""
    changed = False

    if version == "Platinum" and "vars_flags" in sources:
        print("  Fetching vars_flags.txt...")
        if not (content := fetch(sources["vars_flags"])):
            return False
        flags, vars_ = parse_platinum_vars_flags(content)
        print(f"  Parsed {len(flags)} flags and {len(vars_)} vars from vars_flags.txt")
        changed = apply_symbol_section(db, "flags", flags) or apply_symbol_section(
            db, "vars", vars_
        )

    elif version == "HeartGold/SoulSilver":
        parsed_sections: list[tuple[str, dict[str, dict[str, int]]]] = []
        for source_key, label, parser in (
            ("flags", "flags.h", parse_hgss_flags_header),
            ("vars", "vars.h", parse_hgss_vars_header),
        ):
            if source_key not in sources:
                continue
            print(f"  Fetching {label}...")
            if content := fetch(sources[source_key]):
                section = parser(content)
                print(f"  Parsed {len(section)} {source_key} from {label}")
                parsed_sections.append((source_key, section))

        if not parsed_sections:
            return False

        for key, section in parsed_sections:
            changed = apply_symbol_section(db, key, section) or changed

    else:
        return False

    if changed:
        print("  Updated flags and vars sections")
    return changed
