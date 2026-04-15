#!/usr/bin/env python3
"""
Generate Excel spreadsheet from v2 database files.

Supports both old format (*_scrcmd_database.json) and new v2 format (*_v2.json).
"""

import glob
import json
import os
import re

import xlsxwriter

# Configuration
NAME_MAP = {"diamond_pearl": "DP", "platinum": "Pt", "hgss": "HGSS"}
MATCH_STRIP_CHARS = ".,;:!?()[]{}"

# Load styles from JSON
script_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(script_dir, "styles.json"), "r", encoding="utf-8") as f:
    s = json.load(f)
    HEADER = {int(k): v for k, v in s["header"].items()}
    DATA = {int(k): v for k, v in s["data"].items()}
    CW = {int(k): v for k, v in s["widths"].items()}


def is_v2_format(data: dict) -> bool:
    """Check if database is in v2 format."""
    return "commands" in data and "meta" in data


def load_database(path: str) -> dict:
    """Load database and normalize to v2-like structure."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if is_v2_format(data):
        return data

    # Convert old format to v2-like structure for processing
    result = {
        "meta": {"version": "Unknown"},
        "commands": {},
        "sounds": data.get("sounds", {}),
    }

    # Convert scrcmd
    for hex_id, info in data.get("scrcmd", {}).items():
        cmd_id = int(hex_id, 16)
        name = info.get("decomp_name", info.get("name", f"cmd_{cmd_id}"))

        # Build params from parameter_values
        params = []
        for pv in info.get("parameter_values", []):
            if ":" in pv:
                type_str, param_name = pv.split(":", 1)
                params.append({"type": type_str.strip(), "name": param_name.strip()})
            else:
                params.append({"type": "u16", "name": pv})

        result["commands"][name] = {
            "type": "script_cmd",
            "id": cmd_id,
            "legacy_name": info.get("name"),
            "description": info.get("description", ""),
            "params": params,
        }

    # Convert movements
    for hex_id, info in data.get("movements", {}).items():
        mov_id = int(hex_id, 16)
        name = info.get("decomp_name", info.get("name", f"mov_{mov_id}"))

        # Handle name collision
        if name in result["commands"]:
            name = f"{name}Movement"

        result["commands"][name] = {
            "type": "movement",
            "id": mov_id,
            "legacy_name": info.get("name"),
            "description": info.get("description", ""),
        }

    return result


def prettify_param_type(type_name: str) -> str:
    """Format parameter type names for display."""
    type_map = {
        "var": "Var",
    }
    return type_map.get(type_name, type_name)


def split_identifier_words(identifier: str) -> list[str]:
    """Split snake_case / camelCase / numbered identifiers into words."""
    identifier = re.sub(r"VarID(\d+)$", r"Variable \1", identifier)
    identifier = re.sub(r"VarID$", "Variable", identifier)
    identifier = re.sub(r"varID(\d+)$", r"Variable \1", identifier)
    identifier = re.sub(r"varID$", "Variable", identifier)
    identifier = re.sub(
        r"_var_id(\d+)$", r"_variable_\1", identifier, flags=re.IGNORECASE
    )
    identifier = re.sub(r"_var_id$", "_variable", identifier, flags=re.IGNORECASE)

    spaced = identifier.replace("_", " ")
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced)
    spaced = re.sub(r"([A-Za-z])(\d)", r"\1 \2", spaced)
    spaced = re.sub(r"(\d)([A-Za-z])", r"\1 \2", spaced)

    return [word for word in spaced.split() if word]


def prettify_word(word: str) -> str:
    """Prettify a single identifier word."""
    upper_words = {"id", "cmd", "npc"}
    exact_words = {
        "ow": "OW",
        "sfx": "SFX",
        "bgm": "BGM",
    }

    lower = word.lower()
    if lower in exact_words:
        return exact_words[lower]
    if lower in upper_words:
        return lower.upper()
    if lower == "variable":
        return "Variable"
    return word.capitalize()


def prettify_param_name(name: str, type_name: str | None = None) -> str:
    """Format parameter names for display."""
    if not name or name == "???":
        return name

    words = split_identifier_words(name)
    if not words:
        return name

    prettified = [prettify_word(word) for word in words]

    if type_name == "var" and prettified == ["Variable"]:
        return "Variable"

    return " ".join(prettified)


def normalize_match_text(text: str, case_sensitive: bool = True) -> str:
    """Normalize text for parameter-name matching."""
    parts = [part.strip(MATCH_STRIP_CHARS) for part in text.split()]
    normalized = " ".join(part for part in parts if part)
    return normalized if case_sensitive else normalized.casefold()


def extract_highlight_terms(params: list[dict]) -> list[str]:
    """Build ordered parameter-name terms to highlight in descriptions."""
    terms = []
    for param in params:
        name = param["name"]
        terms.append(name)
        words = name.split()
        if len(words) > 1:
            terms.extend(words)

    unique_terms = []
    seen = set()
    for term in terms:
        normalized = normalize_match_text(term)
        if normalized and normalized not in seen:
            unique_terms.append(term)
            seen.add(normalized)
    return unique_terms


def build_description_segments(
    description: str, params: list[dict]
) -> list[tuple[str, bool]]:
    """Split description into plain/bold segments based on parameter-name matches."""
    if not description:
        return []

    terms = extract_highlight_terms(params)
    if not terms:
        return [(description, False)]

    token_matches = list(re.finditer(r"\S+", description))
    if not token_matches:
        return [(description, False)]

    def find_matches_for_terms(
        available_terms: list[str], case_sensitive: bool
    ) -> list[tuple[int, int]]:
        normalized_terms = [
            (
                term,
                normalize_match_text(term, case_sensitive=case_sensitive),
                len(term.split()),
            )
            for term in sorted(
                available_terms, key=lambda value: len(value.split()), reverse=True
            )
        ]

        used = [False] * len(token_matches)
        matches: list[tuple[int, int]] = []

        for token_index in range(len(token_matches)):
            if used[token_index]:
                continue

            best_match: tuple[int, int] | None = None
            for _, normalized_term, token_count in normalized_terms:
                end_index = token_index + token_count - 1
                if end_index >= len(token_matches):
                    continue
                if any(used[i] for i in range(token_index, end_index + 1)):
                    continue

                start = token_matches[token_index].start()
                end = token_matches[end_index].end()
                candidate = description[start:end]

                if (
                    normalize_match_text(candidate, case_sensitive=case_sensitive)
                    == normalized_term
                ):
                    best_match = (token_index, end_index)
                    break

            if best_match is None:
                continue

            start_token, end_token = best_match
            for i in range(start_token, end_token + 1):
                used[i] = True

            start = token_matches[start_token].start()
            end = token_matches[end_token].end()
            matches.append((start, end))

        return matches

    exact_matches = find_matches_for_terms(terms, case_sensitive=True)
    matched_exact_terms = set()
    for term in terms:
        normalized_term = normalize_match_text(term, case_sensitive=True)
        for start, end in exact_matches:
            if (
                normalize_match_text(description[start:end], case_sensitive=True)
                == normalized_term
            ):
                matched_exact_terms.add(term)
                break

    fallback_terms = [term for term in terms if term not in matched_exact_terms]
    fallback_matches = find_matches_for_terms(fallback_terms, case_sensitive=False)

    all_matches = sorted(exact_matches + fallback_matches, key=lambda span: span[0])

    if not all_matches:
        return [(description, False)]

    non_overlapping_matches: list[tuple[int, int]] = []
    for start, end in all_matches:
        if not non_overlapping_matches:
            non_overlapping_matches.append((start, end))
            continue

        last_start, last_end = non_overlapping_matches[-1]
        if start < last_end:
            if (end - start) > (last_end - last_start):
                non_overlapping_matches[-1] = (start, end)
            continue

        non_overlapping_matches.append((start, end))

    segments = []
    cursor = 0
    for start, end in non_overlapping_matches:
        if start > cursor:
            segments.append((description[cursor:start], False))
        segments.append((description[start:end], True))
        cursor = end

    if cursor < len(description):
        segments.append((description[cursor:], False))

    merged = []
    for text, is_bold in segments:
        if not text:
            continue
        if merged and merged[-1][1] == is_bold:
            merged[-1] = (merged[-1][0] + text, is_bold)
        else:
            merged.append((text, is_bold))

    return merged


def build_param_segments(cmd: dict) -> tuple[str, list[dict], list[tuple[str, bool]]]:
    """
    Build plain and rich-text parameter representations.

    Returns (formatted_string, prettified_params, segments)
    """
    if "variants" in cmd:
        lines = []
        segments = []

        for i, variant in enumerate(cmd["variants"]):
            if lines:
                segments.append(("\n", False))

            prefix = f"[{i}] "
            lines.append(prefix)
            segments.append((prefix, False))

            variant_params = variant.get("params", [])
            for index, param in enumerate(variant_params):
                if index > 0:
                    lines.append("; ")
                    segments.append(("; ", False))

                type_text = prettify_param_type(param["type"])
                name_text = prettify_param_name(param["name"], param["type"])
                text = f"{type_text}: {name_text}"
                if "const" in param:
                    text += f"={param['const']}"

                lines.append(text)
                segments.append((f"{type_text}:", True))
                segments.append(
                    (
                        f" {name_text}"
                        + (f"={param['const']}" if "const" in param else ""),
                        False,
                    )
                )

            desc = variant.get("desc", "")
            if desc:
                lines.append(f" - {desc}")
                segments.append((f" - {desc}", False))

        return "".join(lines), [], segments

    prettified_params = []
    segments = []

    for index, param in enumerate(cmd.get("params", [])):
        if index > 0:
            segments.append(("; ", False))

        prettified_params.append(
            {
                "type": param["type"],
                "name": prettify_param_name(param["name"], param["type"]),
            }
        )

        type_text = prettify_param_type(param["type"])
        name_text = prettify_param_name(param["name"], param["type"])
        segments.append((f"{type_text}:", True))
        segments.append((f" {name_text}", False))

    formatted = "".join(text for text, _ in segments)
    return formatted, prettified_params, segments


def format_params(cmd: dict) -> tuple[str, list[dict]]:
    """
    Format parameters for display.

    Returns (formatted_string, list of {type, name} dicts for highlighting)
    """
    formatted, params, _ = build_param_segments(cmd)
    return formatted, params


def create_workbook():
    """Create and configure the workbook."""
    wb = xlsxwriter.Workbook(os.path.join(script_dir, "scrcmd_database.xlsx"))

    # Create formats dict
    formats = {}
    for section in ["header", "data"]:
        for col, style in s[section].items():
            key = f"{section}_{col}"
            formats[key] = wb.add_format(
                {
                    "font_name": style.get("font", {}).get("name", "Courier New"),
                    "font_size": style.get("font", {}).get("size", 11),
                    "bold": style.get("font", {}).get("bold", False),
                    "italic": style.get("font", {}).get("italic", False),
                    "bg_color": style.get("fill", {}).get("fg", "FFFFFF")[2:],
                    "text_wrap": style.get("align", {}).get("wrap", False),
                    "align": "left",
                    "valign": style.get("align", {}).get("vert", "top"),
                    "border": 1,
                    "border_color": "CCCCCC",
                }
            )

    # Add parameter type bold format
    formats["param_bold"] = wb.add_format(
        {
            "font_name": "Courier New",
            "font_size": DATA[4]["font"]["size"],
            "bold": True,
            "bg_color": DATA[4]["fill"]["fg"][2:],
            "valign": DATA[4]["align"]["vert"],
            "text_wrap": True,
            "border": 1,
            "border_color": "CCCCCC",
        }
    )

    # Add function column bold format
    formats["function_bold"] = wb.add_format(
        {
            "font_name": "Courier New",
            "font_size": DATA[5]["font"]["size"],
            "bold": True,
            "bg_color": DATA[5]["fill"]["fg"][2:],
            "valign": DATA[5]["align"]["vert"],
            "text_wrap": True,
            "border": 1,
            "border_color": "CCCCCC",
        }
    )

    return wb, formats


def write_header(ws, wb, header_spec: dict):
    """Write header row to worksheet."""
    ws.set_row(0, 30)
    for col, spec in header_spec.items():
        merged_format = wb.add_format(
            {
                "font_name": spec.get("font", {}).get("name", "Courier New"),
                "font_size": spec.get("font", {}).get("size", 11),
                "bold": spec.get("font", {}).get("bold", False),
                "italic": spec.get("font", {}).get("italic", False),
                "bg_color": spec.get("fill", {}).get("fg", "FFFFFF")[2:],
                "text_wrap": True,
                "align": "center",
                "valign": "center",
                "border": 1,
                "border_color": "CCCCCC",
            }
        )
        ws.write(0, col - 1, spec.get("value", ""), merged_format)


def write_scrcmd_sheet(wb, formats, name: str, commands: dict):
    """Write script commands worksheet."""
    ws = wb.add_worksheet(name)

    # Set column widths
    for col, width in CW.items():
        ws.set_column(col - 1, col - 1, width)

    write_header(ws, wb, HEADER)
    ws.freeze_panes(1, 3)

    # Filter and sort script commands by ID
    script_cmds = [
        (cmd_name, cmd_data)
        for cmd_name, cmd_data in commands.items()
        if cmd_data.get("type") == "script_cmd"
    ]
    script_cmds.sort(key=lambda x: x[1]["id"])

    row = 1
    for cmd_name, cmd in script_cmds:
        code = f"{cmd['id']:04X}"
        legacy_name = cmd.get("legacy_name", cmd_name)
        param_str, params, param_segments = build_param_segments(cmd)
        description = cmd.get("description", "")

        ws.write(row, 0, code, formats["data_1"])
        ws.write(row, 1, cmd_name, formats["data_2"])
        ws.write(row, 2, legacy_name, formats["data_3"])

        if param_segments:
            rich_parts = []
            for text, is_bold in param_segments:
                rich_parts.extend(
                    [formats["param_bold"] if is_bold else formats["data_4"], text]
                )

            if len(rich_parts) > 2:
                ws.write_rich_string(row, 3, *rich_parts, formats["data_4"])
            else:
                ws.write(row, 3, param_str, formats["data_4"])
        else:
            ws.write(row, 3, "", formats["data_4"])

        description_segments = build_description_segments(description, params)
        if description_segments and any(is_bold for _, is_bold in description_segments):
            rich_parts = []
            for text, is_bold in description_segments:
                rich_parts.extend(
                    [formats["function_bold"] if is_bold else formats["data_5"], text]
                )

            if len(rich_parts) > 2:
                ws.write_rich_string(row, 4, *rich_parts, formats["data_5"])
            else:
                ws.write(row, 4, description, formats["data_5"])
        else:
            ws.write(row, 4, description, formats["data_5"])

        row += 1

    return ws


def write_movement_sheet(wb, formats, commands: dict):
    """Write movements worksheet (Platinum only)."""
    ws = wb.add_worksheet("Movements")

    movement_columns = {1: CW[1], 2: CW[2], 3: CW[3], 4: CW[5], 5: CW[5]}
    for col, width in movement_columns.items():
        ws.set_column(col - 1, col - 1, width)

    movement_headers = {
        1: HEADER[1],
        2: HEADER[2],
        3: HEADER[3],
        4: {"value": "Function", "font": HEADER[4]["font"], "fill": HEADER[4]["fill"]},
        5: {"value": "Notes", "font": HEADER[5]["font"], "fill": HEADER[5]["fill"]},
    }
    write_header(ws, wb, movement_headers)
    ws.freeze_panes(1, 3)

    # Filter and sort movements by ID
    movements = [
        (name, data)
        for name, data in commands.items()
        if data.get("type") == "movement"
    ]
    movements.sort(key=lambda x: x[1]["id"])

    row = 1
    for mov_name, mov in movements:
        code = f"{mov['id']:04X}"
        legacy_name = mov.get("legacy_name", mov_name)
        description = mov.get("description", "")

        # Split description at semicolon
        if ";" in description:
            function, notes = description.split(";", 1)
            notes = notes.strip()
        else:
            function = description
            notes = ""

        ws.write(row, 0, code, formats["data_1"])
        ws.write(row, 1, mov_name, formats["data_2"])
        ws.write(row, 2, legacy_name, formats["data_3"])
        ws.write(row, 3, function, formats["data_4"])
        ws.write(row, 4, notes, formats["data_5"])

        row += 1

    return ws


def write_sound_sheet(wb, formats, name: str, sounds: dict):
    """Write sounds worksheet."""
    ws = wb.add_worksheet(name)

    sound_columns = {1: CW[1], 2: CW[3], 3: CW[5]}
    for col, width in sound_columns.items():
        ws.set_column(col - 1, col - 1, width)

    sound_headers = {
        1: {"value": "ID", "font": HEADER[1]["font"], "fill": HEADER[1]["fill"]},
        2: {"value": "Name", "font": HEADER[3]["font"], "fill": HEADER[3]["fill"]},
        3: {"value": "Used In", "font": HEADER[5]["font"], "fill": HEADER[5]["fill"]},
    }
    write_header(ws, wb, sound_headers)
    ws.freeze_panes(1, 1)

    # Sort sounds by ID (handle both string and int keys)
    sound_items = []
    for sound_id, info in sounds.items():
        try:
            numeric_id = int(sound_id)
        except ValueError:
            numeric_id = 0
        sound_items.append((sound_id, numeric_id, info))
    sound_items.sort(key=lambda x: x[1])

    row = 1
    for sound_id, _, info in sound_items:
        ws.write(row, 0, str(sound_id), formats["data_1"])
        ws.write(row, 1, info.get("name", ""), formats["data_3"])
        ws.write(row, 2, info.get("used_in", ""), formats["data_5"])
        row += 1

    return ws


def main():
    parent_dir = os.path.dirname(script_dir)

    # Find database files - prefer v2 format
    v2_files = sorted(glob.glob(os.path.join(parent_dir, "*_v2.json")))
    old_files = sorted(glob.glob(os.path.join(parent_dir, "*_scrcmd_database.json")))

    # Use v2 files if available, otherwise fall back to old format
    if v2_files:
        db_files = v2_files
        print(f"Using v2 format files: {len(v2_files)} found")
    else:
        db_files = old_files
        print(f"Using old format files: {len(old_files)} found")

    if not db_files:
        print("No database files found!")
        return 1

    wb, formats = create_workbook()
    platinum_commands = None
    platinum_sounds = None

    for path in db_files:
        # Extract game name from filename
        base = os.path.basename(path)
        if "_v2.json" in base:
            game_key = base.replace("_v2.json", "")
        else:
            game_key = base.replace("_scrcmd_database.json", "")

        sheet_name = NAME_MAP.get(game_key, game_key.upper())
        print(f"Processing {base} -> {sheet_name}")

        data = load_database(path)

        # Write script commands sheet
        write_scrcmd_sheet(wb, formats, sheet_name, data["commands"])

        # Store Platinum data for movements sheet
        if "platinum" in game_key.lower():
            platinum_commands = data["commands"]
            platinum_sounds = data.get("sounds", {})

    # Write Platinum movements (only once, not per-game)
    if platinum_commands:
        write_movement_sheet(wb, formats, platinum_commands)

    # Write sound sheets for each game
    for path in db_files:
        base = os.path.basename(path)
        if "_v2.json" in base:
            game_key = base.replace("_v2.json", "")
        else:
            game_key = base.replace("_scrcmd_database.json", "")

        sheet_name = f"Sounds {NAME_MAP.get(game_key, game_key.upper())}"

        data = load_database(path)
        sounds = data.get("sounds", {})
        if sounds:
            write_sound_sheet(wb, formats, sheet_name, sounds)

    wb.close()
    print(f"Excel generated: {os.path.join(script_dir, 'scrcmd_database.xlsx')}")
    return 0


if __name__ == "__main__":
    exit(main())
