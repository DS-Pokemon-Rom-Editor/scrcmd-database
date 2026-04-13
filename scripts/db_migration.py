#!/usr/bin/env python3
"""
Migrate every legacy scrcmd database in the repository to the v2 schema.

This script scans the repo root and `custom_databases/` for
`*_scrcmd_database.json` files and refreshes their sibling `*_v2.json` outputs.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def size_to_type(size: int) -> str:
    """Convert byte size to type string."""
    if size == 1:
        return "u8"
    if size == 2:
        return "u16"
    if size == 4:
        return "u32"
    return "u16"  # Fallback


def map_semantic_type(legacy_type: str, size: int) -> str:
    """Maps old semantic types + size to new concise types."""
    legacy_type = legacy_type.strip() if legacy_type else ""

    if legacy_type == "Variable":
        return "var"
    if legacy_type == "Flag":
        return "flag"
    if legacy_type == "Text":
        return "msg_id"
    if legacy_type == "Movement":
        return "movement_id"
    if legacy_type == "Script":
        return "script_id"

    # Integers depend on size
    if legacy_type == "Integer" or not legacy_type:
        return size_to_type(size)

    return "u16"  # Fallback

def get_best_name(data: dict) -> str:
    """Get the best name for a command, preferring the decomp name when present."""
    decomp_name = data.get("decomp_name", "")
    legacy_name = data.get("name", "")

    if decomp_name:
        return decomp_name

    return legacy_name or "Unknown"


def generate_param_name(semantic_type: str, index: int, total: int) -> str:
    """Generate a meaningful parameter name based on its type and position."""
    type_names = {
        "var": "result" if total == 1 else f"var_{index}",
        "flag": "flag_id",
        "msg_id": "message_id",
        "movement_id": "movement",
        "script_id": "script",
    }

    if semantic_type in type_names:
        base = type_names[semantic_type]
        # If we have multiple of the same type, add index
        if total > 1 and semantic_type == "var":
            return f"var_{index}"
        return base

    # For numeric types, use generic names
    return f"arg_{index}"


def parse_param_name_from_value(param_value: str) -> tuple[str, str]:
    """
    Parse parameter name and type from parameter_values entry.
    Format: "u16: Time" -> ("time", "u16")
    Format: "Var: Countdown Variable" -> ("countdown_variable", "var")
    """
    if not param_value or ":" not in param_value:
        return ("arg", "u16")

    # Skip documentation-style entries
    if "Command format depends" in param_value or "\n" in param_value:
        return ("mode", "u8")

    parts = param_value.split(":", 1)
    type_str = parts[0].strip().lower()
    name = parts[1].strip() if len(parts) > 1 else "arg"

    # Normalize type
    if type_str == "var":
        type_str = "var"
    elif type_str in ("u8", "u16", "u32", "fx32"):
        pass  # Keep as-is
    else:
        type_str = "u16"  # Default

    # Convert name to snake_case
    name = name.replace(" ", "_").lower()

    return (name, type_str)


def parse_conditional_parameters(
    parameters: list[int], parameter_types: list[str], description: str
) -> list[dict]:
    """
    Parse the 255-prefixed conditional parameter format into variants.

    Format: [255, discriminant_size, (value, param_count, param_sizes...), ...]
    Example: [255, 1, 0, 0, 1, 0, 2, 1, 2]
             255 = marker
             1 = discriminant is 1 byte (u8)
             0, 0 = if mode=0, 0 additional params
             1, 0 = if mode=1, 0 additional params
             2, 1, 2 = if mode=2, 1 additional param of size 2 (u16)

    The parameter_types array contains types for ALL params across ALL variants,
    starting with the discriminant type (usually "Integer").
    """
    if not parameters or parameters[0] != 255:
        return []

    # Parse mode descriptions from description text
    # Format: "0: Disables Strength...\n1: Allows...\n2: Checks..."
    mode_descriptions = {}
    for line in description.split("\n"):
        match = re.match(r"(\d+):\s*(.+)", line.strip())
        if match:
            mode_descriptions[match.group(1)] = match.group(2)

    variants = []
    discriminant_size = parameters[1]
    discriminant_type = size_to_type(discriminant_size)

    # Track position in parameter_types array
    # Skip index 0 (discriminant type) - we handle it specially
    type_index = 1

    i = 2  # Start after [255, size]
    while i < len(parameters):
        mode_value = parameters[i]
        param_count = parameters[i + 1] if i + 1 < len(parameters) else 0

        variant_params = [
            {"name": "mode", "type": discriminant_type, "const": str(mode_value)}
        ]

        # Read additional parameter sizes and assign types from parameter_types
        for j in range(param_count):
            if i + 2 + j < len(parameters):
                param_size = parameters[i + 2 + j]

                # Get semantic type from parameter_types if available
                if type_index < len(parameter_types):
                    param_type = map_semantic_type(
                        parameter_types[type_index], param_size
                    )
                    type_index += 1
                else:
                    param_type = size_to_type(param_size)

                # Generate meaningful name based on type
                param_name = generate_param_name(param_type, j, param_count)

                variant_params.append({"name": param_name, "type": param_type})

        # Build variant with description if available
        variant = {"params": variant_params}
        if str(mode_value) in mode_descriptions:
            variant["desc"] = mode_descriptions[str(mode_value)]

        variants.append(variant)

        # Move to next variant
        i += 2 + param_count

    return variants


def extract_game_version(filename: str) -> str:
    """Extract game version from filename."""
    basename = os.path.basename(filename).lower()

    if "platinum" in basename:
        return "Platinum"
    if "diamond" in basename or "pearl" in basename:
        return "Diamond/Pearl"
    if "hgss" in basename:
        return "HeartGold/SoulSilver"

    # Try to extract from pattern like "xxx_scrcmd_database.json"
    match = re.match(r"(.+?)_scrcmd_database\.json", basename)
    if match:
        return match.group(1).replace("_", " ").title()

    return "Unknown"


def build_migrated_output(old_path: str, old_data: dict) -> dict:
    """Build migrated v2 data for one legacy database."""
    new_commands = {}

    # 1. Process Script Commands
    for hex_id, data in old_data.get("scrcmd", {}).items():
        name = get_best_name(data)
        legacy_name = data.get("name")
        cmd_id = int(hex_id, 16)
        description = data.get("description", "")

        sizes = data.get("parameters", [])
        types = data.get("parameter_types", [])
        param_values = data.get("parameter_values", [])

        entry = {
            "type": "script_cmd",
            "id": cmd_id,
            "legacy_name": legacy_name,
            "description": description,
        }

        # Check if this is a conditional command
        if sizes and sizes[0] == 255:
            variants = parse_conditional_parameters(sizes, types, description)
            entry["variants"] = variants
        else:
            # Simple command - parse parameters
            params = []

            # Try to get names from parameter_values first
            for i, size in enumerate(sizes):
                if i < len(param_values):
                    name_str, type_str = parse_param_name_from_value(param_values[i])
                else:
                    name_str = f"arg_{i}"
                    type_str = None

                # Fall back to parameter_types for semantic type
                if not type_str or type_str == "u16":
                    if i < len(types):
                        type_str = map_semantic_type(types[i], size)
                    else:
                        type_str = size_to_type(size)

                # Generate better name if still generic
                if name_str.startswith("arg_"):
                    name_str = generate_param_name(type_str, i, len(sizes))

                params.append({"name": name_str, "type": type_str})

            entry["params"] = params

        new_commands[name] = entry

    # 2. Process Movements
    # Movement names may collide with script command names (e.g. "End" exists in both).
    # Decomps solve this by prefixing movements (e.g. "EndMovement" in movement.inc).
    # We check for collisions and prefix with "Movement" if needed.
    for hex_id, data in old_data.get("movements", {}).items():
        name = get_best_name(data)

        # Handle known naming collisions with script commands
        if name in new_commands:
            name = f"{name}Movement"

        entry = {
            "type": "movement",
            "id": int(hex_id, 16),
            "legacy_name": data.get("name"),
        }

        # Only add description if non-empty
        desc = data.get("description", "")
        if desc:
            entry["description"] = desc

        new_commands[name] = entry

    # 3. Process Sounds (keyed by integer ID)
    sounds = {}
    for sound_id, data in old_data.get("sounds", {}).items():
        entry = {
            "name": data.get("name", f"SOUND_{sound_id}"),
        }

        # Only add used_in if non-empty
        used_in = data.get("used_in", "")
        if used_in:
            entry["used_in"] = used_in

        sounds[int(sound_id)] = entry

    # 4. Process lookup tables (convert hex string keys to integers)

    comparison_operators = {}
    for hex_id, value in old_data.get("comparisonOperators", {}).items():
        comparison_operators[int(hex_id, 16)] = value

    overworld_directions = {}
    for hex_id, value in old_data.get("overworldDirections", {}).items():
        overworld_directions[int(hex_id, 16)] = value

    special_overworlds = {}
    for hex_id, value in old_data.get("specialOverworlds", {}).items():
        special_overworlds[int(hex_id, 16)] = value

    # Build output with meta section
    game_version = extract_game_version(old_path)
    output = {
        "meta": {
            "version": game_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_from": os.path.basename(old_path),
        },
        "commands": new_commands,
        "sounds": sounds,
        "comparison_operators": comparison_operators,
        "overworld_directions": overworld_directions,
        "special_overworlds": special_overworlds,
    }

    return output


def count_command_types(commands: dict) -> dict[str, int]:
    """Count commands by type for status output."""
    type_counts: dict[str, int] = {}
    for cmd in commands.values():
        cmd_type = cmd.get("type", "unknown")
        type_counts[cmd_type] = type_counts.get(cmd_type, 0) + 1
    return type_counts


def strip_generated_at(data: dict) -> dict:
    """Normalize generated output for equality checks."""
    normalized = json.loads(json.dumps(data))
    normalized.get("meta", {}).pop("generated_at", None)
    return normalized


def migrate_db(old_path: str | Path, new_path: str | Path) -> bool:
    """Migrate one legacy database and write the v2 output if it changed."""
    old_path = Path(old_path)
    new_path = Path(new_path)

    with open(old_path, "r", encoding="utf-8") as f:
        old_data = json.load(f)

    output = build_migrated_output(str(old_path), old_data)
    type_counts = count_command_types(output["commands"])

    if new_path.exists():
        with open(new_path, "r", encoding="utf-8") as f:
            existing_output = json.load(f)

        if strip_generated_at(existing_output) == strip_generated_at(output):
            print(f"Up to date: {new_path}")
            return False

    with open(new_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Migrated {old_path} -> {new_path}:")
    for t, count in sorted(type_counts.items()):
        print(f"  {t}: {count}")
    print(f"  sounds: {len(output['sounds'])}")
    if output["comparison_operators"]:
        print(f"  comparison_operators: {len(output['comparison_operators'])}")
    if output["overworld_directions"]:
        print(f"  overworld_directions: {len(output['overworld_directions'])}")
    if output["special_overworlds"]:
        print(f"  special_overworlds: {len(output['special_overworlds'])}")
    return True


def get_repo_root() -> Path:
    """Return the repository root."""
    return Path(__file__).resolve().parent.parent


def find_legacy_database_paths() -> list[Path]:
    """Find every legacy database that should be migrated."""
    repo_root = get_repo_root()
    paths = sorted(repo_root.glob("*_scrcmd_database.json"))

    custom_db_dir = repo_root / "custom_databases"
    if custom_db_dir.is_dir():
        paths.extend(sorted(custom_db_dir.rglob("*_scrcmd_database.json")))

    return paths


def legacy_to_v2_path(old_path: Path) -> Path:
    """Map `*_scrcmd_database.json` to the sibling `*_v2.json` path."""
    if not old_path.name.endswith("_scrcmd_database.json"):
        raise ValueError(f"Unexpected legacy database name: {old_path}")

    return old_path.with_name(
        old_path.name.removesuffix("_scrcmd_database.json") + "_v2.json"
    )


def main() -> int:
    if len(sys.argv) > 1:
        print("db_migration.py no longer accepts arguments.")
        print("Run `python scripts/db_migration.py` to migrate every legacy database.")
        return 2

    legacy_paths = find_legacy_database_paths()
    if not legacy_paths:
        print("No *_scrcmd_database.json files found in repository")
        return 1

    changed_count = 0
    for old_path in legacy_paths:
        if migrate_db(old_path, legacy_to_v2_path(old_path)):
            changed_count += 1

    print(
        f"\nProcessed {len(legacy_paths)} legacy database(s); "
        f"{changed_count} file(s) changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
