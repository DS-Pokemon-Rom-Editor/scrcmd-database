#!/usr/bin/env python3
"""
Migration script for converting old scrcmd database format to new schema.

Old format: Parallel arrays with hex string keys, conditional commands marked with 255 prefix
New format: Decomp name as key, explicit variants for conditionals, proper type annotations
"""
import json
import re
import os
import argparse
from datetime import datetime, timezone


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


def is_placeholder_name(name: str) -> bool:
    """Check if a name is a placeholder (like ScrCmd_21D, scrcmd_465, or contains Unused)."""
    if not name:
        return True
    
    # Check for "Unused" in name
    if "unused" in name.lower():
        return True

    # Match patterns like ScrCmd_XXX, scrcmd_XXX, Dummy_XXX
    return bool(re.match(r'^(ScrCmd_|scrcmd_|Dummy)\w+$', name, re.IGNORECASE))


def get_best_name(data: dict) -> str:
    """Get the best name for a command, preferring meaningful names over placeholders."""
    decomp_name = data.get("decomp_name", "")
    legacy_name = data.get("name", "")
    
    # If decomp_name is a placeholder but legacy_name isn't, use legacy_name
    if is_placeholder_name(decomp_name) and not is_placeholder_name(legacy_name):
        return legacy_name
    
    # Otherwise prefer decomp_name
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
    parameters: list[int], 
    parameter_types: list[str],
    description: str
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
                    param_type = map_semantic_type(parameter_types[type_index], param_size)
                    type_index += 1
                else:
                    param_type = size_to_type(param_size)
                
                # Generate meaningful name based on type
                param_name = generate_param_name(param_type, j, param_count)
                
                variant_params.append({
                    "name": param_name,
                    "type": param_type
                })
        
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


def migrate_db(old_path: str, new_path: str) -> None:
    """Migrate old database format to new schema."""
    with open(old_path, 'r', encoding='utf-8') as f:
        old_data = json.load(f)

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
                
                params.append({
                    "name": name_str,
                    "type": type_str
                })
            
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

    # 5. Process lvlscrcmd (levelscript commands)
    # These define how scripts are triggered on a map.
    # Entries with valid type IDs go into commands with type="levelscript_cmd"
    # Structural entries (id=None) go into levelscript_meta
    levelscript_meta = {}
    
    for name, data in old_data.get("lvlscrcmd", {}).items():
        type_id = data.get("value")  # The type discriminant value
        discriminant_size = data.get("length")  # e.g., "u8", "u16", or None
        description = data.get("description", "")
        
        # Parse parameters
        sizes = data.get("parameters", [])
        types = data.get("parameter_types", [])
        params = []
        used_names = set()
        
        for i, size in enumerate(sizes):
            param_type = map_semantic_type(types[i] if i < len(types) else "", size)
            # Use better parameter names based on type and context
            if param_type == "var":
                param_name = "condition_var" if i == 0 else f"var_{i}"
            elif param_type == "u32" or param_type == "u16":
                # For levelscript entries, these are usually script IDs or values
                if i == len(sizes) - 1:
                    param_name = "script_id"
                elif i == 1 and len(sizes) == 3:
                    param_name = "check_value"
                else:
                    param_name = f"arg_{i}"
            else:
                param_name = f"arg_{i}"
            
            # Ensure unique names
            if param_name in used_names:
                param_name = f"{param_name}_{i}"
            used_names.add(param_name)
            
            params.append({
                "name": param_name,
                "type": param_type
            })
        
        # Entries with valid type IDs are commands
        if type_id is not None:
            entry = {
                "type": "levelscript_cmd",
                "id": type_id,
                "description": description,
            }
            # Add discriminant_size as metadata for tools that need it
            if discriminant_size:
                entry["discriminant_size"] = discriminant_size
            if params:
                entry["params"] = params
            else:
                entry["params"] = []
            
            new_commands[name] = entry
        else:
            # Structural entries (no type discriminant) go to meta
            entry = {
                "description": description,
            }
            if params:
                entry["params"] = params
            levelscript_meta[name] = entry

    # Build output with meta section
    game_version = extract_game_version(old_path)
    output = {
        "meta": {
            "version": game_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_from": os.path.basename(old_path)
        },
        "commands": new_commands,
        "sounds": sounds,
        "levelscript_meta": levelscript_meta,
        "comparison_operators": comparison_operators,
        "overworld_directions": overworld_directions,
        "special_overworlds": special_overworlds,
    }
    
    with open(new_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2)
    
    # Count by type
    type_counts = {}
    for cmd in new_commands.values():
        t = cmd.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    
    print(f"Migrated to {new_path}:")
    for t, count in sorted(type_counts.items()):
        print(f"  {t}: {count}")
    print(f"  sounds: {len(sounds)}")
    if levelscript_meta:
        print(f"  levelscript_meta: {len(levelscript_meta)}")
    if comparison_operators:
        print(f"  comparison_operators: {len(comparison_operators)}")
    if overworld_directions:
        print(f"  overworld_directions: {len(overworld_directions)}")
    if special_overworlds:
        print(f"  special_overworlds: {len(special_overworlds)}")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate old scrcmd database format to new schema"
    )
    parser.add_argument(
        "input",
        help="Input JSON file (old format)"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output JSON file (new format). Defaults to <input>_v2.json"
    )
    
    args = parser.parse_args()
    
    input_path = args.input
    if args.output:
        output_path = args.output
    else:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_v2{ext}"
    
    if not os.path.exists(input_path):
        print(f"Error: Input file not found: {input_path}")
        return 1
    
    migrate_db(input_path, output_path)
    return 0


if __name__ == "__main__":
    exit(main())
