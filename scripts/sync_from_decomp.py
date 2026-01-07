#!/usr/bin/env python3
"""
Sync database with decomp project definitions.

Fetches .inc macro files from pret decomp repos and compares/updates
the v2 database with canonical decomp names, opcodes, and parameter info.

Handles:
- Simple commands with fixed opcodes
- Conditional commands (.if/.else based on parameter values)
- Opcode-switching commands (different opcodes based on param range)
- Wrapper macros (macros that call other macros)
- Optional parameters (.ifnb)

Usage:
    python sync_from_decomp.py platinum_v2.json
    python sync_from_decomp.py platinum_v2.json --update
    python sync_from_decomp.py --all
    python sync_from_decomp.py --dump platinum  # Dump parsed macros
"""
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.request import urlopen
from urllib.error import URLError


# Decomp repo URLs for each game
DECOMP_SOURCES = {
    "Platinum": {
        "scrcmd": "https://raw.githubusercontent.com/pret/pokeplatinum/main/asm/macros/scrcmd.inc",
        "movement": "https://raw.githubusercontent.com/pret/pokeplatinum/main/asm/macros/movement.inc",
    },
    "HeartGold/SoulSilver": {
        "scrcmd": "https://raw.githubusercontent.com/pret/pokeheartgold/master/asm/macros/script.inc",
        # HGSS doesn't have a separate movement.inc - movements use raw constants
    },
    # Diamond/Pearl decomp isn't as mature, skip for now
}

# Helper/utility macro names to skip (they don't emit actual script commands)
SKIP_MACROS = {
    'scrdef', 'map_script', 'ScriptEntry', 'save_game_normal', 'script_entry',
    'script_entry_fixed', 'script_entry_go_to_if_equal',
}

# Levelscript macros - these define how scripts are triggered on a map
# We parse these specially since they don't have numeric opcodes like regular commands
LEVELSCRIPT_MACROS = {
    'InitScriptEntry_Fixed',
    'InitScriptEntry_OnFrameTable',
    'InitScriptEntry_OnTransition',
    'InitScriptEntry_OnResume',
    'InitScriptEntry_OnLoad',
    'InitScriptEntryEnd',
    'InitScriptGoToIfEqual',
    'InitScriptFrameTableEnd',
    'InitScriptEnd',
}


@dataclass
class MacroParam:
    """A macro parameter definition."""
    name: str
    default: str | None = None


@dataclass 
class Variant:
    """A conditional variant of a command."""
    condition: str  # e.g., "mode == 2", "arg0 <= 3"
    params_emitted: list[str] = field(default_factory=list)  # param names emitted in this branch
    

@dataclass
class MacroExpansion:
    """How a wrapper macro expands to another macro."""
    target_macro: str  # The macro being called
    args: list[str]  # Arguments passed (can be constants or param references)


@dataclass
class ParsedMacro:
    """A fully parsed macro definition."""
    name: str
    params: list[MacroParam]
    opcodes: list[int]  # Can have multiple if opcode-switching
    is_conditional: bool = False
    variants: list[Variant] = field(default_factory=list)
    opcode_switches: list[tuple[str, int]] = field(default_factory=list)
    emitted_params: list[str] = field(default_factory=list)
    expansion: MacroExpansion | None = None  # If this is a wrapper macro
    body: str = ""  # Raw body for debugging
    
    @property
    def is_wrapper(self) -> bool:
        return self.expansion is not None


@dataclass
class LevelscriptMacro:
    """A parsed levelscript macro definition."""
    name: str
    params: list[MacroParam]
    type_id: int | None = None  # The INIT_SCRIPT_* constant value if fixed type
    emits: list[str] = field(default_factory=list)  # What it emits: [".byte", ".short", etc.]
    is_wrapper: bool = False  # If it wraps another macro
    wrapper_target: str | None = None  # Target macro if wrapper


def is_placeholder_name(name: str) -> bool:
    """Check if a name is a placeholder (like ScrCmd_21D, scrcmd_465, or contains Unused)."""
    if not name:
        return True
    
    # Check for "Unused" in name
    if "unused" in name.lower():
        return True

    # Match patterns like ScrCmd_XXX, scrcmd_XXX, Dummy_XXX, CMD_XXX
    return bool(re.match(r'^(ScrCmd_|scrcmd_|Dummy|CMD_)\w+$', name, re.IGNORECASE))


def fetch_url(url: str) -> str | None:
    """Fetch content from URL, return None on error."""
    try:
        with urlopen(url, timeout=30) as response:
            return response.read().decode('utf-8')
    except URLError as e:
        print(f"  Warning: Failed to fetch {url}: {e}")
        return None


def extract_macros(content: str) -> list[tuple[str, str, str]]:
    """
    Extract all macro definitions from content.
    
    Returns list of (name, params_str, body) tuples.
    """
    # Pattern to match .macro Name [params] ... .endm
    # Use [ \t]* instead of \s* to avoid consuming newlines before params
    macro_pattern = re.compile(
        r'\.macro\s+(\w+)[ \t]*([^\n]*)\n(.*?)\.endm',
        re.MULTILINE | re.DOTALL
    )
    
    macros = []
    for match in macro_pattern.finditer(content):
        name = match.group(1)
        params_str = match.group(2).strip()
        body = match.group(3)
        macros.append((name, params_str, body))
    
    return macros


def parse_params(params_str: str) -> list[MacroParam]:
    """Parse macro parameter string into list of MacroParam.
    
    Handles:
    - "param1, param2" (comma-separated)
    - "param1 param2" (space-separated)
    - "name = default" (default values)
    - "name=default" (default values, no spaces)
    - Strip comments starting with ;
    """
    if not params_str:
        return []
    
    # Strip comments
    if ';' in params_str:
        params_str = params_str.split(';', 1)[0]
    
    params_str = params_str.strip()
    if not params_str:
        return []
    
    params = []
    
    # Split by comma if present, otherwise by whitespace
    if ',' in params_str:
        parts = [p.strip() for p in params_str.split(',')]
    else:
        parts = params_str.split()
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # Handle both "name=value" and "name = value" formats
        if '=' in part:
            name, default = part.split('=', 1)
            params.append(MacroParam(name.strip(), default.strip()))
        else:
            # Simple parameter without default
            params.append(MacroParam(part))
    
    return params


def extract_emitted_params(body: str) -> list[str]:
    """
    Extract list of parameter names emitted by the macro, in order.
    Returns empty list if macro contains conditionals or complex logic.
    """
    params = []
    lines = body.split('\n')
    for line in lines:
        line = line.strip()
        if not line or line.startswith((';', '/*', '@', '#')):
            continue
            
        # Abort on conditionals as flow is ambiguous
        if line.startswith(('.if', '.else', '.endif', '.macro', '.endm')):
            return []
            
        # Match .directive \param
        # We look for backslash followed by word char
        match = re.search(r'\.(?:short|2byte|hword|byte|word|long)\s+(?:.*\\(\w+))', line)
        if match:
            params.append(match.group(1))
            
    return params


def extract_opcodes(body: str) -> list[int]:
    """Extract all numeric opcode emissions from macro body."""
    # Match .short/.byte/.2byte/.hword followed by numeric value
    # Must be at the start of emissions (first .short is the opcode)
    opcode_pattern = re.compile(r'\.(?:short|2byte|hword)\s+(\d+)')
    
    opcodes = []
    for match in opcode_pattern.finditer(body):
        opcodes.append(int(match.group(1)))
    
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for op in opcodes:
        if op not in seen:
            seen.add(op)
            unique.append(op)
    
    return unique


def extract_first_opcode(body: str) -> int | None:
    """Extract the first opcode emission (before any conditionals)."""
    lines = body.split('\n')
    for line in lines:
        line = line.strip()
        # Skip comments
        if line.startswith(';') or line.startswith('/*'):
            continue
        # Check for .if before finding opcode
        if line.startswith('.if'):
            return None  # Opcode is inside conditional
        # Look for opcode emission
        match = re.match(r'\.(?:short|2byte|hword)\s+(\d+)', line)
        if match:
            return int(match.group(1))
    return None


def detect_opcode_switching(body: str) -> list[tuple[str, int]]:
    """
    Detect if macro emits different opcodes based on conditions.
    
    Returns list of (condition, opcode) pairs.
    """
    switches = []

    # Pattern: .if CONDITION followed by .short OPCODE
    if_pattern = re.compile(
        r'\.if\s+(.+?)\n\s*\.(?:short|2byte|hword)\s+(\d+)\s*/\*\s*(\w+)',
        re.MULTILINE
    )
    
    for match in if_pattern.finditer(body):
        condition = match.group(1).strip()
        opcode = int(match.group(2))
        switches.append((condition, opcode))
    
    # Pattern: .else followed by .short OPCODE
    else_pattern = re.compile(
        r'\.else\s*\n\s*\.(?:short|2byte|hword)\s+(\d+)\s*/\*\s*(\w+)',
        re.MULTILINE
    )
    
    for match in else_pattern.finditer(body):
        opcode = int(match.group(1))
        switches.append(("else", opcode))

    return switches


def parse_conditionals(body: str, params: list[MacroParam]) -> list[Variant]:
    r"""
    Parse conditional parameter emission patterns.
    
    Handles patterns like:
        .if \mode == 2
            .short \checkDestVarID
        .endif
    """
    variants = []
    
    # Find all .if blocks (including nested)
    # Simplified: just find top-level .if conditions and what params they emit
    
    # Pattern for .if CONDITION ... .endif blocks
    if_pattern = re.compile(
        r'\.if\s+(.+?)\n(.*?)(?:\.else|\.endif)',
        re.MULTILINE | re.DOTALL
    )
    
    for match in if_pattern.finditer(body):
        condition = match.group(1).strip()
        block = match.group(2)
        
        # Skip opcode-switching conditions (those emit .short with numeric + comment)
        if re.search(r'\.short\s+\d+\s*/\*', block):
            continue
        
        # Find params emitted in this block
        emitted = re.findall(r'\.(?:short|byte)\s+\\(\w+)', block)
        
        if emitted:
            variants.append(Variant(condition=condition, params_emitted=emitted))
    
    return variants


def detect_wrapper_macro(body: str, all_macro_names: set[str]) -> MacroExpansion | None:
    """
    Detect if this macro just calls another macro (wrapper/convenience macro).
    
    Returns MacroExpansion with target and args, or None.
    """
    lines = [l.strip() for l in body.split('\n') if l.strip() and not l.strip().startswith(';')]
    
    # If body is just one line that calls another macro
    if len(lines) == 1:
        line = lines[0]
        # Check if line starts with a known macro name
        for macro_name in all_macro_names:
            if line.startswith(macro_name + ' ') or line.startswith(macro_name + ',') or line == macro_name:
                # Extract arguments
                if ' ' in line:
                    args_str = line[len(macro_name):].strip()
                    # Split on comma, handling backslash-prefixed params
                    args = [a.strip() for a in args_str.split(',') if a.strip()]
                else:
                    args = []
                return MacroExpansion(target_macro=macro_name, args=args)
    
    return None


def parse_macro(name: str, params_str: str, body: str, all_macro_names: set[str]) -> ParsedMacro | None:
    """Parse a single macro definition into a ParsedMacro object."""
    if name in SKIP_MACROS:
        return None
    
    params = parse_params(params_str)
    
    # Check if it's a wrapper macro first
    expansion = detect_wrapper_macro(body, all_macro_names)
    if expansion:
        return ParsedMacro(
            name=name,
            params=params,
            opcodes=[],
            expansion=expansion,
            body=body.strip()
        )
    
    # Get first opcode (before any conditionals)
    first_opcode = extract_first_opcode(body)
    
    # Check for opcode-switching
    opcode_switches = detect_opcode_switching(body)
    
    if opcode_switches:
        # This macro can emit different opcodes
        opcodes = [op for _, op in opcode_switches]
        if first_opcode and first_opcode not in opcodes:
            opcodes.insert(0, first_opcode)
        return ParsedMacro(
            name=name,
            params=params,
            opcodes=opcodes,
            opcode_switches=opcode_switches,
            is_conditional=True,
            body=body.strip()
        )
    
    # Check for conditional parameter emission
    variants = parse_conditionals(body, params)
    
    opcodes = [first_opcode] if first_opcode is not None else extract_opcodes(body)[:1]
    
    if not opcodes:
        # No numeric opcode found - might use symbolic constant
        return None
    
    emitted_params = extract_emitted_params(body)
    
    return ParsedMacro(
        name=name,
        params=params,
        opcodes=opcodes,
        is_conditional=bool(variants),
        variants=variants,
        emitted_params=emitted_params,
        body=body.strip()
    )


def parse_scrcmd_inc(content: str) -> dict[str, ParsedMacro]:
    """Parse scrcmd.inc into dict of name -> ParsedMacro."""
    raw_macros = extract_macros(content)
    all_names = {name for name, _, _ in raw_macros}
    
    parsed = {}
    for name, params_str, body in raw_macros:
        macro = parse_macro(name, params_str, body, all_names)
        if macro:
            parsed[name] = macro
    
    return parsed


def parse_movement_inc(content: str) -> dict[str, int | None]:
    r"""
    Parse movement.inc to extract movement macro names.
    
    Movements may use symbolic constants (MOVEMENT_ACTION_*) instead of
    numeric literals, so we extract macro names and optionally resolve them.
    
    Returns dict of name -> opcode (or None if symbolic).
    """
    movements = {}
    
    # Pattern: .macro Name followed by .short/.byte VALUE
    macro_pattern = re.compile(
        r'\.macro\s+(\w+).*?\n\s*\.(?:byte|short|hword)\s+(\S+)',
        re.MULTILINE | re.DOTALL
    )
    
    for match in macro_pattern.finditer(content):
        name = match.group(1)
        value_str = match.group(2)
        
        # Try to parse as numeric
        try:
            if value_str.startswith('0x'):
                opcode = int(value_str, 16)
            else:
                opcode = int(value_str)
            movements[name] = opcode
        except ValueError:
            # Symbolic constant - store None for now
            movements[name] = None
    
    return movements


def parse_levelscript_macros(content: str) -> dict[str, LevelscriptMacro]:
    """
    Parse levelscript-related macros from scrcmd.inc.
    
    These macros define how scripts are triggered on map load/transition.
    Unlike regular script commands, they use symbolic constants for type IDs.
    
    Returns dict of name -> LevelscriptMacro.
    """
    raw_macros = extract_macros(content)
    
    # Map of INIT_SCRIPT_* constants to their values
    # These are defined in constants/init_script_types.h in the decomp
    init_script_types = {
        'INIT_SCRIPT_ON_FRAME_TABLE': 1,
        'INIT_SCRIPT_ON_TRANSITION': 2,
        'INIT_SCRIPT_ON_RESUME': 3,
        'INIT_SCRIPT_ON_LOAD': 4,
    }
    
    results = {}
    
    for name, params_str, body in raw_macros:
        if name not in LEVELSCRIPT_MACROS:
            continue
        
        # For levelscript macros, we need cleaner param parsing
        # Filter out .set directives that might appear on the same line
        clean_params_str = params_str.strip()
        if clean_params_str.startswith('.'):
            # This is a directive, not params
            clean_params_str = ""
        
        params = parse_params(clean_params_str)
        
        # Check if it's a wrapper macro (single line calling another macro)
        lines = [l.strip() for l in body.split('\n') 
                 if l.strip() and not l.strip().startswith(('.if', '.else', '.endif', '.error', '.set'))]
        
        is_wrapper = False
        wrapper_target = None
        type_id = None
        emits = []
        
        # Check for wrapper pattern (calls another InitScript macro)
        if len(lines) == 1:
            line = lines[0]
            for target in LEVELSCRIPT_MACROS:
                if line.startswith(target + ' ') or line.startswith(target + ','):
                    is_wrapper = True
                    wrapper_target = target
                    break
        
        # Parse what this macro emits
        for line in body.split('\n'):
            line = line.strip()
            if line.startswith('.byte'):
                emits.append('.byte')
                # Check for type constant
                match = re.search(r'\.byte\s+(INIT_SCRIPT_\w+|\d+|\\?\w+)', line)
                if match:
                    val = match.group(1)
                    if val in init_script_types:
                        type_id = init_script_types[val]
                    elif val.isdigit():
                        type_id = int(val)
            elif line.startswith('.short'):
                emits.append('.short')
            elif line.startswith('.long'):
                emits.append('.long')
        
        results[name] = LevelscriptMacro(
            name=name,
            params=params,
            type_id=type_id,
            emits=emits,
            is_wrapper=is_wrapper,
            wrapper_target=wrapper_target
        )
    
    return results


def compare_levelscript_with_db(
    db: dict,
    decomp_macros: dict[str, LevelscriptMacro]
) -> tuple[list, list, list]:
    """
    Compare levelscript macros with database.
    
    Returns:
        - missing: Macros in decomp but not in database
        - mismatched: Macros with wrong type IDs or params
        - corrections: Suggested corrections for database
    """
    db_commands = db.get("commands", {})
    db_meta = db.get("levelscript_meta", {})
    
    missing = []
    mismatched = []
    corrections = []
    
    for name, macro in decomp_macros.items():
        # Check in commands (for type_id != None) or meta
        in_commands = name in db_commands and db_commands[name].get("type") == "levelscript_cmd"
        in_meta = name in db_meta
        
        if macro.is_wrapper:
            # Wrapper macros may or may not need to be in DB
            continue
        
        if not in_commands and not in_meta:
            missing.append({
                "name": name,
                "type_id": macro.type_id,
                "emits": macro.emits,
                "params": [p.name for p in macro.params]
            })
            continue
        
        # Check for mismatches
        if in_commands:
            db_entry = db_commands[name]
            db_type_id = db_entry.get("id")
            db_params = db_entry.get("params", [])
            
            # Check type ID
            if macro.type_id is not None and db_type_id != macro.type_id:
                mismatched.append({
                    "name": name,
                    "issue": "type_id",
                    "decomp": macro.type_id,
                    "db": db_type_id
                })
            
            # Check param count
            # Count params (excluding wrapper target args)
            decomp_param_count = len([p for p in macro.params if not p.name.startswith('\\')])
            db_param_count = len(db_params)
            
            # Special case: if decomp emits nothing but DB has params, it's wrong
            if len(macro.emits) == 0 and db_param_count > 0:
                corrections.append({
                    "name": name,
                    "issue": "has_params",
                    "current": db_param_count,
                    "should_be": 0,
                    "reason": "Decomp macro emits no data"
                })
            elif len(macro.emits) == 1 and macro.emits[0] == '.short' and db_param_count > 0:
                # Just emits .short 0 or similar (no params)
                # Check if macro has no real params
                if len(macro.params) == 0:
                    corrections.append({
                        "name": name,
                        "issue": "has_params", 
                        "current": db_param_count,
                        "should_be": 0,
                        "reason": "Decomp macro just emits a constant value"
                    })
    
    return missing, mismatched, corrections


def parse_macro_expansion_lines(body: str) -> list[str]:
    """
    Parse macro body into expansion lines (calls to other macros).
    
    Filters out directives (.short, .byte, .if, etc.) and returns
    only the macro call lines.
    """
    lines = []
    for line in body.split('\n'):
        line = line.strip()
        # Skip empty lines, comments, and assembler directives
        if not line:
            continue
        if line.startswith(('.', ';', '/*', '@', '#')):
            continue
        # Skip .ifnb blocks content for now (optional params)
        if '\\' in line or line[0].isupper():
            lines.append(line)
    return lines


def format_expansion_line(line: str, params: list[MacroParam]) -> str:
    """
    Convert a macro call line to expansion format with $param syntax.
    
    Input:  "CompareVar \\varID, \\valueOrVarID"
    Output: "CompareVar $varID, $valueOrVarID"
    """
    result = line
    # Replace \param with $param
    for p in params:
        result = result.replace(f'\\{p.name}', f'${p.name}')
    # Also handle any remaining backslash-prefixed identifiers
    import re
    result = re.sub(r'\\([a-zA-Z_][a-zA-Z0-9_]*)', r'$\1', result)
    return result


def infer_param_type(name: str, context: str = "") -> str:
    """Infer parameter type from name and context."""
    name_lower = name.lower()
    
    if 'var' in name_lower:
        return 'var'
    if 'flag' in name_lower:
        return 'flag'
    if 'offset' in name_lower or 'label' in name_lower or 'dest' in name_lower:
        return 'label'
    if 'message' in name_lower or 'msg' in name_lower:
        return 'msg_id'
    if 'script' in name_lower:
        return 'script_id'
    if 'species' in name_lower:
        return 'species'
    if 'item' in name_lower:
        return 'item'
    if 'map' in name_lower:
        return 'map_id'
    if 'trainer' in name_lower:
        return 'trainer_id'
    
    return 'u16'


def extract_macros_for_db(content: str, id_to_name: dict[int, str] | None = None) -> dict[str, dict]:
    """
    Extract convenience macros from decomp and format for v2 database.
    
    Returns dict of macro_name -> macro entry in v2 schema format.
    Handles standard macros and opcode-switching conditional macros.
    """
    parsed_macros = parse_scrcmd_inc(content)
    macros = {}
    
    for name, macro in parsed_macros.items():
        if name in SKIP_MACROS or name in LEVELSCRIPT_MACROS:
            continue
        
        v2_params = []
        for p in macro.params:
            v2_params.append({
                "name": p.name,
                "type": infer_param_type(p.name),
                **({"default": p.default} if p.default else {})
            })
            
        # 1. Handle Opcode Switchers (Conditional Macros)
        if macro.opcode_switches and id_to_name:
            variants = []
            for cond, opcode in macro.opcode_switches:
                target_cmd = id_to_name.get(opcode, f"UnkCmd_{opcode:04X}")
                # Heuristic: Pass all params to the target command
                args = ", ".join(f"${p.name}" for p in macro.params)
                variants.append({
                    "condition": cond,
                    "expansion": [f"{target_cmd} {args}"]
                })
            
            macros[name] = {
                "type": "macro",
                "params": v2_params,
                "variants": variants
            }
            continue
        
        # 2. Handle Standard Macros (Expansion Lines)
        expansion_lines = parse_macro_expansion_lines(macro.body)
        
        if not expansion_lines:
            continue
        
        # Skip if first line emits .short (it's a real command with opcode),
        # unless it also calls other macros (mixed content)
        if any(line.strip().startswith('.short') for line in macro.body.split('\n')[:5]):
            has_macro_calls = any(
                line.strip() and 
                not line.strip().startswith('.') and 
                line.strip()[0].isupper()
                for line in macro.body.split('\n')
            )
            if not has_macro_calls:
                continue
        
        v2_expansion = [format_expansion_line(line, macro.params) for line in expansion_lines]
        
        if v2_expansion:
            macros[name] = {
                "type": "macro",
                "params": v2_params,
                "expansion": v2_expansion
            }
    
    return macros


def inject_macros_into_db(db_path: str, verbose: bool = False) -> int:
    """
    Fetch macros from decomp and inject into v2 database file.
    
    Returns number of macros added/updated.
    """
    with open(db_path, 'r', encoding='utf-8') as f:
        db = json.load(f)
    
    version = get_game_version(db)
    if version not in DECOMP_SOURCES:
        print(f"  Skipping: No decomp source configured for {version}")
        return 0
    
    sources = DECOMP_SOURCES[version]
    if "scrcmd" not in sources:
        print(f"  Skipping: No scrcmd source for {version}")
        return 0
    
    # Build id_to_name map for resolving opcodes in conditional macros
    commands = db.get("commands", {})
    id_to_name = build_id_to_name_map(commands, "script_cmd")
    
    print(f"  Fetching macros from decomp for {version}...")
    content = fetch_url(sources["scrcmd"])
    if not content:
        return 0
    
    macros = extract_macros_for_db(content, id_to_name)
    print(f"  Extracted {len(macros)} macros from decomp")
    
    # Add/update macros in commands section
    # commands = db.get("commands", {}) # Already got above
    added = 0
    updated = 0
    
    for name, macro_data in macros.items():
        if name in commands:
            if commands[name].get("type") == "macro":
                # Update existing macro
                commands[name] = macro_data
                updated += 1
            # else: skip - don't overwrite real commands with macros
        else:
            commands[name] = macro_data
            added += 1
    
    db["commands"] = commands
    
    # Write back
    with open(db_path, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2)
    
    print(f"  Added {added} new macros, updated {updated} existing")
    if verbose and added + updated > 0:
        print(f"  Sample macros:")
        for name in list(macros.keys())[:5]:
            m = macros[name]
            print(f"    - {name}: {len(m['expansion'])} expansion lines")
    
    return added + updated


def get_game_version(db: dict) -> str:
    """Extract game version from database metadata."""
    return db.get("meta", {}).get("version", "Unknown")


def build_id_to_name_map(commands: dict, cmd_type: str) -> dict[int, str]:
    """Build opcode -> name mapping for commands of a given type."""
    id_map = {}
    for name, data in commands.items():
        if data.get("type") == cmd_type:
            id_map[data["id"]] = name
    return id_map


def compare_macros_with_db(
    db: dict, 
    decomp_macros: dict[str, ParsedMacro], 
    cmd_type: str
) -> tuple[list, list, list, list]:
    """
    Compare parsed decomp macros with database.
    
    Returns:
        - missing: Commands in decomp but not in database
        - extra: Commands in database but not in decomp  
        - mismatched: Commands with different opcodes
        - wrappers: Wrapper macros found
    """
    db_commands = db.get("commands", {})
    db_id_to_name = build_id_to_name_map(db_commands, cmd_type)
    db_name_to_id = {
        name: data["id"] 
        for name, data in db_commands.items() 
        if data.get("type") == cmd_type
    }
    
    missing = []
    mismatched = []
    wrappers = []
    
    for name, macro in decomp_macros.items():
        # Skip wrapper macros for now (track separately)
        if macro.is_wrapper and macro.expansion:
            wrappers.append({
                "name": name,
                "target": macro.expansion.target_macro,
                "args": macro.expansion.args,
                "params": [p.name for p in macro.params]
            })
            continue
            
        # Check if macro is an opcode switcher (multiple opcodes)
        if len(macro.opcodes) > 1:
            continue
            
        if not macro.opcodes:
            continue
            
        primary_opcode = macro.opcodes[0]
        
        if name in db_name_to_id:
            # Name exists - check opcode matches
            if db_name_to_id[name] != primary_opcode:
                mismatched.append({
                    "name": name,
                    "decomp_opcode": primary_opcode,
                    "db_opcode": db_name_to_id[name],
                    "is_conditional": macro.is_conditional,
                    "all_opcodes": macro.opcodes,
                    "params": [p.name for p in macro.params]
                })
        elif primary_opcode in db_id_to_name:
            # Opcode exists with different name
            db_name = db_id_to_name[primary_opcode]
            
            # Check if existing DB name is also a valid decomp name (alias)
            # If so, don't rename it (prevent flip-flopping between aliases)
            if db_name in decomp_macros:
                continue
                
            mismatched.append({
                "name": name,
                "decomp_opcode": primary_opcode,
                "db_name": db_name,
                "is_conditional": macro.is_conditional,
                "all_opcodes": macro.opcodes,
                "params": [p.name for p in macro.params]
            })
        else:
            # Completely missing
            missing.append({
                "name": name,
                "opcode": primary_opcode,
                "is_conditional": macro.is_conditional,
                "params": [p.name for p in macro.params],
                "variants": [
                    {"condition": v.condition, "emits": v.params_emitted}
                    for v in macro.variants
                ]
            })
    
    # Find commands in DB but not in decomp
    decomp_opcodes = set()
    for macro in decomp_macros.values():
        decomp_opcodes.update(macro.opcodes)
    
    decomp_names = set(decomp_macros.keys())
    extra = [
        {"name": name, "opcode": data["id"]}
        for name, data in db_commands.items()
        if data.get("type") == cmd_type 
           and data["id"] not in decomp_opcodes
           and name not in decomp_names
    ]
    
    return missing, extra, mismatched, wrappers


def update_param_defaults(db_params: list, macro: ParsedMacro) -> bool:
    """
    Update database parameters with default values from macro definition.
    Returns True if changes were made.
    """
    if not macro.emitted_params or not db_params:
        return False
        
    if len(macro.emitted_params) != len(db_params):
        return False
        
    changes = False
    
    # Map by position: DB Param[i] corresponds to Emitted Param[i]
    for i, emitted_name in enumerate(macro.emitted_params):
        # emitted_name is the name used in the body (e.g. entryStringID)
        # Find corresponding argument in macro.params
        arg_def = next((p for p in macro.params if p.name == emitted_name), None)
        
        if arg_def and arg_def.default:
            # We found a default value
            if "default" not in db_params[i] or db_params[i]["default"] != arg_def.default:
                db_params[i]["default"] = arg_def.default
                changes = True
                
    return changes


def update_db_from_sync(
    db: dict,
    missing: list,
    mismatched: list,
    cmd_type: str
) -> int:
    """
    Update database based on sync results.
    Returns number of changes made.
    """
    commands = db.get("commands", {})
    changes = 0
    
    # Handle mismatches (Potential Renames or Opcode fixes)
    for item in mismatched:
        decomp_name = item['name']
        
        # skip if decomp name is unused/placeholder
        if is_placeholder_name(decomp_name):
            continue
            
        if "db_name" in item:
            # ID match, Name mismatch -> Rename
            old_name = item['db_name']
            
            # If DB name is NOT a placeholder and Decomp name IS (already checked above),
            # we would keep DB name. But here we know Decomp name is valid.
            # So we rename Old -> New
            
            # Retrieve data using old name
            if old_name in commands:
                data = commands[old_name]
                del commands[old_name]
                commands[decomp_name] = data
                # Update legacy_name only if it's missing (preserve original legacy name)
                if "legacy_name" not in data:
                    data["legacy_name"] = old_name
                changes += 1
                print(f"    Renamed {old_name} -> {decomp_name}")
        
        elif "db_opcode" in item:
            # Name match, Opcode mismatch -> Update Opcode
            # This is dangerous if ID is the primary key in some contexts, but here Name is key
            if decomp_name in commands:
                commands[decomp_name]["id"] = item['decomp_opcode']
                changes += 1
                print(f"    Updated opcode for {decomp_name}: {item['db_opcode']} -> {item['decomp_opcode']}")
            else:
                # Key missing? Might have been renamed away in this same batch.
                # Treat as new command.
                entry = {
                    "type": cmd_type,
                    "id": item['decomp_opcode'],
                    "description": f"Imported from decomp: {decomp_name}",
                    "params": []
                }
                
                if item.get('params'):
                    entry["params"] = [
                        {"name": p, "type": infer_param_type(p)} 
                        for p in item['params']
                    ]
                
                commands[decomp_name] = entry
                changes += 1
                print(f"    Re-added {decomp_name} (0x{item['decomp_opcode']:04X}) after rename collision")

    # Handle missing (New Commands)
    for item in missing:
        name = item['name']
        if is_placeholder_name(name):
            continue
            
        # Create new entry
        entry = {
            "type": cmd_type,
            "id": item['opcode'],
            "description": f"Imported from decomp: {name}",
            "params": []
        }
        
        # Add params if available
        if item.get('params'):
            entry["params"] = [
                {"name": p, "type": infer_param_type(p)} 
                for p in item['params']
            ]
            
        if item.get('is_conditional'):
            # TODO: Better handling of conditional variants from sync
            pass
            
        commands[name] = entry
        changes += 1
        print(f"    Added new command: {name} (0x{item['opcode']:04X})")

    # Sort commands by Type then ID
    def get_sort_key(item):
        name, data = item
        type_priority = {
            "script_cmd": 0,
            "movement": 1,
            "levelscript_cmd": 2,
            "macro": 3
        }
        priority = type_priority.get(data.get("type"), 4)
        return priority, data.get("id", 999999), name
    
    sorted_commands = dict(sorted(commands.items(), key=get_sort_key))
    db["commands"] = sorted_commands
    return changes


def sync_database(db_path: str, update: bool = False, verbose: bool = False) -> bool:
    """
    Sync a database file with decomp definitions.
    
    Args:
        db_path: Path to the v2 database JSON file
        update: If True, update the database file with decomp names
        verbose: If True, show more details
        
    Returns:
        True if changes were made (or would be made), False otherwise
    """
    with open(db_path, 'r', encoding='utf-8') as f:
        db = json.load(f)
    
    version = get_game_version(db)
    print(f"\nSyncing {db_path} ({version})")
    
    if version not in DECOMP_SOURCES:
        print(f"  Skipping: No decomp source configured for {version}")
        return False
    
    sources = DECOMP_SOURCES[version]
    has_changes = False
    
    # Sync script commands
    if "scrcmd" in sources:
        print("  Fetching scrcmd.inc...")
        content = fetch_url(sources["scrcmd"])
        if content:
            decomp_macros = parse_scrcmd_inc(content)
            
            # Count types
            simple = sum(1 for m in decomp_macros.values() if not m.is_conditional and not m.is_wrapper)
            conditional = sum(1 for m in decomp_macros.values() if m.is_conditional)
            wrapper = sum(1 for m in decomp_macros.values() if m.is_wrapper)
            
            print(f"  Parsed {len(decomp_macros)} macros: {simple} simple, {conditional} conditional, {wrapper} wrapper")
            
            missing, extra, mismatched, wrappers = compare_macros_with_db(db, decomp_macros, "script_cmd")
            
            if missing:
                print(f"  Missing in DB: {len(missing)}")
                for item in missing[:10]:
                    cond_str = " (conditional)" if item['is_conditional'] else ""
                    print(f"    - {item['name']} (0x{item['opcode']:04X}){cond_str}")
                    if verbose and item['variants']:
                        for v in item['variants']:
                            print(f"        when {v['condition']}: emits {v['emits']}")
                if len(missing) > 10:
                    print(f"    ... and {len(missing) - 10} more")
                has_changes = True
            
            if mismatched:
                print(f"  Name/opcode mismatches: {len(mismatched)}")
                for item in mismatched[:10]:
                    if "db_name" in item:
                        cond_str = " (conditional)" if item['is_conditional'] else ""
                        print(f"    - Decomp '{item['name']}' vs DB '{item['db_name']}' (0x{item['decomp_opcode']:04X}){cond_str}")
                    else:
                        print(f"    - {item['name']}: decomp=0x{item['decomp_opcode']:04X}, db=0x{item['db_opcode']:04X}")
                if len(mismatched) > 10:
                    print(f"    ... and {len(mismatched) - 10} more")
                has_changes = True

            # Apply updates if requested
            if update and (missing or mismatched):
                print("  Applying updates...")
                count = update_db_from_sync(db, missing, mismatched, "script_cmd")
                if count > 0:
                    print(f"  Applied {count} changes to commands")
                    # Save immediately to avoid losing progress if next steps fail
                    with open(db_path, 'w', encoding='utf-8') as f:
                        json.dump(db, f, indent=2)
            
            # Update parameter defaults if requested
            if update:
                print("  Checking parameter defaults...")
                defaults_updated = 0
                db_commands = db.get("commands", {})
                # Rebuild map as updates might have changed things
                db_name_to_id = {
                    name: data["id"] 
                    for name, data in db_commands.items() 
                    if data.get("type") == "script_cmd"
                }
                db_id_to_name = {v: k for k, v in db_name_to_id.items()}
                
                for name, macro in decomp_macros.items():
                    if macro.is_wrapper or macro.is_conditional or len(macro.opcodes) > 1:
                        continue
                    if not macro.opcodes:
                        continue
                        
                    # Find corresponding DB command
                    target_name = None
                    if name in db_name_to_id:
                        target_name = name
                    elif macro.opcodes[0] in db_id_to_name:
                        target_name = db_id_to_name[macro.opcodes[0]]
                    
                    if target_name and target_name in db_commands:
                        cmd = db_commands[target_name]
                        if update_param_defaults(cmd.get("params", []), macro):
                            defaults_updated += 1
                            
                if defaults_updated > 0:
                    print(f"  Updated defaults for {defaults_updated} commands")
                    has_changes = True
                    with open(db_path, 'w', encoding='utf-8') as f:
                        json.dump(db, f, indent=2)
            
            if wrappers and verbose:
                print(f"  Wrapper macros found: {len(wrappers)}")
                for w in wrappers[:5]:
                    args = ', '.join(w['args']) if w['args'] else ""
                    print(f"    - {w['name']}({', '.join(w['params'])}) -> {w['target']}({args})")
                if len(wrappers) > 5:
                    print(f"    ... and {len(wrappers) - 5} more")
            
            if extra:
                print(f"  Extra in DB (not in decomp): {len(extra)}")
    
    # Sync movements
    if "movement" in sources:
        print("  Fetching movement.inc...")
        content = fetch_url(sources["movement"])
        if content:
            decomp_moves = parse_movement_inc(content)
            print(f"  Parsed {len(decomp_moves)} movements from decomp")
            
            # Use simple comparison for movements
            db_commands = db.get("commands", {})
            db_id_to_name = build_id_to_name_map(db_commands, "movement")
            db_name_to_id = {
                name: data["id"] 
                for name, data in db_commands.items() 
                if data.get("type") == "movement"
            }
            
            missing = []
            mismatched = []
            
            for name, opcode in decomp_moves.items():
                if name in db_name_to_id:
                    if opcode is not None and db_name_to_id[name] != opcode:
                        mismatched.append({
                            "name": name, 
                            "decomp_opcode": opcode, 
                            "db_opcode": db_name_to_id[name]
                        })
                elif opcode is not None and opcode in db_id_to_name:
                    db_name = db_id_to_name[opcode]
                    # Check if existing DB name is also a valid decomp name (alias)
                    if db_name in decomp_moves:
                        continue
                        
                    mismatched.append({
                        "name": name, 
                        "decomp_opcode": opcode, 
                        "db_name": db_name
                    })
                else:
                    if opcode is not None:
                        missing.append({"name": name, "opcode": opcode})
            
            if missing:
                print(f"  Missing movements in DB: {len(missing)}")
                for item in missing[:5]:
                    opcode = item['opcode']
                    opcode_str = f"0x{opcode:02X}" if opcode is not None else "(symbolic)"
                    print(f"    - {item['name']} ({opcode_str})")
                if len(missing) > 5:
                    print(f"    ... and {len(missing) - 5} more")
                has_changes = True
            
            if mismatched:
                print(f"  Movement mismatches: {len(mismatched)}")
                has_changes = True

            # Apply updates if requested
            if update and (missing or mismatched):
                print("  Applying movement updates...")
                count = update_db_from_sync(db, missing, mismatched, "movement")
                if count > 0:
                    print(f"  Applied {count} changes to movements")
                    with open(db_path, 'w', encoding='utf-8') as f:
                        json.dump(db, f, indent=2)
    
    # Sync levelscript macros (parsed from scrcmd.inc already fetched above)
    if "scrcmd" in sources:
        # Re-fetch or reuse content
        content = fetch_url(sources["scrcmd"])
        if content:
            levelscript_macros = parse_levelscript_macros(content)
            if levelscript_macros:
                print(f"  Parsed {len(levelscript_macros)} levelscript macros")
                
                ls_missing, ls_mismatched, ls_corrections = compare_levelscript_with_db(
                    db, levelscript_macros
                )
                
                if ls_corrections:
                    print(f"  Levelscript corrections needed: {len(ls_corrections)}")
                    for c in ls_corrections:
                        print(f"    - {c['name']}: {c['issue']} (current={c['current']}, should be={c['should_be']})")
                        print(f"      Reason: {c['reason']}")
                    has_changes = True
                
                if ls_mismatched and verbose:
                    print(f"  Levelscript mismatches: {len(ls_mismatched)}")
                    for m in ls_mismatched:
                        print(f"    - {m['name']}: {m['issue']} decomp={m['decomp']}, db={m['db']}")
    
    if not has_changes:
        print("  ✓ Database is in sync with decomp")
    
    return has_changes


def dump_macros(game: str) -> None:
    """Dump all parsed macros for a game (for debugging)."""
    game_map = {
        "platinum": "Platinum",
        "pt": "Platinum", 
        "hgss": "HeartGold/SoulSilver",
        "heartgold": "HeartGold/SoulSilver",
    }
    
    version = game_map.get(game.lower())
    if not version:
        print(f"Unknown game: {game}")
        print(f"Valid options: {', '.join(game_map.keys())}")
        return
    
    if version not in DECOMP_SOURCES:
        print(f"No decomp source for {version}")
        return
    
    sources = DECOMP_SOURCES[version]
    
    print(f"Fetching macros for {version}...")
    content = fetch_url(sources["scrcmd"])
    if not content:
        return
    
    macros = parse_scrcmd_inc(content)
    
    # Group by type
    simple = []
    conditional = []
    opcode_switch = []
    wrappers = []
    
    for name, macro in sorted(macros.items(), key=lambda x: x[1].opcodes[0] if x[1].opcodes else 9999):
        if macro.is_wrapper:
            wrappers.append(macro)
        elif len(macro.opcodes) > 1:
            opcode_switch.append(macro)
        elif macro.is_conditional:
            conditional.append(macro)
        else:
            simple.append(macro)
    
    print(f"\n=== Simple Commands ({len(simple)}) ===")
    for m in simple[:20]:
        params = ', '.join(p.name + (f"={p.default}" if p.default else "") for p in m.params)
        print(f"  0x{m.opcodes[0]:04X} {m.name}({params})")
    if len(simple) > 20:
        print(f"  ... and {len(simple) - 20} more")
    
    print(f"\n=== Conditional Commands ({len(conditional)}) ===")
    for m in conditional:
        params = ', '.join(p.name + (f"={p.default}" if p.default else "") for p in m.params)
        print(f"  0x{m.opcodes[0]:04X} {m.name}({params})")
        for v in m.variants:
            print(f"      when {v.condition}: emits {v.params_emitted}")
    
    print(f"\n=== Opcode-Switching Commands ({len(opcode_switch)}) ===")
    for m in opcode_switch:
        params = ', '.join(p.name for p in m.params)
        opcodes = ', '.join(f"0x{op:04X}" for op in m.opcodes)
        print(f"  {m.name}({params}) -> [{opcodes}]")
    
    print(f"\n=== Wrapper Macros ({len(wrappers)}) ===")
    for m in wrappers[:20]:
        params = ', '.join(p.name for p in m.params)
        if m.expansion:
            args = ', '.join(m.expansion.args) if m.expansion.args else ""
            print(f"  {m.name}({params}) -> {m.expansion.target_macro}({args})")
        else:
            print(f"  {m.name}({params}) -> ???")
    if len(wrappers) > 20:
        print(f"  ... and {len(wrappers) - 20} more")


def main():
    parser = argparse.ArgumentParser(
        description="Sync database with decomp project definitions"
    )
    parser.add_argument(
        "database",
        nargs="?",
        help="Path to v2 database file"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Sync all *_v2.json files in the repository"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update database with decomp names (not yet implemented)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show more details including wrapper macros and variant conditions"
    )
    parser.add_argument(
        "--dump",
        metavar="GAME",
        help="Dump all parsed macros for a game (platinum, hgss)"
    )
    parser.add_argument(
        "--inject-macros",
        action="store_true",
        default=True,
        help="Inject convenience macros from decomp into the v2 database (default: enabled)"
    )
    parser.add_argument(
        "--no-inject-macros",
        action="store_true",
        help="Disable automatic macro injection"
    )
    
    args = parser.parse_args()
    
    if args.dump:
        dump_macros(args.dump)
        return 0
    
    if args.inject_macros and not args.no_inject_macros:
        if args.all:
            # Inject into all v2 files (root + custom_databases)
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            v2_files = [
                os.path.join(repo_root, f)
                for f in os.listdir(repo_root)
                if f.endswith("_v2.json")
            ]
            
            # Also check custom_databases directory
            custom_db_dir = os.path.join(repo_root, "custom_databases")
            if os.path.isdir(custom_db_dir):
                for f in os.listdir(custom_db_dir):
                    if f.endswith("_v2.json"):
                        v2_files.append(os.path.join(custom_db_dir, f))
            
            if not v2_files:
                print("No *_v2.json files found in repository")
                return 1
            
            for path in sorted(v2_files):
                print(f"\nInjecting macros into {path}")
                inject_macros_into_db(path, args.verbose)
        elif args.database:
            print(f"Injecting macros into {args.database}")
            inject_macros_into_db(args.database, args.verbose)
        else:
            print("Error: --inject-macros requires --all or a database path")
            return 1
        return 0
    
    if args.all:
        # Find all v2 files in repo root + custom_databases
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        v2_files = [
            os.path.join(repo_root, f)
            for f in os.listdir(repo_root)
            if f.endswith("_v2.json")
        ]
        
        # Also check custom_databases directory
        custom_db_dir = os.path.join(repo_root, "custom_databases")
        if os.path.isdir(custom_db_dir):
            for f in os.listdir(custom_db_dir):
                if f.endswith("_v2.json"):
                    v2_files.append(os.path.join(custom_db_dir, f))
        
        if not v2_files:
            print("No *_v2.json files found in repository")
            return 1
        
        for path in sorted(v2_files):
            sync_database(path, args.update, args.verbose)
    elif args.database:
        if not os.path.exists(args.database):
            print(f"Error: File not found: {args.database}")
            return 1
        sync_database(args.database, args.update, args.verbose)
    else:
        parser.print_help()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
