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
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
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
    "scrdef",
    "map_script",
    "ScriptEntry",
    "save_game_normal",
    "script_entry",
    "script_entry_fixed",
    "script_entry_go_to_if_equal",
}

# Manual primitive definitions for macros that wrap primitives but don't have
# the /* PrimitiveName */ comment pattern in decomp. These are injected into
# the hidden primitives dict as if they were extracted from comments.
# Format: macro_name -> (opcode, primitive_name, [param dicts])
MANUAL_PRIMITIVES = {
    # GoToIfCannotAddCoins wraps an unnamed primitive at opcode 630
    # Macro: .short 630; .short VAR_RESULT; .short \amount; Noop; GoToIfEq ...
    "GoToIfCannotAddCoins": (
        630,
        "CannotAddCoins",
        [
            {"name": "result", "type": "var"},
            {"name": "amount", "type": "u16"},
        ],
    ),
}

# Levelscript macros - these define how scripts are triggered on a map
# We parse these specially since they don't have numeric opcodes like regular commands
LEVELSCRIPT_MACROS = {
    "InitScriptEntry_Fixed",
    "InitScriptEntry_OnFrameTable",
    "InitScriptEntry_OnTransition",
    "InitScriptEntry_OnResume",
    "InitScriptEntry_OnLoad",
    "InitScriptEntryEnd",
    "InitScriptGoToIfEqual",
    "InitScriptFrameTableEnd",
    "InitScriptEnd",
}


@dataclass
class MacroParam:
    """A macro parameter definition."""

    name: str
    default: str | None = None
    optional: bool = False  # True if used inside .ifnb block


@dataclass
class Variant:
    """A conditional variant of a command."""

    condition: str  # e.g., "mode == 2", "arg0 <= 3"
    params_emitted: list[str] = field(
        default_factory=list
    )  # param names emitted in this branch


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
    all_emitted_values: list[dict] = field(
        default_factory=list
    )  # All emitted values including literals
    expansion: MacroExpansion | None = None  # If this is a wrapper macro
    body: str = ""  # Raw body for debugging
    primitive_name: str | None = None  # If macro wraps a differently-named primitive
    primitive_params: list[dict] = field(
        default_factory=list
    )  # Params for the primitive

    @property
    def is_wrapper(self) -> bool:
        return self.expansion is not None

    @property
    def wraps_primitive(self) -> bool:
        """True if this macro wraps a primitive command with a different name."""
        return self.primitive_name is not None and self.primitive_name != self.name


@dataclass
class LevelscriptMacro:
    """A parsed levelscript macro definition."""

    name: str
    params: list[MacroParam]
    type_id: int | None = None  # The INIT_SCRIPT_* constant value if fixed type
    emits: list[str] = field(
        default_factory=list
    )  # What it emits: [".byte", ".short", etc.]
    is_wrapper: bool = False  # If it wraps another macro
    wrapper_target: str | None = None  # Target macro if wrapper


@dataclass
class MovementMacro:
    """A parsed movement macro definition."""

    name: str
    opcode: int | None  # Numeric opcode or None if symbolic
    params: list[MacroParam] = field(default_factory=list)  # Empty for EndMovement


def is_placeholder_name(name: str) -> bool:
    """Check if a name is a placeholder (like ScrCmd_21D, scrcmd_465, ScrCmd_Unused_XXX).

    NOTE: Names like TrySetUnusedUndergroundField are NOT placeholders - they are
    descriptive names where "Unused" is part of the description (the field itself is unused).
    We only filter placeholder patterns like ScrCmd_Unused_XXX where the name format indicates
    the command itself hasn't been properly identified yet.
    """
    if not name:
        return True

    # Match patterns like ScrCmd_XXX, scrcmd_XXX, Dummy_XXX, CMD_XXX
    # This includes ScrCmd_Unused_XXX patterns naturally
    return bool(re.match(r"^(ScrCmd_|scrcmd_|Dummy|CMD_)\w+$", name, re.IGNORECASE))


def fetch_url(url: str) -> str | None:
    """Fetch content from URL, return None on error."""
    try:
        with urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
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
        r"\.macro\s+(\w+)[ \t]*([^\n]*)\n(.*?)\.endm", re.MULTILINE | re.DOTALL
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
    if ";" in params_str:
        params_str = params_str.split(";", 1)[0]

    params_str = params_str.strip()
    if not params_str:
        return []

    params = []

    # Split by comma if present, otherwise by whitespace
    if "," in params_str:
        parts = [p.strip() for p in params_str.split(",")]
    else:
        parts = params_str.split()

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Handle both "name=value" and "name = value" formats
        if "=" in part:
            name, default = part.split("=", 1)
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
    lines = body.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith((";", "/*", "@", "#")):
            continue

        # Abort on conditionals as flow is ambiguous
        if line.startswith((".if", ".else", ".endif", ".macro", ".endm")):
            return []

        # Match .directive \param
        # We look for backslash followed by word char
        match = re.search(
            r"\.(?:short|2byte|hword|byte|word|long)\s+(?:.*\\(\w+))", line
        )
        if match:
            params.append(match.group(1))

    return params


def extract_all_emitted_values(body: str) -> list[dict]:
    """
    Extract ALL emitted values from macro body, including both macro params and literals.

    Returns a list of dicts, each with:
        - name: param name (e.g., 'banlistMsgStartIdx') or 'unused_N' for literals
        - type: directive type ('u8', 'u16', 'u32')
        - default: literal value if hardcoded (e.g., 0), None if from macro arg
        - is_literal: True if this is a hardcoded value, False if from macro arg

    Skips the first .short (opcode) and stops at conditionals.
    """
    result = []
    lines = body.split("\n")
    first_short_seen = False
    unused_counter = 0

    # Map directive to type
    directive_to_type = {
        "byte": "u8",
        "short": "u16",
        "2byte": "u16",
        "hword": "u16",
        "word": "u32",
        "long": "u32",
    }

    for line in lines:
        line = line.strip()
        if not line or line.startswith((";", "/*", "@", "#")):
            continue

        # Abort on conditionals as flow is ambiguous
        if line.startswith((".if", ".else", ".endif", ".macro", ".endm")):
            break

        # Match directives: .byte, .short, .word, etc.
        match = re.match(r"\.(\w+)\s+(.+?)(?:\s*[@;/].*)?$", line)
        if not match:
            continue

        directive = match.group(1).lower()
        if directive not in directive_to_type:
            continue

        value_str = match.group(2).strip()
        param_type = directive_to_type[directive]

        # Skip the first .short - that's the opcode
        if directive in ("short", "2byte", "hword") and not first_short_seen:
            first_short_seen = True
            continue

        # Check if it's a macro param reference (\paramName)
        param_match = re.search(r"\\(\w+)", value_str)
        if param_match:
            result.append(
                {
                    "name": param_match.group(1),
                    "type": param_type,
                    "default": None,
                    "is_literal": False,
                }
            )
        else:
            # It's a literal value - parse it
            try:
                if value_str.startswith("0x"):
                    literal_val = int(value_str, 16)
                else:
                    literal_val = int(value_str)

                result.append(
                    {
                        "name": f"unused_{unused_counter}",
                        "type": param_type,
                        "default": f"{literal_val}",
                        "is_literal": True,
                    }
                )
                unused_counter += 1
            except ValueError:
                # Could be a constant like TRUE/FALSE - skip for now
                pass

    return result


def extract_param_types(body: str, param_names: list[str]) -> dict[str, str]:
    """
    Extract parameter types from macro body based on directive used.

    Returns dict mapping param name -> type ('u8', 'u16', 'u32').
    Only returns types for params that are found in the body.
    """
    type_map = {}
    lines = body.split("\n")

    for line in lines:
        line = line.strip()
        if not line or line.startswith((";", "/*", "@", "#")):
            continue

        # Skip conditionals
        if line.startswith((".if", ".else", ".endif", ".macro", ".endm")):
            continue

        # Match .directive value \param
        # e.g., ".byte \rightSide", ".short \flagID", ".long \value"
        match = re.search(r"\.(byte|short|2byte|hword|word|long)\s+.*\\(\w+)", line)
        if match:
            directive = match.group(1)
            param_name = match.group(2)

            if param_name not in param_names:
                continue

            # Map directive to type
            if directive in ("byte",):
                param_type = "u8"
            elif directive in ("short", "2byte", "hword"):
                param_type = "u16"
            elif directive in ("word", "long"):
                param_type = "u32"
            else:
                continue

            # Only set if not already set (prefer first occurrence)
            if param_name not in type_map:
                type_map[param_name] = param_type

    return type_map


def extract_opcodes(body: str) -> list[int]:
    """Extract all numeric opcode emissions from macro body."""
    # Match .short/.byte/.2byte/.hword followed by numeric value
    # Must be at the start of emissions (first .short is the opcode)
    opcode_pattern = re.compile(r"\.(?:short|2byte|hword)\s+(\d+)")

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
    lines = body.split("\n")
    for line in lines:
        line = line.strip()
        # Skip comments
        if line.startswith(";") or line.startswith("/*"):
            continue
        # Check for .if before finding opcode
        if line.startswith(".if"):
            return None  # Opcode is inside conditional
        # Look for opcode emission
        match = re.match(r"\.(?:short|2byte|hword)\s+(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def extract_primitive_from_comment(body: str) -> tuple[int, str] | None:
    """
    Extract the primitive command name from a .short OPCODE /* PrimitiveName */ pattern.

    This detects when a macro emits an opcode with a comment indicating the actual
    command name (which may differ from the macro name). For example:
        .short 624 /* SetHiddenLocation */

    Returns (opcode, primitive_name) or None if pattern not found.
    """
    # Match: .short OPCODE /* CommandName */
    # The command name should be a valid identifier (PascalCase typically)
    pattern = re.compile(
        r"\.(?:short|2byte|hword)\s+(\d+)\s*/\*\s*([A-Z][a-zA-Z0-9_]+)\s*\*/",
        re.MULTILINE,
    )

    match = pattern.search(body)
    if match:
        opcode = int(match.group(1))
        primitive_name = match.group(2)
        return (opcode, primitive_name)

    return None


def extract_all_primitives_from_comments(body: str) -> list[tuple[int, str]]:
    """
    Extract ALL primitive command names from .short OPCODE /* PrimitiveName */ patterns.

    This handles opcode-switching macros like SetVar which reference multiple primitives:
        .short 40 /* SetVarFromValue */
        .short 41 /* SetVarFromVar */

    Returns list of (opcode, primitive_name) tuples.
    """
    pattern = re.compile(
        r"\.(?:short|2byte|hword)\s+(\d+)\s*/\*\s*([A-Z][a-zA-Z0-9_]+)\s*\*/",
        re.MULTILINE,
    )

    primitives = []
    for match in pattern.finditer(body):
        opcode = int(match.group(1))
        primitive_name = match.group(2)
        primitives.append((opcode, primitive_name))

    return primitives


def extract_primitive_params(body: str) -> list[dict]:
    """
    Extract parameter info for a primitive command from macro body.

    Looks at directives after the opcode emission to determine params.
    Returns list of {name, type} dicts.

    This captures ALL parameters including constants like VAR_RESULT,
    since when calling the primitive directly you need all arguments.
    """
    params = []
    lines = body.split("\n")
    found_opcode = False
    const_counter = 0  # For generating names for constant params

    for line in lines:
        line = line.strip()
        if not line or line.startswith((";", "@", "#")):
            continue

        # Skip standalone block comments
        if line.startswith("/*") and "*/" not in line:
            continue

        # Skip until we find the opcode (with comment pattern)
        if not found_opcode:
            if re.match(r"\.(?:short|2byte|hword)\s+\d+\s*/\*", line):
                found_opcode = True
            continue

        # After opcode, look for param emissions
        # Stop if we hit a macro call (non-directive line starting with uppercase)
        if not line.startswith(".") and line and line[0].isupper():
            break

        # Stop on conditionals
        if line.startswith((".if", ".else", ".endif")):
            break

        # Match .directive VALUE
        match = re.match(r"\.(byte|short|2byte|hword|word|long)\s+([^\s]+)", line)
        if match:
            directive = match.group(1)
            value = match.group(2).strip()

            # Remove trailing comments
            if "/*" in value:
                value = value.split("/*")[0].strip()

            # Determine type from directive
            if directive in ("byte",):
                param_type = "u8"
            elif directive in ("short", "2byte", "hword"):
                param_type = "u16"
            elif directive in ("word", "long"):
                param_type = "u32"
            else:
                param_type = "u16"

            # Check if it's a parameter reference (\name) or a constant
            if value.startswith("\\"):
                param_name = value[1:]  # Remove backslash
                params.append({"name": param_name, "type": param_type})
            else:
                # It's a constant (like VAR_RESULT, TRUE, FALSE)
                # These are still params to the primitive command
                # Use the constant name in lowercase as param name, or infer from context
                if "VAR_RESULT" in value or "RESULT" in value:
                    param_name = "result"
                    param_type = "var"  # Result vars are var type
                elif value in ("TRUE", "FALSE"):
                    param_name = f"flag_{const_counter}"
                    param_type = "u8"
                else:
                    param_name = f"arg_{const_counter}"
                const_counter += 1
                params.append({"name": param_name, "type": param_type})

    return params


def extract_primitive_call_line(
    body: str, id_to_name: dict[int, str] | None = None, macro_name: str | None = None
) -> str | None:
    """
    Extract a primitive call expansion line from a macro body.

    For macros like:
        .short 736 /* CheckAmitySquareManGiftIsAccesory */
        .short \\giftID
        .short VAR_RESULT
        GoToIfEq VAR_RESULT, FALSE, \\offset

    Returns: "CheckAmitySquareManGiftIsAccesory $giftID, VAR_RESULT"

    Returns None if no primitive comment pattern is found and no manual override exists.
    """
    # First, find the primitive name and opcode from comment
    primitive_info = extract_primitive_from_comment(body)

    # If no comment pattern, check for manual primitive override
    if not primitive_info:
        if macro_name and macro_name in MANUAL_PRIMITIVES:
            opcode, primitive_name, manual_params = MANUAL_PRIMITIVES[macro_name]
            # Build the call line from manual params
            args = [f"${p['name']}" for p in manual_params]
            if args:
                return f"{primitive_name} {', '.join(args)}"
            else:
                return primitive_name
        return None

    opcode, primitive_name = primitive_info

    # If we have id_to_name mapping and the primitive exists in DB, use that name
    if id_to_name and opcode in id_to_name:
        primitive_name = id_to_name[opcode]

    # Now extract the arguments that follow the opcode
    args = []
    lines = body.split("\n")
    found_opcode = False

    for line in lines:
        line = line.strip()
        if not line or line.startswith((";", "@", "#")):
            continue

        # Skip block comments but not inline comments
        if line.startswith("/*") and "*/" not in line:
            continue

        # Find the opcode line
        if not found_opcode:
            if re.match(r"\.(?:short|2byte|hword)\s+\d+\s*/\*", line):
                found_opcode = True
            continue

        # After opcode, look for param emissions
        # Stop if we hit a macro call (non-directive line starting with uppercase)
        if not line.startswith(".") and line and line[0].isupper():
            break

        # Stop on conditionals
        if line.startswith((".if", ".else", ".endif")):
            break

        # Match .directive VALUE
        match = re.match(r"\.(byte|short|2byte|hword|word|long)\s+([^\s]+)", line)
        if match:
            value = match.group(2).strip()
            # Remove trailing comments
            if "/*" in value:
                value = value.split("/*")[0].strip()

            # Convert \param to $param format
            if value.startswith("\\"):
                args.append(f"${value[1:]}")
            else:
                # It's a constant (like VAR_RESULT, TRUE, FALSE)
                args.append(value)

    if args:
        return f"{primitive_name} {', '.join(args)}"
    else:
        return f"{primitive_name}"


def detect_opcode_switching(body: str) -> list[tuple[str, int]]:
    """
    Detect if macro emits different opcodes based on conditions.

    Returns list of (condition, opcode) pairs.
    """
    switches = []

    # Pattern: .if CONDITION followed by .short OPCODE
    if_pattern = re.compile(
        r"\.if\s+(.+?)\n\s*\.(?:short|2byte|hword)\s+(\d+)\s*/\*\s*(\w+)", re.MULTILINE
    )

    for match in if_pattern.finditer(body):
        condition = match.group(1).strip()
        opcode = int(match.group(2))
        switches.append((condition, opcode))

    # Pattern: .else followed by .short OPCODE
    else_pattern = re.compile(
        r"\.else\s*\n\s*\.(?:short|2byte|hword)\s+(\d+)\s*/\*\s*(\w+)", re.MULTILINE
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
        r"\.if\s+(.+?)\n(.*?)(?:\.else|\.endif)", re.MULTILINE | re.DOTALL
    )

    for match in if_pattern.finditer(body):
        condition = match.group(1).strip()
        block = match.group(2)

        # Skip opcode-switching conditions (those emit .short with numeric + comment)
        if re.search(r"\.short\s+\d+\s*/\*", block):
            continue

        # Find params emitted in this block
        emitted = re.findall(r"\.(?:short|byte)\s+\\(\w+)", block)

        if emitted:
            variants.append(Variant(condition=condition, params_emitted=emitted))

    return variants


def detect_ifnb_optional_params(body: str, params: list[MacroParam]) -> list[str]:
    """
    Detect parameters that are conditionally used via .ifnb (if not blank) directives.
    These are optional params - the macro can be called with fewer arguments.

    Returns list of parameter names that are optional.
    """
    optional_params = []
    ifnb_pattern = re.compile(r"\.ifnb\s+\\(\w+)", re.MULTILINE)

    for match in ifnb_pattern.finditer(body):
        param_name = match.group(1)
        if any(p.name == param_name for p in params):
            optional_params.append(param_name)

    return optional_params


def parse_ifnb_expansion_variants(
    body: str, params: list[MacroParam]
) -> list[dict] | None:
    """
    Parse .ifnb blocks to generate expansion variants for macros with optional params.

    For a macro like:
        .macro TVBroadcastDummy arg0, arg1, arg2
            Dummy1F9 \\arg0
            .ifnb \\arg1
                Dummy1F9 \\arg1
            .endif
            .ifnb \\arg2
                Dummy1F9 \\arg2
            .endif
        .endm

    Returns a list of variant dicts with condition and expansion, or None if no .ifnb found.
    """
    optional_params = detect_ifnb_optional_params(body, params)
    if not optional_params:
        return None

    lines = body.split("\n")
    base_expansion = []
    ifnb_expansions = {}  # param_name -> list of expansion lines inside .ifnb block

    current_ifnb_param = None
    in_ifnb_block = False

    for line in lines:
        stripped = line.strip()

        # Detect .ifnb \param
        ifnb_match = re.match(r"\.ifnb\s+\\(\w+)", stripped)
        if ifnb_match:
            current_ifnb_param = ifnb_match.group(1)
            in_ifnb_block = True
            ifnb_expansions[current_ifnb_param] = []
            continue

        # Detect .endif
        if stripped == ".endif" and in_ifnb_block:
            in_ifnb_block = False
            current_ifnb_param = None
            continue

        # Skip other directives
        if stripped.startswith(".") or not stripped:
            continue

        # Skip comments
        if stripped.startswith((";", "/*", "@", "#")):
            continue

        # This is a macro call line
        if stripped and stripped[0].isupper():
            if in_ifnb_block and current_ifnb_param:
                ifnb_expansions[current_ifnb_param].append(stripped)
            else:
                base_expansion.append(stripped)

    if not ifnb_expansions:
        return None

    # Build variants based on which optional params are provided
    # For N optional params, we have N+1 variants (0 optional, 1 optional, ... N optional)
    required_params = [p for p in params if p.name not in optional_params]
    optional_param_list = [p for p in params if p.name in optional_params]

    variants = []

    # Start with base case (required params only)
    current_expansion = list(base_expansion)
    num_args = len(required_params)

    variants.append(
        {
            "condition": f"{num_args} arg(s)",
            "expansion": [
                format_expansion_line(line, params) for line in current_expansion
            ],
        }
    )

    # Add each optional param incrementally
    for opt_param in optional_param_list:
        num_args += 1
        if opt_param.name in ifnb_expansions:
            current_expansion = current_expansion + ifnb_expansions[opt_param.name]
        variants.append(
            {
                "condition": f"{num_args} args",
                "expansion": [
                    format_expansion_line(line, params) for line in current_expansion
                ],
            }
        )

    return variants


def detect_wrapper_macro(body: str, all_macro_names: set[str]) -> MacroExpansion | None:
    """
    Detect if this macro just calls another macro (wrapper/convenience macro).

    Returns MacroExpansion with target and args, or None.
    """
    lines = [
        l.strip()
        for l in body.split("\n")
        if l.strip() and not l.strip().startswith(";")
    ]

    # If body is just one line that calls another macro
    if len(lines) == 1:
        line = lines[0]
        # Check if line starts with a known macro name
        for macro_name in all_macro_names:
            if (
                line.startswith(macro_name + " ")
                or line.startswith(macro_name + ",")
                or line == macro_name
            ):
                # Extract arguments
                if " " in line:
                    args_str = line[len(macro_name) :].strip()
                    # Split on comma, handling backslash-prefixed params
                    args = [a.strip() for a in args_str.split(",") if a.strip()]
                else:
                    args = []
                return MacroExpansion(target_macro=macro_name, args=args)

    return None


def parse_macro(
    name: str, params_str: str, body: str, all_macro_names: set[str]
) -> ParsedMacro | None:
    """Parse a single macro definition into a ParsedMacro object."""
    if name in SKIP_MACROS:
        return None

    params = parse_params(params_str)

    # Check if it's a wrapper macro first
    expansion = detect_wrapper_macro(body, all_macro_names)
    if expansion:
        return ParsedMacro(
            name=name, params=params, opcodes=[], expansion=expansion, body=body.strip()
        )

    # Check for primitive command name in comment (e.g., .short 624 /* SetHiddenLocation */)
    primitive_info = extract_primitive_from_comment(body)
    primitive_name = None
    primitive_params = []

    if primitive_info:
        opcode, comment_name = primitive_info
        # If the comment name differs from the macro name, this macro wraps a primitive
        if comment_name != name:
            primitive_name = comment_name
            primitive_params = extract_primitive_params(body)

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
            body=body.strip(),
            primitive_name=primitive_name,
            primitive_params=primitive_params,
        )

    # Check for conditional parameter emission
    variants = parse_conditionals(body, params)

    opcodes = [first_opcode] if first_opcode is not None else extract_opcodes(body)[:1]

    if not opcodes:
        # No numeric opcode found - check if it's a multi-line wrapper macro
        # that calls other macros (like ShowArrowSign, ShowMapSign, etc.)
        expansion_lines = parse_macro_expansion_lines(body)
        if expansion_lines:
            # This is a multi-line wrapper macro
            return ParsedMacro(name=name, params=params, opcodes=[], body=body.strip())
        return None

    emitted_params = extract_emitted_params(body)
    all_emitted = extract_all_emitted_values(body)

    return ParsedMacro(
        name=name,
        params=params,
        opcodes=opcodes,
        is_conditional=bool(variants),
        variants=variants,
        emitted_params=emitted_params,
        all_emitted_values=all_emitted,
        body=body.strip(),
        primitive_name=primitive_name,
        primitive_params=primitive_params,
    )


def parse_scrcmd_inc(
    content: str,
) -> tuple[dict[str, ParsedMacro], dict[str, tuple[int, list[dict]]]]:
    """
    Parse scrcmd.inc into dict of name -> ParsedMacro.

    Also extracts primitives that are only defined via comments in wrapper macros.
    This handles both simple cases (EnableHiddenLocation -> SetHiddenLocation) and
    opcode-switching cases (SetVar -> SetVarFromValue, SetVarFromVar).

    Returns:
        - parsed: dict of macro_name -> ParsedMacro
        - primitives: dict of primitive_name -> (opcode, params) for commands
                     that don't have their own macro definition
    """
    raw_macros = extract_macros(content)
    all_names = {name for name, _, _ in raw_macros}

    parsed = {}
    primitives = {}  # primitive_name -> (opcode, params)

    for name, params_str, body in raw_macros:
        macro = parse_macro(name, params_str, body, all_names)
        if macro:
            parsed[name] = macro

            # Extract ALL primitives from comments in this macro's body
            # This handles both simple wrappers and opcode-switching macros
            all_prims = extract_all_primitives_from_comments(body)
            for prim_opcode, prim_name in all_prims:
                # Only add if:
                # 1. The primitive name differs from the macro name
                # 2. The primitive doesn't already have its own macro
                if prim_name != name and prim_name not in all_names:
                    if prim_name not in primitives:
                        # Extract params for this primitive
                        prim_params = extract_primitive_params(body)
                        primitives[prim_name] = (prim_opcode, prim_params)

    # Inject manual primitives for macros that don't have the comment pattern
    for macro_name, (opcode, prim_name, prim_params) in MANUAL_PRIMITIVES.items():
        if (
            macro_name in parsed
            and prim_name not in primitives
            and prim_name not in all_names
        ):
            primitives[prim_name] = (opcode, prim_params)
            # Also update the parsed macro to know it wraps this primitive
            parsed[macro_name].primitive_name = prim_name
            parsed[macro_name].primitive_params = prim_params

    return parsed, primitives


def parse_movement_inc(
    content: str, constants: dict[str, int] | None = None
) -> dict[str, tuple[int | None, list[MacroParam]]]:
    r"""
    Parse movement.inc to extract movement macro names, opcodes, and params.

    Returns dict of name -> (opcode, params).
    Most movements have a `length` param with default=1.
    EndMovement has no params and id=254.

    Args:
        content: The movement.inc file content
        constants: Optional dict of MOVEMENT_ACTION_* constant names to values
    """
    movements = {}

    # Known symbolic movement action constants
    MOVEMENT_ACTION_END = 254

    # Pattern to extract: .macro Name [params] on one line,
    # followed by .short VALUE on the next line (first directive)
    macro_pattern = re.compile(
        r"\.macro\s+(\w+)\s*([^\n]*)\n\s*\.(?:byte|short|hword)\s+(\S+)", re.MULTILINE
    )

    for match in macro_pattern.finditer(content):
        name = match.group(1)
        params_str = match.group(2)
        value_str = match.group(3)

        # Handle EndMovement specially - it has no params in the macro signature
        # but uses two .short directives (opcode and length=0)
        if name == "EndMovement":
            movements[name] = (MOVEMENT_ACTION_END, [])
            continue

        # Parse params - EndMovement has no params
        if params_str.strip():
            params = parse_params(params_str)
        else:
            params = []

        # Try to parse opcode as numeric
        try:
            if value_str.startswith("0x"):
                opcode = int(value_str, 16)
            else:
                opcode = int(value_str)
        except ValueError:
            # Try to resolve from constants (e.g., MOVEMENT_ACTION_FACE_NORTH -> 0)
            if constants and value_str in constants:
                opcode = constants[value_str]
            else:
                opcode = None

        movements[name] = (opcode, params)

    return movements


def get_movement_action_constants() -> dict[str, int]:
    """
    Fetch movement_actions.txt from decomp and use metang to build constant map.

    Returns dict of MOVEMENT_ACTION_* constant name -> numeric value.
    """
    constants = {
        "MOVEMENT_ACTION_END": 254,
        "MOVEMENT_ACTION_NONE": 255,
    }

    try:
        # Fetch movement_actions.txt from decomp
        url = "https://raw.githubusercontent.com/pret/pokeplatinum/main/generated/movement_actions.txt"
        with urlopen(url, timeout=10) as response:
            content = response.read().decode("utf-8")

        # Run metang to generate Python enum using stdin
        metang_path = Path(__file__).parent.parent / "metang" / "metang.py"
        result = subprocess.run(
            [
                "python",
                str(metang_path),
                "enum",
                "-",
                "-L",
                "py",
                "-t",
                "MOVEMENT_ACTION",
            ],
            input=content,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent / "metang"),
        )

        if result.returncode != 0:
            return constants

        # Parse the generated enum
        enum_code = result.stdout

        # Extract constant values using regex
        pattern = r"(MOVEMENT_ACTION_\w+)\s*=\s*(\d+)"
        for match in re.finditer(pattern, enum_code):
            name = match.group(1)
            value = int(match.group(2))
            constants[name] = value

    except Exception:
        pass

    return constants


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
        "INIT_SCRIPT_ON_FRAME_TABLE": 1,
        "INIT_SCRIPT_ON_TRANSITION": 2,
        "INIT_SCRIPT_ON_RESUME": 3,
        "INIT_SCRIPT_ON_LOAD": 4,
    }

    results = {}

    for name, params_str, body in raw_macros:
        if name not in LEVELSCRIPT_MACROS:
            continue

        # For levelscript macros, we need cleaner param parsing
        # Filter out .set directives that might appear on the same line
        clean_params_str = params_str.strip()
        if clean_params_str.startswith("."):
            # This is a directive, not params
            clean_params_str = ""

        params = parse_params(clean_params_str)

        # Check if it's a wrapper macro (single line calling another macro)
        lines = [
            l.strip()
            for l in body.split("\n")
            if l.strip()
            and not l.strip().startswith((".if", ".else", ".endif", ".error", ".set"))
        ]

        is_wrapper = False
        wrapper_target = None
        type_id = None
        emits = []

        # Check for wrapper pattern (calls another InitScript macro)
        if len(lines) == 1:
            line = lines[0]
            for target in LEVELSCRIPT_MACROS:
                if line.startswith(target + " ") or line.startswith(target + ","):
                    is_wrapper = True
                    wrapper_target = target
                    break

        # Parse what this macro emits
        for line in body.split("\n"):
            line = line.strip()
            if line.startswith(".byte"):
                emits.append(".byte")
                # Check for type constant
                match = re.search(r"\.byte\s+(INIT_SCRIPT_\w+|\d+|\\?\w+)", line)
                if match:
                    val = match.group(1)
                    if val in init_script_types:
                        type_id = init_script_types[val]
                    elif val.isdigit():
                        type_id = int(val)
            elif line.startswith(".short"):
                emits.append(".short")
            elif line.startswith(".long"):
                emits.append(".long")

        results[name] = LevelscriptMacro(
            name=name,
            params=params,
            type_id=type_id,
            emits=emits,
            is_wrapper=is_wrapper,
            wrapper_target=wrapper_target,
        )

    return results


def compare_levelscript_with_db(
    db: dict, decomp_macros: dict[str, LevelscriptMacro]
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
        in_commands = (
            name in db_commands and db_commands[name].get("type") == "levelscript_cmd"
        )
        in_meta = name in db_meta

        if macro.is_wrapper:
            # Wrapper macros may or may not need to be in DB
            continue

        if not in_commands and not in_meta:
            missing.append(
                {
                    "name": name,
                    "type_id": macro.type_id,
                    "emits": macro.emits,
                    "params": [p.name for p in macro.params],
                }
            )
            continue

        # Check for mismatches
        if in_commands:
            db_entry = db_commands[name]
            db_type_id = db_entry.get("id")
            db_params = db_entry.get("params", [])

            # Check type ID
            if macro.type_id is not None and db_type_id != macro.type_id:
                mismatched.append(
                    {
                        "name": name,
                        "issue": "type_id",
                        "decomp": macro.type_id,
                        "db": db_type_id,
                    }
                )

            # Check param count
            # Count params (excluding wrapper target args)
            decomp_param_count = len(
                [p for p in macro.params if not p.name.startswith("\\")]
            )
            db_param_count = len(db_params)

            # Special case: if decomp emits nothing but DB has params, it's wrong
            if len(macro.emits) == 0 and db_param_count > 0:
                corrections.append(
                    {
                        "name": name,
                        "issue": "has_params",
                        "current": db_param_count,
                        "should_be": 0,
                        "reason": "Decomp macro emits no data",
                    }
                )
            elif (
                len(macro.emits) == 1
                and macro.emits[0] == ".short"
                and db_param_count > 0
            ):
                # Just emits .short 0 or similar (no params)
                # Check if macro has no real params
                if len(macro.params) == 0:
                    corrections.append(
                        {
                            "name": name,
                            "issue": "has_params",
                            "current": db_param_count,
                            "should_be": 0,
                            "reason": "Decomp macro just emits a constant value",
                        }
                    )

    return missing, mismatched, corrections


def parse_macro_expansion_lines(body: str) -> list[str]:
    """
    Parse macro body into expansion lines (calls to other macros).

    Filters out directives (.short, .byte, .if, etc.) and returns
    only the macro call lines.
    """
    lines = []
    for line in body.split("\n"):
        line = line.strip()
        # Skip empty lines, comments, and assembler directives
        if not line:
            continue
        if line.startswith((".", ";", "/*", "@", "#")):
            continue
        # Skip .ifnb blocks content for now (optional params)
        if "\\" in line or line[0].isupper():
            lines.append(line)
    return lines


def format_expansion_line(line: str, params: list[MacroParam]) -> str:
    """
    Convert a macro call line to expansion format with $param syntax.

    Input:  "CompareVar \\varID, \\valueOrVarID"
            "Signpost \\messageID, SIGNPOST_TYPE_ARROW"
    Output: "CompareVar $varID, $valueOrVarID"
            "Signpost $messageID, SIGNPOST_TYPE_ARROW"

    Always uses comma-separated arguments for script compiler compatibility.
    """
    import re

    # Handle empty or no-argument cases
    if not line or not line.strip():
        return line

    # Always split by whitespace first to get command name
    tokens = line.split()
    cmd = tokens[0]

    # Remaining tokens are arguments (comma-separated or space-separated in source)
    all_args = tokens[1:] if len(tokens) > 1 else []

    # Clean each argument: replace \param with $param, strip whitespace and commas
    clean_args = []
    for arg in all_args:
        # Replace \param with $param for all params
        for p in params:
            arg = arg.replace(f"\\{p.name}", f"${p.name}")
        # Also handle any remaining backslash-prefixed identifiers
        arg = re.sub(r"\\([a-zA-Z_][a-zA-Z0-9_]*)", r"$\1", arg)
        # Strip whitespace and trailing commas
        arg = arg.strip().rstrip(",")
        if arg:
            clean_args.append(arg)

    if not clean_args:
        return cmd

    return f"{cmd} {', '.join(clean_args)}"


def infer_param_type(name: str, context: str = "") -> str:
    """Infer parameter type from name and context."""
    name_lower = name.lower()

    if "var" in name_lower:
        return "var"
    if "flag" in name_lower:
        return "flag"
    if "offset" in name_lower or "label" in name_lower or "dest" in name_lower:
        return "label"
    if "message" in name_lower or "msg" in name_lower:
        return "msg_id"
    if "script" in name_lower:
        return "script_id"
    if "species" in name_lower:
        return "species"
    if "item" in name_lower:
        return "item"
    if "map" in name_lower:
        return "map_id"
    if "trainer" in name_lower:
        return "trainer_id"

    return "u16"


def _find_hardcoded_param_value(
    body: str, param_name: str, manual_params: list[dict]
) -> str | None:
    """
    Find the hardcoded value for a param that's not in the macro declaration.
    Scans the macro body for emitted constants in the position corresponding to param_name.
    """
    param_index = next(
        (i for i, p in enumerate(manual_params) if p["name"] == param_name), None
    )
    if param_index is None:
        return None

    lines = body.split("\n")
    found_opcode = False
    emit_index = 0

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "@", "#")):
            continue

        if not found_opcode:
            if re.match(r"\.(?:short|2byte|hword)\s+\d+", stripped):
                found_opcode = True
            continue

        if not stripped.startswith(".") and stripped and stripped[0].isupper():
            break

        match = re.match(r"\.(byte|short|2byte|hword|word|long)\s+([^\s]+)", stripped)
        if match:
            value = match.group(2).strip()
            if "/*" in value:
                value = value.split("/*")[0].strip()

            if emit_index == param_index:
                if not value.startswith("\\"):
                    return value
                return None
            emit_index += 1

    return None


def extract_macros_for_db(
    content: str, id_to_name: dict[int, str] | None = None
) -> dict[str, dict]:
    """
    Extract convenience macros from decomp and format for v2 database.

    Returns dict of macro_name -> macro entry in v2 schema format.
    Handles standard macros and opcode-switching conditional macros.
    """
    parsed_macros, _ = parse_scrcmd_inc(content)
    macros = {}

    for name, macro in parsed_macros.items():
        if name in SKIP_MACROS or name in LEVELSCRIPT_MACROS:
            continue

        v2_params = []
        for p in macro.params:
            v2_params.append(
                {
                    "name": p.name,
                    "type": infer_param_type(p.name),
                    **({"default": p.default} if p.default else {}),
                }
            )

        if name in MANUAL_PRIMITIVES:
            _, _, manual_params = MANUAL_PRIMITIVES[name]
            existing_names = {p["name"] for p in v2_params}

            for mp in manual_params:
                if mp["name"] not in existing_names:
                    hardcoded_value = _find_hardcoded_param_value(
                        macro.body, mp["name"], manual_params
                    )
                    v2_params.insert(
                        0,
                        {
                            "name": mp["name"],
                            "type": mp.get("type", "var"),
                            "default": hardcoded_value or "VAR_RESULT",
                        },
                    )

        # 1. Handle Opcode Switchers (Conditional Macros)
        if macro.opcode_switches and id_to_name:
            variants = []
            for cond, opcode in macro.opcode_switches:
                target_cmd = id_to_name.get(opcode, f"UnkCmd_{opcode:04X}")
                # Heuristic: Pass all params to the target command
                args = ", ".join(f"${p.name}" for p in macro.params)
                variants.append(
                    {"condition": cond, "expansion": [f"{target_cmd} {args}"]}
                )

            macros[name] = {"type": "macro", "params": v2_params, "variants": variants}
            continue

        # 2. Handle .ifnb optional params (e.g., TVBroadcastDummy)
        optional_params = detect_ifnb_optional_params(macro.body, macro.params)
        if optional_params:
            # Mark optional params in v2_params
            for i, p in enumerate(macro.params):
                if p.name in optional_params:
                    v2_params[i]["optional"] = True

            # Generate variants for different argument counts
            ifnb_variants = parse_ifnb_expansion_variants(macro.body, macro.params)
            if ifnb_variants:
                macros[name] = {
                    "type": "macro",
                    "params": v2_params,
                    "variants": ifnb_variants,
                }
                continue

        # 3. Handle Standard Macros (Expansion Lines)
        expansion_lines = parse_macro_expansion_lines(macro.body)

        # Check if this macro wraps a primitive (has .short OPCODE /* Name */ pattern or manual override)
        primitive_call = extract_primitive_call_line(macro.body, id_to_name, name)

        # If no expansion lines and no primitive call, skip
        if not expansion_lines and not primitive_call:
            continue

        # Skip if it's a plain command (emits .short but no macro calls AND no primitive comment)
        if any(
            line.strip().startswith(".short") for line in macro.body.split("\n")[:5]
        ):
            has_macro_calls = any(
                line.strip()
                and not line.strip().startswith(".")
                and line.strip()[0].isupper()
                for line in macro.body.split("\n")
            )
            # If no macro calls AND no primitive comment pattern, it's a plain command, skip
            if not has_macro_calls and not primitive_call:
                continue

        # Build the expansion list
        v2_expansion = []

        # Prepend the primitive call if present
        if primitive_call:
            v2_expansion.append(primitive_call)

        # Add the macro call expansion lines
        v2_expansion.extend(
            format_expansion_line(line, macro.params) for line in expansion_lines
        )

        if v2_expansion:
            macros[name] = {
                "type": "macro",
                "params": v2_params,
                "expansion": v2_expansion,
            }

    return macros


def inject_macros_into_db(db_path: str, verbose: bool = False) -> int:
    """
    Fetch macros from decomp and inject into v2 database file.

    Returns number of macros added/updated.
    """
    with open(db_path, "r", encoding="utf-8") as f:
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
    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

    print(f"  Added {added} new macros, updated {updated} existing")
    if verbose and added + updated > 0:
        print(f"  Sample macros:")
        for name in list(macros.keys())[:5]:
            m = macros[name]
            if "expansion" in m:
                print(f"    - {name}: {len(m['expansion'])} expansion lines")
            elif "variants" in m:
                print(f"    - {name}: {len(m['variants'])} variants")

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
    cmd_type: str,
    decomp_primitives: dict[str, tuple[int, list[dict]]] | None = None,
) -> tuple[list, list, list, list, list]:
    """
    Compare parsed decomp macros with database.

    Args:
        db: The database dict
        decomp_macros: Parsed macros from decomp
        cmd_type: Command type to compare (e.g., "script_cmd")
        decomp_primitives: Hidden primitives extracted from comments (opcode, params)

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
    param_updates = []  # Commands that exist but need param updates

    # First, check hidden primitives from comments (like SetHiddenLocation, CheckHasEnoughMonForCatchingShow)
    if decomp_primitives:
        for prim_name, (prim_opcode, prim_params) in decomp_primitives.items():
            if prim_name in db_name_to_id:
                # Primitive exists in DB - check opcode
                if db_name_to_id[prim_name] != prim_opcode:
                    mismatched.append(
                        {
                            "name": prim_name,
                            "decomp_opcode": prim_opcode,
                            "db_opcode": db_name_to_id[prim_name],
                            "is_conditional": False,
                            "all_opcodes": [prim_opcode],
                            "params": [
                                p.get("name", f"arg_{i}")
                                for i, p in enumerate(prim_params)
                            ],
                            "is_hidden_primitive": True,
                        }
                    )
                else:
                    # Name and opcode match - check if params need updating
                    # Compare param count (decomp has more than DB)
                    db_cmd = db_commands.get(prim_name, {})
                    db_params = db_cmd.get("params", [])
                    if len(prim_params) > len(db_params):
                        param_updates.append(
                            {
                                "name": prim_name,
                                "params": prim_params,
                                "is_hidden_primitive": True,
                            }
                        )
            elif prim_opcode in db_id_to_name:
                # Opcode exists with different name - this is a renaming situation
                # The DB has a command (possibly a wrapper name) but decomp says
                # the primitive at this opcode has a different name
                db_name = db_id_to_name[prim_opcode]
                mismatched.append(
                    {
                        "name": prim_name,
                        "decomp_opcode": prim_opcode,
                        "db_name": db_name,
                        "is_conditional": False,
                        "all_opcodes": [prim_opcode],
                        "params": prim_params,  # Pass full param info for hidden primitives
                        "is_hidden_primitive": True,
                    }
                )
            else:
                # Completely missing - add to missing list
                missing.append(
                    {
                        "name": prim_name,
                        "opcode": prim_opcode,
                        "is_conditional": False,
                        "params": [
                            p.get("name", f"arg_{i}") for i, p in enumerate(prim_params)
                        ],
                        "param_types": prim_params,  # Include full param info
                        "variants": [],
                        "is_hidden_primitive": True,
                    }
                )

    for name, macro in decomp_macros.items():
        # Handle wrapper macros
        if macro.is_wrapper and macro.expansion:
            wrappers.append(
                {
                    "name": name,
                    "target": macro.expansion.target_macro,
                    "args": macro.expansion.args,
                    "params": [p.name for p in macro.params],
                }
            )

            # For CallCommonScript wrappers, also add them to missing list
            # with opcode 20 so they get added to the database
            if macro.expansion.target_macro == "CallCommonScript":
                # Extract the script ID constant from the call
                # e.g., "CallCommonScript 0x7D6" -> script ID 0x7D6
                import re

                call_match = re.search(
                    r"CallCommonScript\s+(0x[0-9A-Fa-f]+|\d+)", macro.body
                )
                if call_match:
                    script_id_str = call_match.group(1)
                    try:
                        if script_id_str.startswith("0x"):
                            script_id = int(script_id_str, 16)
                        else:
                            script_id = int(script_id_str)

                        # Add to missing so it gets created in the database
                        missing.append(
                            {
                                "name": name,
                                "opcode": 20,  # CallCommonScript opcode
                                "is_conditional": False,
                                "params": [p.name for p in macro.params],
                                "script_id": script_id,  # Store for documentation
                                "is_wrapper_cmd": True,  # Mark as wrapper command
                            }
                        )
                    except ValueError:
                        pass

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
                mismatched.append(
                    {
                        "name": name,
                        "decomp_opcode": primary_opcode,
                        "db_opcode": db_name_to_id[name],
                        "is_conditional": macro.is_conditional,
                        "all_opcodes": macro.opcodes,
                        "params": [p.name for p in macro.params],
                    }
                )
        elif primary_opcode in db_id_to_name:
            # Opcode exists with different name
            db_name = db_id_to_name[primary_opcode]

            # Skip if this macro wraps a hidden primitive - the primitive is the real command
            if macro.wraps_primitive:
                continue

            # Check if existing DB name is also a valid decomp name (alias)
            # If so, don't rename it (prevent flip-flopping between aliases)
            if db_name in decomp_macros:
                # But only if it's NOT a wrapper - wrappers don't emit opcodes
                # so they shouldn't block adding the actual command
                if not decomp_macros[db_name].is_wrapper:
                    continue

            # Check if DB name is a hidden primitive - don't rename primitives to wrapper names
            if decomp_primitives and db_name in decomp_primitives:
                continue

            mismatched.append(
                {
                    "name": name,
                    "decomp_opcode": primary_opcode,
                    "db_name": db_name,
                    "is_conditional": macro.is_conditional,
                    "all_opcodes": macro.opcodes,
                    "params": [p.name for p in macro.params],
                }
            )
        else:
            # Completely missing
            missing.append(
                {
                    "name": name,
                    "opcode": primary_opcode,
                    "is_conditional": macro.is_conditional,
                    "params": [p.name for p in macro.params],
                    "variants": [
                        {"condition": v.condition, "emits": v.params_emitted}
                        for v in macro.variants
                    ],
                }
            )

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

    return missing, extra, mismatched, wrappers, param_updates


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
            if (
                "default" not in db_params[i]
                or db_params[i]["default"] != arg_def.default
            ):
                db_params[i]["default"] = arg_def.default
                changes = True

    return changes


def update_param_types(db_params: list, macro: ParsedMacro) -> bool:
    """
    Update database parameter types based on actual types from macro body.
    Returns True if changes were made.
    """
    if not macro.emitted_params or not db_params:
        return False

    if len(macro.emitted_params) != len(db_params):
        return False

    # Extract actual types from macro body
    param_names = [p.name for p in macro.params]
    actual_types = extract_param_types(macro.body, param_names)

    changes = False

    # Map by position: DB Param[i] corresponds to Emitted Param[i]
    for i, emitted_name in enumerate(macro.emitted_params):
        if emitted_name not in actual_types:
            continue

        actual_type = actual_types[emitted_name]
        current_type = db_params[i].get("type", "u16")

        # Prefer "var" type for parameters with "var" in their name
        if "var" in emitted_name.lower():
            actual_type = "var"

        # Only update if types differ
        if current_type != actual_type:
            db_params[i]["type"] = actual_type
            changes = True

    return changes


def update_db_from_sync(
    db: dict, missing: list, mismatched: list, cmd_type: str
) -> int:
    """
    Update database based on sync results.
    Returns number of changes made.
    """
    commands = db.get("commands", {})
    changes = 0

    # Handle mismatches (Potential Renames or Opcode fixes)
    for item in mismatched:
        decomp_name = item["name"]

        # skip if decomp name is unused/placeholder
        if is_placeholder_name(decomp_name):
            continue

        if "db_name" in item:
            # ID match, Name mismatch -> Rename
            old_name = item["db_name"]

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

                # For hidden primitives, also update params from the primitive info
                if item.get("is_hidden_primitive") and item.get("params"):
                    data["params"] = [
                        {
                            "name": p.get("name", f"arg_{i}")
                            if isinstance(p, dict)
                            else p,
                            "type": p.get("type", "u16")
                            if isinstance(p, dict)
                            else "u16",
                        }
                        for i, p in enumerate(item["params"])
                    ]

                changes += 1
                print(f"    Renamed {old_name} -> {decomp_name}")

        elif "db_opcode" in item:
            # Name match, Opcode mismatch -> Update Opcode
            # This is dangerous if ID is the primary key in some contexts, but here Name is key
            if decomp_name in commands:
                commands[decomp_name]["id"] = item["decomp_opcode"]
                changes += 1
                print(
                    f"    Updated opcode for {decomp_name}: {item['db_opcode']} -> {item['decomp_opcode']}"
                )
            else:
                # Key missing? Might have been renamed away in this same batch.
                # Treat as new command.
                entry = {
                    "type": cmd_type,
                    "id": item["decomp_opcode"],
                    "description": f"Imported from decomp: {decomp_name}",
                    "params": [],
                }

                if item.get("params"):
                    entry["params"] = [
                        {
                            "name": p.name if hasattr(p, "name") else p,
                            "type": infer_param_type(
                                p.name if hasattr(p, "name") else p
                            ),
                        }
                        for p in item["params"]
                    ]

                commands[decomp_name] = entry
                changes += 1
                print(
                    f"    Re-added {decomp_name} (0x{item['decomp_opcode']:04X}) after rename collision"
                )

    # Handle missing (New Commands)
    for item in missing:
        name = item["name"]
        if is_placeholder_name(name):
            continue

        # For CallCommonScript wrappers, add as macro with expansion
        if item.get("is_wrapper_cmd") and item.get("script_id"):
            script_id = item["script_id"]
            script_id_str = (
                f"0x{script_id:03X}" if script_id < 0x1000 else f"0x{script_id:04X}"
            )

            # Build expansion lines
            expansion = []
            if item.get("params"):
                # Has params - need to format them (e.g., "SetVar VAR_0x8007, $nurseLocalID")
                for p in item["params"]:
                    pname = p.name if hasattr(p, "name") else p
                    # Check if this is a special SetVar call for CallCommonScript
                    if name == "CallPokecenterNurse":
                        expansion.append(f"SetVar VAR_0x8007, ${pname}")
                    else:
                        # Just pass through the param
                        pass
            expansion.append(f"CallCommonScript {script_id_str}")

            # Create macro entry
            entry = {
                "type": "macro",
                "description": f"Convenience wrapper for CallCommonScript {script_id_str}",
                "params": [],
                "expansion": expansion,
            }

            # Add params if available
            if item.get("params"):
                entry["params"] = [
                    {
                        "name": p.name if hasattr(p, "name") else p,
                        "type": infer_param_type(p.name if hasattr(p, "name") else p),
                    }
                    for p in item["params"]
                ]

            commands[name] = entry
            changes += 1
            print(f"    Added new macro: {name}")
            continue

        # Create description for regular commands
        description = f"Imported from decomp: {name}"

        # Create new entry
        entry = {
            "type": cmd_type,
            "id": item["opcode"],
            "description": description,
            "params": [],
        }

        # Add params if available
        if item.get("params"):
            entry["params"] = [
                {
                    "name": p.name if hasattr(p, "name") else p,
                    "type": infer_param_type(p.name if hasattr(p, "name") else p),
                }
                for p in item["params"]
            ]

        if item.get("is_conditional"):
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
            "macro": 3,
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
    with open(db_path, "r", encoding="utf-8") as f:
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
            decomp_macros, decomp_primitives = parse_scrcmd_inc(content)

            # Count types
            simple = sum(
                1
                for m in decomp_macros.values()
                if not m.is_conditional and not m.is_wrapper
            )
            conditional = sum(1 for m in decomp_macros.values() if m.is_conditional)
            wrapper = sum(1 for m in decomp_macros.values() if m.is_wrapper)

            print(
                f"  Parsed {len(decomp_macros)} macros: {simple} simple, {conditional} conditional, {wrapper} wrapper"
            )
            if decomp_primitives:
                print(
                    f"  Found {len(decomp_primitives)} hidden primitives from comments"
                )

            missing, extra, mismatched, wrappers, param_updates = (
                compare_macros_with_db(
                    db, decomp_macros, "script_cmd", decomp_primitives
                )
            )

            if missing:
                print(f"  Missing in DB: {len(missing)}")
                for item in missing[:10]:
                    cond_str = " (conditional)" if item.get("is_conditional") else ""
                    prim_str = (
                        " (hidden primitive)" if item.get("is_hidden_primitive") else ""
                    )
                    print(
                        f"    - {item['name']} (0x{item['opcode']:04X}){cond_str}{prim_str}"
                    )
                    if verbose and item.get("variants"):
                        for v in item["variants"]:
                            print(f"        when {v['condition']}: emits {v['emits']}")
                if len(missing) > 10:
                    print(f"    ... and {len(missing) - 10} more")
                has_changes = True

            if mismatched:
                print(f"  Name/opcode mismatches: {len(mismatched)}")
                for item in mismatched[:10]:
                    if "db_name" in item:
                        cond_str = " (conditional)" if item["is_conditional"] else ""
                        print(
                            f"    - Decomp '{item['name']}' vs DB '{item['db_name']}' (0x{item['decomp_opcode']:04X}){cond_str}"
                        )
                    else:
                        print(
                            f"    - {item['name']}: decomp=0x{item['decomp_opcode']:04X}, db=0x{item['db_opcode']:04X}"
                        )
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
                    with open(db_path, "w", encoding="utf-8") as f:
                        json.dump(db, f, indent=2)

            # Apply param updates for hidden primitives
            if update and param_updates:
                db_commands = db.get("commands", {})
                param_updates_count = 0
                for item in param_updates:
                    name = item["name"]
                    if name in db_commands:
                        new_params = [
                            {
                                "name": p.get("name", f"arg_{i}")
                                if isinstance(p, dict)
                                else str(p),
                                "type": p.get("type", "u16")
                                if isinstance(p, dict)
                                else "u16",
                            }
                            for i, p in enumerate(item["params"])
                        ]
                        db_commands[name]["params"] = new_params
                        param_updates_count += 1
                        print(
                            f"    Updated params for {name}: {len(new_params)} params"
                        )
                if param_updates_count > 0:
                    with open(db_path, "w", encoding="utf-8") as f:
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
                    if (
                        macro.is_wrapper
                        or macro.is_conditional
                        or len(macro.opcodes) > 1
                    ):
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
                    with open(db_path, "w", encoding="utf-8") as f:
                        json.dump(db, f, indent=2)

            # Update parameter types from macro body directives
            if update:
                print("  Checking parameter types...")
                types_updated = 0
                db_commands = db.get("commands", {})
                # Rebuild map as updates might have changed things
                db_name_to_id = {
                    name: data["id"]
                    for name, data in db_commands.items()
                    if data.get("type") == "script_cmd"
                }
                db_id_to_name = {v: k for k, v in db_name_to_id.items()}

                for name, macro in decomp_macros.items():
                    if (
                        macro.is_wrapper
                        or macro.is_conditional
                        or len(macro.opcodes) > 1
                    ):
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
                        if update_param_types(cmd.get("params", []), macro):
                            types_updated += 1

                if types_updated > 0:
                    print(f"  Updated types for {types_updated} commands")
                    has_changes = True
                    with open(db_path, "w", encoding="utf-8") as f:
                        json.dump(db, f, indent=2)

            # Update params with hardcoded defaults (for unused params like `.short 0`)
            if update:
                print("  Checking for hardcoded/unused params...")
                unused_params_updated = 0
                db_commands = db.get("commands", {})
                # Rebuild map as updates might have changed things
                db_name_to_id = {
                    name: data["id"]
                    for name, data in db_commands.items()
                    if data.get("type") == "script_cmd"
                }
                db_id_to_name = {v: k for k, v in db_name_to_id.items()}

                for name, macro in decomp_macros.items():
                    if (
                        macro.is_wrapper
                        or macro.is_conditional
                        or len(macro.opcodes) > 1
                    ):
                        continue
                    if not macro.opcodes or not macro.all_emitted_values:
                        continue

                    has_hardcoded = any(
                        v.get("is_literal") for v in macro.all_emitted_values
                    )
                    emitted_names = [
                        v.get("name")
                        for v in macro.all_emitted_values
                        if not v.get("is_literal")
                    ]
                    has_duplicates = len(emitted_names) != len(set(emitted_names))

                    if not has_hardcoded and not has_duplicates:
                        continue

                    # Find corresponding DB command
                    target_name = None
                    if name in db_name_to_id:
                        target_name = name
                    elif macro.opcodes[0] in db_id_to_name:
                        target_name = db_id_to_name[macro.opcodes[0]]

                    if target_name and target_name in db_commands:
                        cmd = db_commands[target_name]
                        db_params = cmd.get("params", [])
                        decomp_params = macro.all_emitted_values

                        # Check if DB has correct number of params with defaults
                        if len(decomp_params) == len(db_params):
                            seen_param_names: dict[str, int] = {}
                            changed = False
                            for i, decomp_p in enumerate(decomp_params):
                                db_name = db_params[i].get("name", "")
                                decomp_name = decomp_p.get("name", "")

                                if (
                                    decomp_name
                                    and not decomp_p.get("is_literal")
                                    and decomp_name in seen_param_names
                                ):
                                    first_idx = seen_param_names[decomp_name]
                                    first_param_name = db_params[first_idx].get(
                                        "name", decomp_name
                                    )
                                    db_params[i]["default"] = f"${first_param_name}"
                                    changed = True
                                elif decomp_name and not decomp_p.get("is_literal"):
                                    seen_param_names[decomp_name] = i

                                if decomp_name and (
                                    db_name.startswith("???")
                                    or (
                                        db_name in ("variable", "value", "arg")
                                        and decomp_name
                                        not in ("variable", "value", "arg")
                                    )
                                ):
                                    db_params[i]["name"] = decomp_name
                                    changed = True
                                if (
                                    decomp_p.get("is_literal")
                                    and decomp_p.get("default") is not None
                                ):
                                    if (
                                        db_params[i].get("default")
                                        != decomp_p["default"]
                                    ):
                                        db_params[i]["default"] = decomp_p["default"]
                                        changed = True
                            if changed:
                                unused_params_updated += 1
                        elif len(decomp_params) > len(db_params):
                            # DB is missing some params - this shouldn't happen normally
                            # but could occur if DB was manually truncated
                            pass

                if unused_params_updated > 0:
                    print(
                        f"  Updated {unused_params_updated} commands with hardcoded param defaults"
                    )
                    has_changes = True
                    with open(db_path, "w", encoding="utf-8") as f:
                        json.dump(db, f, indent=2)

            if wrappers and verbose:
                print(f"  Wrapper macros found: {len(wrappers)}")
                for w in wrappers[:5]:
                    args = ", ".join(w["args"]) if w["args"] else ""
                    print(
                        f"    - {w['name']}({', '.join(w['params'])}) -> {w['target']}({args})"
                    )
                if len(wrappers) > 5:
                    print(f"    ... and {len(wrappers) - 5} more")

            if extra:
                print(f"  Extra in DB (not in decomp): {len(extra)}")

    # Sync movements
    if "movement" in sources:
        print("  Fetching movement.inc...")
        content = fetch_url(sources["movement"])
        if content:
            # Fetch movement action constants for resolving opcodes
            movement_constants = get_movement_action_constants()
            decomp_moves = parse_movement_inc(content, movement_constants)
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

            for name, (opcode, move_params) in decomp_moves.items():
                if name in db_name_to_id:
                    if opcode is not None and db_name_to_id[name] != opcode:
                        mismatched.append(
                            {
                                "name": name,
                                "decomp_opcode": opcode,
                                "db_opcode": db_name_to_id[name],
                                "params": move_params,
                            }
                        )
                elif opcode is not None and opcode in db_id_to_name:
                    db_name = db_id_to_name[opcode]
                    # Check if existing DB name is also a valid decomp name (alias)
                    if db_name in decomp_moves:
                        continue

                    mismatched.append(
                        {
                            "name": name,
                            "decomp_opcode": opcode,
                            "db_name": db_name,
                            "params": move_params,
                        }
                    )
                else:
                    if opcode is not None:
                        missing.append(
                            {"name": name, "opcode": opcode, "params": move_params}
                        )

            if missing:
                print(f"  Missing movements in DB: {len(missing)}")
                for item in missing[:5]:
                    opcode = item["opcode"]
                    opcode_str = (
                        f"0x{opcode:02X}" if opcode is not None else "(symbolic)"
                    )
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
                    with open(db_path, "w", encoding="utf-8") as f:
                        json.dump(db, f, indent=2)

            # Update movement params if requested
            if update:
                print("  Checking movement params...")
                params_updated = 0
                db_commands = db.get("commands", {})

                for name, (opcode, move_params) in decomp_moves.items():
                    if name not in db_commands:
                        continue

                    cmd = db_commands[name]
                    if cmd.get("type") != "movement":
                        continue

                    # Build expected params list from decomp
                    decomp_param_list = []
                    for p in move_params:
                        decomp_param_list.append(
                            {
                                "name": p.name,
                                "type": infer_param_type(p.name),
                                **({"default": p.default} if p.default else {}),
                            }
                        )

                    # Compare with current params
                    current_params = cmd.get("params", [])
                    if current_params != decomp_param_list:
                        cmd["params"] = decomp_param_list
                        params_updated += 1

                if params_updated > 0:
                    print(f"  Updated params for {params_updated} movements")
                    has_changes = True
                    with open(db_path, "w", encoding="utf-8") as f:
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
                        print(
                            f"    - {c['name']}: {c['issue']} (current={c['current']}, should be={c['should_be']})"
                        )
                        print(f"      Reason: {c['reason']}")
                    has_changes = True

                if ls_mismatched and verbose:
                    print(f"  Levelscript mismatches: {len(ls_mismatched)}")
                    for m in ls_mismatched:
                        print(
                            f"    - {m['name']}: {m['issue']} decomp={m['decomp']}, db={m['db']}"
                        )

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

    macros, primitives = parse_scrcmd_inc(content)

    # Group by type
    simple = []
    conditional = []
    opcode_switch = []
    wrappers = []

    for name, macro in sorted(
        macros.items(), key=lambda x: x[1].opcodes[0] if x[1].opcodes else 9999
    ):
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
        params = ", ".join(
            p.name + (f"={p.default}" if p.default else "") for p in m.params
        )
        print(f"  0x{m.opcodes[0]:04X} {m.name}({params})")
    if len(simple) > 20:
        print(f"  ... and {len(simple) - 20} more")

    print(f"\n=== Conditional Commands ({len(conditional)}) ===")
    for m in conditional:
        params = ", ".join(
            p.name + (f"={p.default}" if p.default else "") for p in m.params
        )
        print(f"  0x{m.opcodes[0]:04X} {m.name}({params})")
        for v in m.variants:
            print(f"      when {v.condition}: emits {v.params_emitted}")

    print(f"\n=== Opcode-Switching Commands ({len(opcode_switch)}) ===")
    for m in opcode_switch:
        params = ", ".join(p.name for p in m.params)
        opcodes = ", ".join(f"0x{op:04X}" for op in m.opcodes)
        print(f"  {m.name}({params}) -> [{opcodes}]")

    print(f"\n=== Wrapper Macros ({len(wrappers)}) ===")
    for m in wrappers[:20]:
        params = ", ".join(p.name for p in m.params)
        if m.expansion:
            args = ", ".join(m.expansion.args) if m.expansion.args else ""
            print(f"  {m.name}({params}) -> {m.expansion.target_macro}({args})")
        else:
            print(f"  {m.name}({params}) -> ???")
    if len(wrappers) > 20:
        print(f"  ... and {len(wrappers) - 20} more")

    # Print hidden primitives (commands only defined via comments)
    if primitives:
        print(f"\n=== Hidden Primitives ({len(primitives)}) ===")
        for name, (opcode, params) in sorted(primitives.items(), key=lambda x: x[1][0]):
            param_str = ", ".join(p.get("name", "?") for p in params) if params else ""
            print(f"  0x{opcode:04X} {name}({param_str})")


def main():
    parser = argparse.ArgumentParser(
        description="Sync database with decomp project definitions"
    )
    parser.add_argument("database", nargs="?", help="Path to v2 database file")
    parser.add_argument(
        "--all", action="store_true", help="Sync all *_v2.json files in the repository"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update database with decomp names (not yet implemented)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show more details including wrapper macros and variant conditions",
    )
    parser.add_argument(
        "--dump",
        metavar="GAME",
        help="Dump all parsed macros for a game (platinum, hgss)",
    )
    parser.add_argument(
        "--inject-macros",
        action="store_true",
        default=True,
        help="Inject convenience macros from decomp into the v2 database (default: enabled)",
    )
    parser.add_argument(
        "--no-inject-macros",
        action="store_true",
        help="Disable automatic macro injection",
    )

    args = parser.parse_args()

    if args.dump:
        dump_macros(args.dump)
        return 0

    if args.inject_macros and not args.no_inject_macros:
        if args.all:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            v2_files = [
                os.path.join(repo_root, f)
                for f in os.listdir(repo_root)
                if f.endswith("_v2.json")
            ]

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

            if args.update:
                for path in sorted(v2_files):
                    sync_database(path, args.update, args.verbose)
            return 0
        elif args.database:
            print(f"Injecting macros into {args.database}")
            inject_macros_into_db(args.database, args.verbose)
            if args.update:
                sync_database(args.database, args.update, args.verbose)
            return 0
        else:
            print("Error: --inject-macros requires --all or a database path")
            return 1

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
