#!/usr/bin/env python3
"""
Import decomp-derived data into every v2 database in the repository.

This script refreshes script commands, movements, parameter metadata, flags,
variables, and convenience macros for every `*_v2.json` file under the repo
root and `custom_databases/`.
"""

import json
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from flags_vars import sync_flags_vars

# Decomp repo URLs for each game
DECOMP_SOURCES = {
    "Platinum": {
        "scrcmd": "https://raw.githubusercontent.com/pret/pokeplatinum/main/asm/macros/scrcmd.inc",
        "movement": "https://raw.githubusercontent.com/pret/pokeplatinum/main/asm/macros/movement.inc",
        "vars_flags": "https://raw.githubusercontent.com/pret/pokeplatinum/main/generated/vars_flags.txt",
    },
    "HeartGold/SoulSilver": {
        "scrcmd": "https://raw.githubusercontent.com/pret/pokeheartgold/master/asm/macros/script.inc",
        "movement": "https://raw.githubusercontent.com/pret/pokeheartgold/master/asm/macros/movement.inc",
        "flags": "https://raw.githubusercontent.com/pret/pokeheartgold/master/include/constants/flags.h",
        "vars": "https://raw.githubusercontent.com/pret/pokeheartgold/master/include/constants/vars.h",
    },
    # Diamond/Pearl decomp isn't as mature, skip for now
}

PLATINUM_SCRCMD_HEADER_URL = "https://raw.githubusercontent.com/pret/pokeplatinum/main/include/data/scripts/scrcmd.h"
PLATINUM_MOVEMENT_ACTIONS_URL = "https://raw.githubusercontent.com/pret/pokeplatinum/main/generated/movement_actions.txt"

# Helper/utility macro names to skip (they don't emit actual script commands)
SKIP_MACROS = {
    "scrdef",
    "map_script",
    "ScriptEntry",
    "ScriptEntryEnd",
    "save_game_normal",
    "script_entry",
    "script_entry_fixed",
    "script_entry_go_to_if_equal",
    # Levelscript macros - these are handled separately and should not be synced
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


# Param defaults to remove after decomp sync, keyed by game version then command name.
# Each value is a set of parameter names whose "default" key should be deleted.
#
# Use this for cases where the decomp macro declares an optional parameter but every
# actual script always supplies it explicitly in binary order, so making it required
# is more correct for rotom's argument-reorder logic.
DECOMP_PARAM_DEFAULT_REMOVALS: dict[str, dict[str, set[str]]] = {
    "HeartGold/SoulSilver": {
        # All 115 HGSS field scripts supply 'door' explicitly; the decomp default is
        # never exercised.  Removing it makes the transpiler skip reordering for Warp
        # (optional_indices becomes empty) so 5-arg binary-order calls pass through
        # unchanged, which is what we want.
        "Warp": {"door"},
    },
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
    description: str | None = None
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
class RawMacroDefinition:
    """A raw macro definition extracted from the decomp source."""

    name: str
    params_str: str
    body: str
    description: str | None = None


@lru_cache(maxsize=None)
def fetch_url(url: str) -> str | None:
    """Fetch content from URL, return None on error."""
    try:
        with urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except URLError as e:
        print(f"  Warning: Failed to fetch {url}: {e}")
        return None


def _extract_preceding_comment_block(content: str, start: int) -> str | None:
    """Extract contiguous comment lines immediately above a macro definition."""
    line_start = content.rfind("\n", 0, start) + 1
    lines = content[:line_start].splitlines()
    if not lines:
        return None

    description_lines: list[str] = []
    saw_comment = False

    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            if saw_comment:
                break
            return None

        comment_text = None
        if stripped.startswith("//"):
            comment_text = stripped[2:].strip()
        elif stripped.startswith(";"):
            comment_text = stripped[1:].strip()
        elif stripped.startswith("/*") and stripped.endswith("*/"):
            comment_text = stripped.removeprefix("/*").removesuffix("*/").strip()

        if comment_text is None:
            if saw_comment:
                break
            return None

        description_lines.append(comment_text)
        saw_comment = True

    if not description_lines:
        return None

    description = " ".join(reversed(description_lines))
    description = re.sub(r"\s+", " ", description).strip()
    return description or None


def extract_macro_definitions(content: str) -> list[RawMacroDefinition]:
    """Extract macro definitions with optional preceding comment descriptions."""
    # Pattern to match .macro Name [params] ... .endm
    # Use [ \t]* instead of \s* to avoid consuming newlines before params
    macro_pattern = re.compile(
        r"\.macro\s+(\w+)[ \t]*([^\n]*)\n(.*?)\.endm", re.MULTILINE | re.DOTALL
    )

    macros: list[RawMacroDefinition] = []
    for match in macro_pattern.finditer(content):
        name = match.group(1)
        params_str = match.group(2).strip()
        body = match.group(3)
        description = _extract_preceding_comment_block(content, match.start())
        macros.append(
            RawMacroDefinition(
                name=name,
                params_str=params_str,
                body=body,
                description=description,
            )
        )

    return macros


def extract_macros(content: str) -> list[tuple[str, str, str]]:
    """
    Extract all macro definitions from content.

    Returns list of (name, params_str, body) tuples.
    """
    return [
        (macro.name, macro.params_str, macro.body)
        for macro in extract_macro_definitions(content)
    ]


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


def extract_emitted_arg_expressions(body: str) -> list[str]:
    r"""
    Extract emitted argument expressions from a macro body after the opcode.

    Returns normalized expressions like `$scope * 3 + $page` or `$record`.
    Stops at conditionals or nested macro calls because the emitted shape is no
    longer a single linear primitive call.
    """
    expressions = []
    lines = body.split("\n")
    first_short_seen = False

    for line in lines:
        line = line.strip()
        if not line or line.startswith((";", "/*", "@", "#")):
            continue

        if line.startswith((".if", ".else", ".endif", ".macro", ".endm")):
            break

        if not line.startswith(".") and line[0].isupper():
            break

        match = re.match(r"\.(\w+)\s+(.+?)(?:\s*[@;/].*)?$", line)
        if not match:
            continue

        directive = match.group(1).lower()
        if directive not in {"byte", "short", "2byte", "hword", "word", "long"}:
            continue

        value_str = match.group(2).strip()
        if directive in ("short", "2byte", "hword") and not first_short_seen:
            first_short_seen = True
            continue

        expressions.append(re.sub(r"\\(\w+)", r"$\1", value_str))

    return expressions


def is_simple_emit_arg_expression(expression: str) -> bool:
    """Return True when an emitted argument is a plain param reference or literal."""
    expr = expression.strip()
    if not expr:
        return False

    if re.fullmatch(r"\$\w+", expr):
        return True
    if re.fullmatch(r"-?(?:0x[0-9A-Fa-f]+|\d+)", expr):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", expr):
        return True

    return False


def build_custom_call_shape_variants(macro: ParsedMacro) -> list[dict] | None:
    r"""
    Build variant metadata for commands whose macro call shape differs from the
    emitted binary argument shape.

    This captures simple packing transforms like:
        .short \scope * 3 + \page
    """
    if macro.is_wrapper or macro.is_conditional or len(macro.opcodes) != 1:
        return None
    if not macro.params:
        return None

    emit_args = extract_emitted_arg_expressions(macro.body)
    if not emit_args:
        return None

    has_nontrivial_expression = any(
        not is_simple_emit_arg_expression(arg) for arg in emit_args
    )
    if not has_nontrivial_expression:
        return None

    # Only a custom call shape when macro arity differs from emitted arity.
    # Simple assembler expressions like `offset-.-4` keep the same arity and
    # do not represent an alternate calling convention.
    if len(macro.params) == len(emit_args):
        return None

    variant_params = []
    for p in macro.params:
        entry = {"name": p.name, "type": infer_param_type(p.name)}
        if p.default:
            entry["default"] = p.default
        variant_params.append(entry)

    return [
        {
            "params": variant_params,
            "condition": f"{len(macro.params)} args",
            "emit_args": emit_args,
        }
    ]


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


def parse_scrcmd_symbol_table(script_commands_header_content: str) -> dict[str, int]:
    """
    Parse pokeplatinum's script command header and build SCRCMD -> opcode order.

    The first entry is opcode 0 and increments by 1.
    """
    symbol_to_opcode: dict[str, int] = {}
    entry_pattern = re.compile(
        r"(?:ScriptCommand|ScriptCommandTableEntry)\(\s*((?:SCRCMD_|ScrCmd_)[A-Za-z0-9_]+)\s*,",
        re.MULTILINE,
    )

    for opcode, match in enumerate(
        entry_pattern.finditer(script_commands_header_content)
    ):
        symbol = match.group(1)
        symbol_to_opcode[symbol] = opcode

    return symbol_to_opcode


@lru_cache(maxsize=1)
def get_platinum_scrcmd_symbol_table() -> dict[str, int] | None:
    """Fetch and cache Platinum's SCRCMD symbol table."""
    header_content = fetch_url(PLATINUM_SCRCMD_HEADER_URL)
    if not header_content:
        return None

    mapping = parse_scrcmd_symbol_table(header_content)
    return mapping or None


def parse_movement_action_table(movement_actions_content: str) -> dict[str, int]:
    """
    Parse pokeplatinum's movement actions list and build MOVEMENT_ACTION -> opcode order.

    The first entry is opcode 0 and increments by 1.
    """
    symbol_to_opcode: dict[str, int] = {}
    entry_pattern = re.compile(
        r"^\s*(MOVEMENT_ACTION_[A-Z0-9_]+)\b",
        re.MULTILINE,
    )

    for opcode, match in enumerate(entry_pattern.finditer(movement_actions_content)):
        symbol = match.group(1)
        symbol_to_opcode[symbol] = opcode

    return symbol_to_opcode


def _parse_opcode_token(
    value: str, scrcmd_symbol_to_opcode: dict[str, int] | None = None
) -> int | None:
    """Parse an opcode token that can be numeric or SCRCMD_* symbolic."""
    token = value.strip()

    if token.startswith("0x"):
        try:
            return int(token, 16)
        except ValueError:
            return None

    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return None

    if scrcmd_symbol_to_opcode and token in scrcmd_symbol_to_opcode:
        return scrcmd_symbol_to_opcode[token]

    return None


def extract_opcodes(
    body: str, scrcmd_symbol_to_opcode: dict[str, int] | None = None
) -> list[int]:
    """Extract opcode emissions from macro body (numeric or SCRCMD_* symbolic)."""
    opcode_pattern = re.compile(r"\.(?:short|2byte|hword)\s+([A-Za-z0-9_]+)")

    opcodes = []
    for match in opcode_pattern.finditer(body):
        token = match.group(1)
        opcode = _parse_opcode_token(token, scrcmd_symbol_to_opcode)
        if opcode is None and token.startswith("SCRCMD_"):
            return []
        if opcode is not None:
            opcodes.append(opcode)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for op in opcodes:
        if op not in seen:
            seen.add(op)
            unique.append(op)

    return unique


def extract_first_opcode(
    body: str, scrcmd_symbol_to_opcode: dict[str, int] | None = None
) -> int | None:
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
        match = re.match(r"\.(?:short|2byte|hword)\s+([A-Za-z0-9_]+)", line)
        if match:
            return _parse_opcode_token(match.group(1), scrcmd_symbol_to_opcode)
    return None


def extract_primitive_from_comment(
    body: str, scrcmd_symbol_to_opcode: dict[str, int] | None = None
) -> tuple[int, str] | None:
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
        r"\.(?:short|2byte|hword)\s+([A-Za-z0-9_]+)\s*/\*\s*([A-Z][a-zA-Z0-9_]+)\s*\*/",
        re.MULTILINE,
    )

    match = pattern.search(body)
    if match:
        opcode = _parse_opcode_token(match.group(1), scrcmd_symbol_to_opcode)
        if opcode is None:
            return None
        primitive_name = match.group(2)
        return (opcode, primitive_name)

    return None


def extract_all_primitives_from_comments(
    body: str, scrcmd_symbol_to_opcode: dict[str, int] | None = None
) -> list[tuple[int, str]]:
    """
    Extract ALL primitive command names from .short OPCODE /* PrimitiveName */ patterns.

    This handles opcode-switching macros like SetVar which reference multiple primitives:
        .short 40 /* SetVarFromValue */
        .short 41 /* SetVarFromVar */

    Returns list of (opcode, primitive_name) tuples.
    """
    pattern = re.compile(
        r"\.(?:short|2byte|hword)\s+([A-Za-z0-9_]+)\s*/\*\s*([A-Z][a-zA-Z0-9_]+)\s*\*/",
        re.MULTILINE,
    )

    primitives = []
    for match in pattern.finditer(body):
        opcode = _parse_opcode_token(match.group(1), scrcmd_symbol_to_opcode)
        if opcode is None:
            continue
        primitive_name = match.group(2)
        primitives.append((opcode, primitive_name))

    return primitives


def extract_primitive_params(
    body: str, scrcmd_symbol_to_opcode: dict[str, int] | None = None
) -> list[dict]:
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
            match = re.match(r"\.(?:short|2byte|hword)\s+([A-Za-z0-9_]+)\s*/\*", line)
            if (
                match
                and _parse_opcode_token(match.group(1), scrcmd_symbol_to_opcode)
                is not None
            ):
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
    body: str,
    id_to_name: dict[int, str] | None = None,
    macro_name: str | None = None,
    scrcmd_symbol_to_opcode: dict[str, int] | None = None,
) -> str | None:
    """
    Extract a primitive call expansion line from a macro body.

    For macros like:
        .short 736 /* CheckAmitySquareManGiftIsAccessory */
        .short \\giftID
        .short VAR_RESULT
        GoToIfEq VAR_RESULT, FALSE, \\offset

    Returns: "CheckAmitySquareManGiftIsAccessory $giftID, VAR_RESULT"

    Returns None if no primitive comment pattern is found and no manual override exists.
    """
    # First, find the primitive name and opcode from comment
    primitive_info = extract_primitive_from_comment(body, scrcmd_symbol_to_opcode)

    if not primitive_info:
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
            match = re.match(r"\.(?:short|2byte|hword)\s+([A-Za-z0-9_]+)\s*/\*", line)
            if (
                match
                and _parse_opcode_token(match.group(1), scrcmd_symbol_to_opcode)
                is not None
            ):
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


def detect_opcode_switching(
    body: str, scrcmd_symbol_to_opcode: dict[str, int] | None = None
) -> list[tuple[str, int]]:
    """
    Detect if macro emits different opcodes based on conditions.

    Returns list of (condition, opcode) pairs.
    """
    switches = []

    # Pattern: .if CONDITION followed by .short OPCODE_TOKEN
    if_pattern = re.compile(
        r"\.if\s+(.+?)\n\s*\.(?:short|2byte|hword)\s+([A-Za-z0-9_]+)(?:\s*/\*\s*(\w+))?",
        re.MULTILINE,
    )

    for match in if_pattern.finditer(body):
        condition = match.group(1).strip()
        opcode = _parse_opcode_token(match.group(2), scrcmd_symbol_to_opcode)
        if opcode is not None:
            switches.append((condition, opcode))

    # Pattern: .else followed by .short OPCODE_TOKEN
    else_pattern = re.compile(
        r"\.else\s*\n\s*\.(?:short|2byte|hword)\s+([A-Za-z0-9_]+)(?:\s*/\*\s*(\w+))?",
        re.MULTILINE,
    )

    for match in else_pattern.finditer(body):
        opcode = _parse_opcode_token(match.group(1), scrcmd_symbol_to_opcode)
        if opcode is not None:
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
        if re.search(r"\.(?:short|2byte|hword)\s+[A-Za-z0-9_]+\s*/\*", block):
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


def _split_body_into_sequential_if_blocks(body: str) -> list[str]:
    """
    Split a macro body into sequential independent top-level if/else/endif groups.

    Returns a list of body strings (one per top-level block) when the body has two
    or more independent if/else chains at the same nesting level (like ItemVars).
    Returns an empty list when there is only one block, no blocks, or when lines
    exist outside all if blocks (which cannot be handled as a simple product).
    """
    groups: list[list[str]] = []
    current_group: list[str] = []
    nesting = 0
    has_lines_outside = False

    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(
            (".macro", ".endm", ";", "/*", "@", "#")
        ):
            continue

        if stripped.startswith(".if "):
            nesting += 1
            current_group.append(line)
        elif stripped.startswith(".endif"):
            current_group.append(line)
            nesting -= 1
            if nesting == 0:
                groups.append(list(current_group))
                current_group = []
        elif nesting > 0:
            current_group.append(line)
        else:
            has_lines_outside = True

    if has_lines_outside or current_group or len(groups) < 2:
        return []

    return ["\n".join(g) for g in groups]


def _cartesian_product_variants(groups: list[list[dict]]) -> list[dict]:
    """
    Compute the Cartesian product of multiple if/else variant groups.

    Combines conditions with ' && ' (dropping any 'else' sub-conditions) and
    concatenates expansions. The last combination is always labelled 'else'.
    """
    from itertools import product as itertools_product

    all_combos = list(itertools_product(*groups))
    result = []
    for i, combo in enumerate(all_combos):
        is_last = i == len(all_combos) - 1
        if is_last:
            condition = "else"
        else:
            non_else = [v["condition"] for v in combo if v["condition"] != "else"]
            condition = " && ".join(non_else) if non_else else "else"
        expansion: list[str] = []
        for v in combo:
            expansion.extend(v.get("expansion", []))
        result.append({"condition": condition, "expansion": expansion})
    return result


def parse_if_else_macro_variants(
    body: str, params: list[MacroParam]
) -> list[dict] | None:
    """
    Parse .if/.else conditionals that call different macros based on condition.

    Also handles `.if ... .endif` blocks with no `.else`, preserving the base
    expansion as an implicit `else` branch when macro call lines exist outside
    the conditional block.

    For macros with multiple sequential independent if/else chains (e.g. ItemVars),
    computes the Cartesian product so every combination of branches is covered by
    a single variant.

    Returns list of variants with condition and expansion, or None if not applicable.

    Example input:
        .if \\valueOrVarID < VARS_START
            CompareVarToValue \\varID, \\valueOrVarID
        .else
            CompareVarToVar \\varID, \\valueOrVarID
        .endif

    Returns:
        [
            {"condition": "valueOrVarID < VARS_START", "expansion": ["CompareVarToValue $varID, $valueOrVarID"]},
            {"condition": "else", "expansion": ["CompareVarToVar $varID, $valueOrVarID"]}
        ]
    """
    lines = body.split("\n")

    has_if = any(line.strip().startswith(".if ") for line in lines)
    has_endif = any(line.strip().startswith(".endif") for line in lines)

    if not (has_if and has_endif):
        return None

    has_short = any(line.strip().startswith(".short") for line in lines)
    if has_short:
        return None

    # Handle macros with multiple sequential independent if/else chains (e.g. ItemVars).
    sequential_groups = _split_body_into_sequential_if_blocks(body)
    if sequential_groups:
        group_variants_list: list[list[dict]] = []
        for group_body in sequential_groups:
            group_variants = parse_if_else_macro_variants(group_body, params)
            if group_variants is None:
                break
            group_variants_list.append(group_variants)
        else:
            if len(group_variants_list) >= 2:
                return _cartesian_product_variants(group_variants_list)

    variants = []
    base_expansion = []
    current_condition = None
    current_expansion = []
    in_conditional = False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "/*", "@", "#")):
            continue
        if stripped.startswith(".macro ") or stripped == ".endm":
            continue

        if stripped.startswith(".if "):
            if current_condition and current_expansion:
                variants.append(
                    {
                        "condition": current_condition,
                        "expansion": [
                            format_expansion_line(exp_line, params)
                            for exp_line in current_expansion
                        ],
                    }
                )
            condition = stripped[4:].strip()
            condition = re.sub(r"\\(\w+)", r"\1", condition)
            current_condition = condition
            current_expansion = list(base_expansion)
            in_conditional = True
        elif stripped.startswith(".elseif "):
            if current_condition and current_expansion:
                variants.append(
                    {
                        "condition": current_condition,
                        "expansion": [
                            format_expansion_line(exp_line, params)
                            for exp_line in current_expansion
                        ],
                    }
                )
            condition = stripped[8:].strip()
            condition = re.sub(r"\\(\w+)", r"\1", condition)
            current_condition = condition
            current_expansion = list(base_expansion)
            in_conditional = True
        elif stripped.startswith(".else"):
            if current_condition and current_expansion:
                variants.append(
                    {
                        "condition": current_condition,
                        "expansion": [
                            format_expansion_line(exp_line, params)
                            for exp_line in current_expansion
                        ],
                    }
                )
            current_condition = "else"
            current_expansion = list(base_expansion)
            in_conditional = True
        elif stripped.startswith(".endif"):
            if current_condition and current_expansion:
                variants.append(
                    {
                        "condition": current_condition,
                        "expansion": [
                            format_expansion_line(exp_line, params)
                            for exp_line in current_expansion
                        ],
                    }
                )
            current_condition = None
            current_expansion = []
            in_conditional = False
        elif not stripped.startswith("."):
            if stripped[0].isupper() or "\\" in stripped:
                if in_conditional and current_condition is not None:
                    current_expansion.append(stripped)
                else:
                    base_expansion.append(stripped)

    if not variants:
        return None

    has_else = any(variant["condition"] == "else" for variant in variants)
    if not has_else and base_expansion:
        variants.append(
            {
                "condition": "else",
                "expansion": [
                    format_expansion_line(exp_line, params)
                    for exp_line in base_expansion
                ],
            }
        )

    if len(variants) >= 2:
        return variants
    return None


def detect_wrapper_macro(body: str, all_macro_names: set[str]) -> MacroExpansion | None:
    """
    Detect if this macro just calls another macro (wrapper/convenience macro).

    Returns MacroExpansion with target and args, or None.
    """
    lines = [
        line.strip()
        for line in body.split("\n")
        if line.strip() and not line.strip().startswith(";")
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
                    args = split_macro_args(args_str)
                else:
                    args = []
                return MacroExpansion(target_macro=macro_name, args=args)

    return None


def parse_macro(
    name: str,
    params_str: str,
    body: str,
    all_macro_names: set[str],
    description: str | None = None,
    scrcmd_symbol_to_opcode: dict[str, int] | None = None,
) -> ParsedMacro | None:
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
            description=description,
            expansion=expansion,
            body=body.strip(),
        )

    # Check for primitive command name in comment (e.g., .short 624 /* SetHiddenLocation */)
    primitive_info = extract_primitive_from_comment(body, scrcmd_symbol_to_opcode)
    primitive_name = None
    primitive_params = []

    if primitive_info:
        opcode, comment_name = primitive_info
        # If the comment name differs from the macro name, this macro wraps a primitive
        if comment_name != name:
            primitive_name = comment_name
            primitive_params = extract_primitive_params(body, scrcmd_symbol_to_opcode)

    # Get first opcode (before any conditionals)
    first_opcode = extract_first_opcode(body, scrcmd_symbol_to_opcode)

    # Check for opcode-switching
    opcode_switches = detect_opcode_switching(body, scrcmd_symbol_to_opcode)

    if opcode_switches:
        # This macro can emit different opcodes
        opcodes = [op for _, op in opcode_switches]
        if first_opcode and first_opcode not in opcodes:
            opcodes.insert(0, first_opcode)
        return ParsedMacro(
            name=name,
            params=params,
            opcodes=opcodes,
            description=description,
            opcode_switches=opcode_switches,
            is_conditional=True,
            body=body.strip(),
            primitive_name=primitive_name,
            primitive_params=primitive_params,
        )

    # Check for conditional parameter emission
    variants = parse_conditionals(body, params)

    opcodes = (
        [first_opcode]
        if first_opcode is not None
        else extract_opcodes(body, scrcmd_symbol_to_opcode)[:1]
    )

    if not opcodes:
        # No numeric opcode found - check if it's a multi-line wrapper macro
        # that calls other macros (like ShowArrowSign, ShowMapSign, etc.)
        expansion_lines = parse_macro_expansion_lines(body)
        if expansion_lines:
            # This is a multi-line wrapper macro
            return ParsedMacro(
                name=name,
                params=params,
                opcodes=[],
                description=description,
                body=body.strip(),
            )
        return None

    emitted_params = extract_emitted_params(body)
    all_emitted = extract_all_emitted_values(body)

    return ParsedMacro(
        name=name,
        params=params,
        opcodes=opcodes,
        description=description,
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
    scrcmd_symbol_to_opcode: dict[str, int] | None = None,
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
    raw_macros = extract_macro_definitions(content)
    all_names = {macro.name for macro in raw_macros}

    parsed = {}
    primitives = {}  # primitive_name -> (opcode, params)

    for raw_macro in raw_macros:
        macro = parse_macro(
            raw_macro.name,
            raw_macro.params_str,
            raw_macro.body,
            all_names,
            description=raw_macro.description,
            scrcmd_symbol_to_opcode=scrcmd_symbol_to_opcode,
        )
        if macro:
            parsed[raw_macro.name] = macro

            # Extract ALL primitives from comments in this macro's body
            # This handles both simple wrappers and opcode-switching macros
            all_prims = extract_all_primitives_from_comments(
                raw_macro.body, scrcmd_symbol_to_opcode
            )
            for prim_opcode, prim_name in all_prims:
                # Only add if:
                # 1. The primitive name differs from the macro name
                # 2. The primitive doesn't already have its own macro
                if prim_name != raw_macro.name and prim_name not in all_names:
                    if prim_name not in primitives:
                        # Extract params for this primitive
                        prim_params = extract_primitive_params(
                            raw_macro.body, scrcmd_symbol_to_opcode
                        )
                        primitives[prim_name] = (prim_opcode, prim_params)

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


@lru_cache(maxsize=1)
def get_platinum_movement_action_constants() -> dict[str, int]:
    """
    Fetch and cache Platinum's movement action table.

    Returns dict of MOVEMENT_ACTION_* constant name -> numeric value.
    """
    content = fetch_url(PLATINUM_MOVEMENT_ACTIONS_URL)
    if not content:
        return {
            "MOVEMENT_ACTION_END": 254,
            "MOVEMENT_ACTION_NONE": 255,
        }

    constants = parse_movement_action_table(content)
    constants.setdefault("MOVEMENT_ACTION_END", 254)
    constants.setdefault("MOVEMENT_ACTION_NONE", 255)
    return constants


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


def split_macro_args(args_str: str) -> list[str]:
    """
    Split a macro argument string into normalized arguments.

    Rules:
    - Split on top-level commas first
    - Within each comma-separated segment, split on whitespace only when the
      segment is just multiple plain tokens (e.g. "CONST \\arg")
    - Keep arithmetic/comparison expressions together (e.g. "\\lower + 1")
    """
    comma_parts = []
    current = []
    paren_depth = 0

    for char in args_str:
        if char == "," and paren_depth == 0:
            part = "".join(current).strip()
            if part:
                comma_parts.append(part)
            current = []
            continue

        if char == "(":
            paren_depth += 1
        elif char == ")" and paren_depth > 0:
            paren_depth -= 1

        current.append(char)

    tail = "".join(current).strip()
    if tail:
        comma_parts.append(tail)

    args = []
    operator_tokens = {
        "+",
        "-",
        "*",
        "/",
        "%",
        "<<",
        ">>",
        "&",
        "|",
        "^",
        "&&",
        "||",
        "==",
        "!=",
        "<",
        ">",
        "<=",
        ">=",
    }

    for part in comma_parts:
        tokens = part.split()
        if len(tokens) <= 1:
            args.append(part)
            continue

        if any(token in operator_tokens for token in tokens):
            args.append(part)
            continue

        args.extend(tokens)

    return args


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
    parts = line.split(None, 1)
    cmd = parts[0]

    if len(parts) == 1:
        return cmd

    raw_args = split_macro_args(parts[1])

    # Clean each argument: replace \param with $param, strip whitespace
    clean_args = []
    for arg in raw_args:
        # Replace \param with $param for all params
        for p in params:
            arg = arg.replace(f"\\{p.name}", f"${p.name}")
        # Also handle any remaining backslash-prefixed identifiers
        arg = re.sub(r"\\([a-zA-Z_][a-zA-Z0-9_]*)", r"$\1", arg)
        arg = arg.strip()
        if arg:
            clean_args.append(arg)

    if not clean_args:
        return cmd

    return f"{cmd} {', '.join(clean_args)}"


VAR_RESULT_PARAM_NAMES = frozenset(
    {
        "destvar",
        "destvarid",
        "sucessvar",
        "successvar",
        "var_dest",
        "retvar",
        "resultvar",
        "checkdestvarid",
    }
)

VAR_RESULT_DEST_PARAM_NAMES = frozenset(
    {
        "destvar",
        "destvarid",
        "var_dest",
        "checkdestvarid",
    }
)

VAR_RESULT_RESULT_PARAM_NAMES = frozenset(
    {
        "sucessvar",
        "retvar",
        "resultvar",
        "result",
    }
)

VAR_RESULT_EXCLUDED_COMMANDS = frozenset(
    {
        "setvar",
        "setvarfromvalue",
        "setvarfromvar",
        "addvar",
        "addvarfromvalue",
        "addvarfromvar",
        "subvar",
        "subvarfromvalue",
        "subvarfromvar",
    }
)


def infer_param_default(
    name: str,
    command_name: str | None = None,
    emitted_param_names: list[str] | None = None,
    version: str | None = None,
) -> str | None:
    """Infer default value for a parameter based on its name.

    Args:
        name: The parameter name
        command_name: Optional command/macro name to exclude certain commands
        emitted_param_names: Optional full emitted parameter list for context-sensitive
            inference when both destination and result vars are present
        version: Game version string. When "HeartGold/SoulSilver", uses
            VAR_SPECIAL_RESULT instead of VAR_RESULT.
    """
    if not name:
        return None
    if command_name and command_name.lower() in VAR_RESULT_EXCLUDED_COMMANDS:
        return None

    name_lower = name.lower()
    if name_lower not in VAR_RESULT_PARAM_NAMES:
        return None

    emitted_names_lower = {
        emitted_name.lower()
        for emitted_name in (emitted_param_names or [])
        if emitted_name
    }
    has_dest_var = bool(emitted_names_lower & VAR_RESULT_DEST_PARAM_NAMES)
    has_result_var = bool(emitted_names_lower & VAR_RESULT_RESULT_PARAM_NAMES)

    if has_dest_var and has_result_var and name_lower in VAR_RESULT_DEST_PARAM_NAMES:
        return None

    return "VAR_SPECIAL_RESULT" if version == "HeartGold/SoulSilver" else "VAR_RESULT"


def infer_param_type(name: str) -> str:
    """Infer parameter type from name."""
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


def extract_macros_for_db(
    content: str,
    id_to_name: dict[int, str] | None = None,
    commands: dict | None = None,
    scrcmd_symbol_to_opcode: dict[str, int] | None = None,
) -> dict[str, dict]:
    """
    Extract convenience macros from decomp and format for v2 database.

    Returns dict of macro_name -> macro entry in v2 schema format.
    Handles standard macros and opcode-switching conditional macros.
    """
    parsed_macros, decomp_primitives = parse_scrcmd_inc(
        content, scrcmd_symbol_to_opcode=scrcmd_symbol_to_opcode
    )
    resolver = build_macro_reference_resolver(
        commands or {},
        id_to_name or {},
        parsed_macros,
        decomp_primitives,
    )
    macros = {}

    for name, macro in parsed_macros.items():
        if name in SKIP_MACROS:
            continue

        v2_params = []
        for p in macro.params:
            default = p.default or infer_param_default(p.name, name)
            v2_params.append(
                {
                    "name": p.name,
                    "type": infer_param_type(p.name),
                    **({"default": default} if default else {}),
                }
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

            entry = {"type": "macro", "params": v2_params, "variants": variants}
            if macro.description:
                entry["description"] = macro.description
            macros[name] = resolve_macro_entry_references(
                entry,
                resolver,
            )
            continue

        # 2. Handle .if/.else conditionals that call different macros
        if_else_variants = parse_if_else_macro_variants(macro.body, macro.params)
        if if_else_variants:
            entry = {
                "type": "macro",
                "params": v2_params,
                "variants": if_else_variants,
            }
            if macro.description:
                entry["description"] = macro.description
            macros[name] = resolve_macro_entry_references(
                entry,
                resolver,
            )
            continue

        # 3. Handle .ifnb optional params (e.g., TVBroadcastDummy)
        optional_params = detect_ifnb_optional_params(macro.body, macro.params)
        if optional_params:
            # Mark optional params in v2_params
            for i, p in enumerate(macro.params):
                if p.name in optional_params:
                    v2_params[i]["optional"] = True

            # Generate variants for different argument counts
            ifnb_variants = parse_ifnb_expansion_variants(macro.body, macro.params)
            if ifnb_variants:
                entry = {
                    "type": "macro",
                    "params": v2_params,
                    "variants": ifnb_variants,
                }
                if macro.description:
                    entry["description"] = macro.description
                macros[name] = resolve_macro_entry_references(
                    entry,
                    resolver,
                )
                continue

        # 4. Handle recursive macros with meaningful conditions (e.g. GoToIfInRange)
        if macro.name in macro.body and ".if " in macro.body:
            recursive_variants = parse_if_else_macro_variants(macro.body, macro.params)
            if recursive_variants:
                entry = {
                    "type": "macro",
                    "params": v2_params,
                    "variants": recursive_variants,
                }
                if macro.description:
                    entry["description"] = macro.description
                macros[name] = resolve_macro_entry_references(
                    entry,
                    resolver,
                )
                continue

        # 5. Handle Standard Macros (Expansion Lines)
        expansion_lines = parse_macro_expansion_lines(macro.body)

        # Check if this macro wraps a primitive (has .short OPCODE /* Name */ pattern or manual override)
        primitive_call = extract_primitive_call_line(
            macro.body,
            id_to_name,
            name,
            scrcmd_symbol_to_opcode=scrcmd_symbol_to_opcode,
        )

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
            entry = {
                "type": "macro",
                "params": v2_params,
                "expansion": v2_expansion,
            }
            if macro.description:
                entry["description"] = macro.description
            macros[name] = resolve_macro_entry_references(
                entry,
                resolver,
            )

    return macros


def inject_macros_into_db(db: dict, scrcmd_content: str) -> int:
    """Inject convenience macros from already-fetched scrcmd.inc content."""
    version = get_game_version(db)
    commands = db.setdefault("commands", {})
    id_to_name = build_id_to_name_map(commands, "script_cmd")
    scrcmd_symbol_to_opcode = (
        get_platinum_scrcmd_symbol_table() if version == "Platinum" else None
    )

    macros = extract_macros_for_db(
        scrcmd_content,
        id_to_name,
        commands=commands,
        scrcmd_symbol_to_opcode=scrcmd_symbol_to_opcode,
    )
    print(f"  Extracted {len(macros)} macros from decomp")

    added = updated = 0
    for name, macro_data in macros.items():
        action = upsert_imported_macro(commands, name, macro_data)
        if action == "added":
            added += 1
        elif action == "updated":
            updated += 1

    print(f"  Added {added} new macros, updated {updated} existing")
    return added + updated


def get_game_version(db: dict) -> str:
    """Extract game version from database metadata."""
    return db.get("meta", {}).get("version", "Unknown")


def validate_unique_command_ids(db: dict) -> None:
    """Reject databases that contain duplicate non-macro command IDs."""
    seen: dict[tuple[str, int], str] = {}

    for name, entry in db.get("commands", {}).items():
        cmd_type = entry.get("type")
        opcode = entry.get("id")

        if cmd_type == "macro" or opcode is None:
            continue

        key = (cmd_type, opcode)
        if key in seen:
            other = seen[key]
            raise ValueError(
                f"Duplicate command ID detected for {cmd_type} 0x{opcode:04X}: "
                f"{other} and {name}"
            )

        seen[key] = name


def is_generated_description(description: str | None) -> bool:
    """Check whether a description is an importer placeholder."""
    if not description:
        return True
    return description.startswith("Imported from decomp:")


def is_placeholder_command_name(name: str | None) -> bool:
    """Check whether a command key is still a placeholder-style identifier."""
    if not name:
        return False

    return bool(
        re.fullmatch(r"CMD_\d+", name)
        or re.fullmatch(r"ScrCmd_[0-9A-Fa-f]+", name)
        or re.fullmatch(r"ScrCmd_Unused_[0-9A-Fa-f]+", name)
    )


def write_db_if_changed(db_path: str | Path, db: dict) -> bool:
    """Write a database file only when its semantic JSON content changed."""
    validate_unique_command_ids(db)

    db_path = Path(db_path)
    if db_path.exists():
        with open(db_path, "r", encoding="utf-8") as f:
            current = json.load(f)
        if json.dumps(current, indent=2) == json.dumps(db, indent=2):
            return False

    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
    return True


def build_id_to_name_map(commands: dict, cmd_type: str) -> dict[int, str]:
    """Build opcode -> name mapping for commands of a given type."""
    id_map = {}
    for name, data in commands.items():
        if data.get("type") == cmd_type:
            id_map[data["id"]] = name
    return id_map


def build_name_to_id_map(commands: dict, cmd_type: str) -> dict[str, int]:
    """Build name -> opcode mapping for commands of a given type."""
    return {
        name: data["id"]
        for name, data in commands.items()
        if data.get("type") == cmd_type
    }


def resolve_command_name_from_maps(
    id_to_name: dict[int, str],
    name_to_id: dict[str, int],
    opcode: int | None,
    *candidate_names: str | None,
) -> tuple[str | None, str | None]:
    """Resolve a primitive command by opcode first, then by same-type name."""
    if opcode is not None and opcode in id_to_name:
        return id_to_name[opcode], "id"

    for candidate_name in candidate_names:
        if candidate_name and candidate_name in name_to_id:
            return candidate_name, "name"

    return None, None


def resolve_command_name_for_sync(
    commands: dict, cmd_type: str, opcode: int | None, *candidate_names: str | None
) -> tuple[str | None, str | None]:
    """Resolve the current DB key for a primitive sync item."""
    return resolve_command_name_from_maps(
        build_id_to_name_map(commands, cmd_type),
        build_name_to_id_map(commands, cmd_type),
        opcode,
        *candidate_names,
    )


def build_canonical_name_by_opcode(
    decomp_macros: dict[str, ParsedMacro],
    decomp_primitives: dict[str, tuple[int, list[dict]]] | None = None,
) -> dict[int, str]:
    """Build the preferred decomp command name for each opcode."""
    canonical: dict[int, str] = {}

    for name, macro in decomp_macros.items():
        if macro.is_wrapper or len(macro.opcodes) != 1:
            continue
        canonical.setdefault(macro.opcodes[0], name)

    if decomp_primitives:
        for name, (opcode, _params) in decomp_primitives.items():
            canonical.setdefault(opcode, name)

    return canonical


def repair_duplicate_command_ids(
    db: dict, cmd_type: str, canonical_name_by_opcode: dict[int, str]
) -> int:
    """
    Remove duplicate command entries that share an opcode with the canonical decomp name.

    Returns the number of stale duplicate entries removed.
    """
    commands = db.get("commands", {})
    names_by_opcode: dict[int, list[str]] = {}

    for name, entry in commands.items():
        if entry.get("type") != cmd_type or entry.get("id") is None:
            continue
        names_by_opcode.setdefault(entry["id"], []).append(name)

    removed = 0
    for opcode, names in names_by_opcode.items():
        if len(names) < 2:
            continue

        canonical_name = canonical_name_by_opcode.get(opcode)
        if canonical_name not in names:
            raise ValueError(
                f"Duplicate {cmd_type} opcode 0x{opcode:04X} with no canonical "
                f"decomp match: {', '.join(sorted(names))}"
            )

        canonical_entry = commands[canonical_name]
        stale_names = [name for name in names if name != canonical_name]

        for stale_name in stale_names:
            stale_entry = commands.pop(stale_name)
            if "legacy_name" not in canonical_entry:
                canonical_entry["legacy_name"] = stale_entry.get(
                    "legacy_name", stale_name
                )
            if is_generated_description(
                canonical_entry.get("description")
            ) and not is_generated_description(stale_entry.get("description")):
                canonical_entry["description"] = stale_entry["description"]
            if "notes" not in canonical_entry and stale_entry.get("notes"):
                canonical_entry["notes"] = stale_entry["notes"]
            removed += 1

        commands[canonical_name] = canonical_entry

    db["commands"] = commands
    return removed


def build_temp_command_name(commands: dict, name: str) -> str:
    """Build a temporary key for parking a conflicting command entry."""
    conflict_entry = commands[name]
    temp_name = (
        f"__tmp__{conflict_entry.get('type', 'command')}__"
        f"{name}__{conflict_entry.get('id', 'noid')}"
    )

    suffix = 1
    while temp_name in commands:
        temp_name = (
            f"__tmp__{conflict_entry.get('type', 'command')}__"
            f"{name}__{conflict_entry.get('id', 'noid')}__{suffix}"
        )
        suffix += 1

    return temp_name


def backfill_dp_placeholder_names_from_platinum(dp_db: dict, platinum_db: dict) -> int:
    """
    Backfill Diamond/Pearl placeholder script command names from Platinum by opcode.

    Only renames DP commands whose current key is still a placeholder and where
    Platinum has a non-placeholder script command name for the same opcode.
    """
    commands = dp_db.get("commands", {})
    platinum_commands = platinum_db.get("commands", {})

    platinum_name_by_opcode: dict[int, str] = {}
    for platinum_name, platinum_entry in platinum_commands.items():
        if platinum_entry.get("type") != "script_cmd":
            continue
        opcode = platinum_entry.get("id")
        if opcode is None or is_placeholder_command_name(platinum_name):
            continue
        platinum_name_by_opcode.setdefault(opcode, platinum_name)

    renamed = 0
    for current_name, entry in list(commands.items()):
        if entry.get("type") != "script_cmd":
            continue
        if not is_placeholder_command_name(current_name):
            continue

        opcode = entry.get("id")
        target_name = platinum_name_by_opcode.get(opcode)
        if not target_name or target_name == current_name:
            continue

        commands = rename_command_key_preserving_order(
            commands, current_name, target_name
        )
        renamed += 1

    dp_db["commands"] = commands
    return renamed


def displace_command_key_preserving_order(
    commands: dict, conflict_name: str
) -> tuple[dict, str]:
    """Move an existing key aside without disturbing the surrounding order."""
    temp_name = build_temp_command_name(commands, conflict_name)

    displaced: dict = {}
    for name, data in commands.items():
        if name == conflict_name:
            displaced[temp_name] = data
        else:
            displaced[name] = data

    return displaced, temp_name


def rename_command_key_preserving_order(
    commands: dict, old_name: str, new_name: str
) -> dict:
    """Rename a command key while preserving the surrounding object order."""
    if new_name in commands and new_name != old_name:
        commands, _temp_name = displace_command_key_preserving_order(commands, new_name)

    renamed: dict = {}
    for name, data in commands.items():
        if name == old_name:
            renamed[new_name] = data
        else:
            renamed[name] = data
    return renamed


def insert_command_key_preserving_order(commands: dict, name: str, data: dict) -> dict:
    """Insert a new key without overwriting an existing entry of another type."""
    if name in commands:
        commands, _temp_name = displace_command_key_preserving_order(commands, name)

    inserted = dict(commands)
    inserted[name] = data
    return inserted


def normalize_symbol_name(name: str) -> str:
    """Normalize a command/macro identifier for loose alias matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def build_legacy_name_map(commands: dict) -> dict[str, str]:
    """Build a reverse lookup of legacy_name -> current database key."""
    legacy_map: dict[str, str] = {}
    ambiguous = set()

    for name, data in commands.items():
        legacy_name = data.get("legacy_name")
        if not legacy_name:
            continue
        if legacy_name in legacy_map and legacy_map[legacy_name] != name:
            ambiguous.add(legacy_name)
            continue
        legacy_map[legacy_name] = name

    for legacy_name in ambiguous:
        legacy_map.pop(legacy_name, None)

    return legacy_map


def build_normalized_name_map(commands: dict) -> dict[str, str]:
    """Build a collision-safe normalized-name -> current database key map."""
    normalized_map: dict[str, str] = {}
    ambiguous = set()

    for name in commands:
        normalized = normalize_symbol_name(name)
        if not normalized:
            continue
        if normalized in normalized_map and normalized_map[normalized] != name:
            ambiguous.add(normalized)
            continue
        normalized_map[normalized] = name

    for normalized in ambiguous:
        normalized_map.pop(normalized, None)

    return normalized_map


def build_decomp_opcode_name_map(
    decomp_macros: dict[str, ParsedMacro],
    decomp_primitives: dict[str, tuple[int, list[dict]]] | None = None,
) -> dict[str, int]:
    """Build a name -> opcode map for decomp-defined primitives/commands."""
    name_to_opcode: dict[str, int] = {}

    for name, macro in decomp_macros.items():
        if len(macro.opcodes) == 1:
            name_to_opcode[name] = macro.opcodes[0]

    if decomp_primitives:
        for name, (opcode, _params) in decomp_primitives.items():
            name_to_opcode.setdefault(name, opcode)

    return name_to_opcode


def build_macro_reference_resolver(
    commands: dict,
    id_to_name: dict[int, str],
    decomp_macros: dict[str, ParsedMacro],
    decomp_primitives: dict[str, tuple[int, list[dict]]] | None = None,
):
    """Resolve decomp macro call targets to the actual keys used in this database."""
    legacy_name_map = build_legacy_name_map(commands)
    normalized_name_map = build_normalized_name_map(commands)
    decomp_name_to_opcode = build_decomp_opcode_name_map(
        decomp_macros, decomp_primitives
    )

    def resolve(name: str) -> str:
        if name in commands:
            return name

        if name in legacy_name_map:
            return legacy_name_map[name]

        opcode = decomp_name_to_opcode.get(name)
        if opcode is not None and opcode in id_to_name:
            return id_to_name[opcode]

        normalized = normalize_symbol_name(name)
        if normalized in normalized_name_map:
            return normalized_name_map[normalized]

        return name

    return resolve


def resolve_expansion_line_reference(line: str, resolver) -> str:
    """Resolve the command/macro identifier at the start of an expansion line."""
    if not line or not line.strip():
        return line

    parts = line.split(None, 1)
    target = resolver(parts[0])
    if len(parts) == 1:
        return target
    return f"{target} {parts[1]}"


def resolve_macro_entry_references(entry: dict, resolver) -> dict:
    """Apply expansion-line reference resolution across a macro entry."""
    resolved = json.loads(json.dumps(entry))

    if "expansion" in resolved:
        resolved["expansion"] = [
            resolve_expansion_line_reference(line, resolver)
            for line in resolved["expansion"]
        ]

    if "variants" in resolved:
        for variant in resolved["variants"]:
            variant["expansion"] = [
                resolve_expansion_line_reference(line, resolver)
                for line in variant.get("expansion", [])
            ]

    return resolved


def get_macro_semantic_view(entry: dict) -> dict:
    """Return the importer-owned fields used to compare macro semantics."""
    view = {
        "type": "macro",
        "params": entry.get("params", []),
    }

    if "expansion" in entry:
        view["expansion"] = entry["expansion"]
    if "variants" in entry:
        view["variants"] = entry["variants"]
    if "description" in entry:
        view["description"] = entry["description"]

    return view


def merge_imported_macro_entry(existing: dict, imported: dict) -> dict:
    """Merge decomp-managed macro fields while preserving local metadata."""
    merged = dict(existing)
    merged["type"] = "macro"
    merged["params"] = imported.get("params", [])
    if "description" in imported:
        merged["description"] = imported["description"]

    if "expansion" in imported:
        merged["expansion"] = imported["expansion"]
        merged.pop("variants", None)
    if "variants" in imported:
        merged["variants"] = imported["variants"]
        merged.pop("expansion", None)

    return merged


def find_equivalent_macro_name(
    commands: dict, imported_name: str, imported_entry: dict
) -> str | None:
    """Find an existing macro with the same semantics under a different name."""
    imported_view = get_macro_semantic_view(imported_entry)

    for name, entry in commands.items():
        if name == imported_name or entry.get("type") != "macro":
            continue
        if get_macro_semantic_view(entry) == imported_view:
            return name

    return None


def upsert_imported_macro(commands: dict, name: str, imported_entry: dict) -> str:
    """
    Insert/update one imported macro.
    Renames existing semantically-equivalent aliases to the canonical decomp name.

    Returns one of: `added`, `updated`, `skipped`.
    """
    if name in commands:
        if commands[name].get("type") != "macro":
            return "skipped"

        merged = merge_imported_macro_entry(commands[name], imported_entry)
        if merged != commands[name]:
            commands[name] = merged
            return "updated"
        return "skipped"

    alias_name = find_equivalent_macro_name(commands, name, imported_entry)
    if alias_name:
        merged = merge_imported_macro_entry(commands[alias_name], imported_entry)
        if "legacy_name" not in merged:
            merged["legacy_name"] = alias_name
        commands.pop(alias_name)
        commands[name] = merged
        return "updated"

    commands[name] = imported_entry
    return "added"


def apply_param_default_removals(db: dict, version: str) -> int:
    """
    Remove parameter defaults that conflict with actual decomp script usage.

    Returns the number of parameters whose default was removed.
    """
    removals = DECOMP_PARAM_DEFAULT_REMOVALS.get(version, {})
    if not removals:
        return 0

    commands = db.get("commands", {})
    count = 0
    for cmd_name, param_names in removals.items():
        cmd = commands.get(cmd_name)
        if not cmd:
            continue
        for param in cmd.get("params", []):
            if param.get("name") in param_names and "default" in param:
                del param["default"]
                count += 1
    return count


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
    db_name_to_id = build_name_to_id_map(db_commands, cmd_type)

    missing = []
    mismatched = []
    wrappers = []
    param_updates = []  # Commands that exist but need param updates

    # First, check hidden primitives from comments (like SetHiddenLocation, CheckHasEnoughMonForCatchingShow)
    if decomp_primitives:
        for prim_name, (prim_opcode, prim_params) in decomp_primitives.items():
            if prim_opcode in db_id_to_name:
                db_name = db_id_to_name[prim_opcode]
                if db_name != prim_name:
                    mismatched.append(
                        {
                            "name": prim_name,
                            "decomp_opcode": prim_opcode,
                            "db_name": db_name,
                            "is_conditional": False,
                            "all_opcodes": [prim_opcode],
                            "params": prim_params,
                            "is_hidden_primitive": True,
                            "description": None,
                        }
                    )
                else:
                    db_cmd = db_commands.get(prim_name, {})
                    db_params = db_cmd.get("params", [])
                    if len(prim_params) > len(db_params):
                        param_updates.append(
                            {
                                "name": prim_name,
                                "opcode": prim_opcode,
                                "params": prim_params,
                                "is_hidden_primitive": True,
                            }
                        )
            elif prim_name in db_name_to_id:
                mismatched.append(
                    {
                        "name": prim_name,
                        "decomp_opcode": prim_opcode,
                        "db_opcode": db_name_to_id[prim_name],
                        "is_conditional": False,
                        "all_opcodes": [prim_opcode],
                        "params": prim_params,
                        "is_hidden_primitive": True,
                        "description": None,
                    }
                )
            else:
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
                        "description": None,
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

            continue

        # Check if macro is an opcode switcher (multiple opcodes)
        if len(macro.opcodes) > 1:
            continue

        if not macro.opcodes:
            continue

        primary_opcode = macro.opcodes[0]

        if primary_opcode in db_id_to_name:
            db_name = db_id_to_name[primary_opcode]

            # Skip if this macro wraps a hidden primitive - the primitive is the real command
            if db_name == name:
                continue

            if macro.wraps_primitive:
                continue

            # Check if existing DB name is also a valid decomp name (alias)
            # If it resolves to the same opcode, don't rename it.
            if db_name in decomp_macros:
                db_name_macro = decomp_macros[db_name]
                if (
                    not db_name_macro.is_wrapper
                    and primary_opcode in db_name_macro.opcodes
                ):
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
                    "description": macro.description,
                }
            )
        elif name in db_name_to_id:
            mismatched.append(
                {
                    "name": name,
                    "decomp_opcode": primary_opcode,
                    "db_opcode": db_name_to_id[name],
                    "is_conditional": macro.is_conditional,
                    "all_opcodes": macro.opcodes,
                    "params": [p.name for p in macro.params],
                    "description": macro.description,
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
                    "description": macro.description,
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


def is_generic_param_name(name: str) -> bool:
    """Check if a parameter name is generic and should be updated from decomp."""
    if not name:
        return True
    name_lower = name.lower()
    if name.startswith("???"):
        return True
    generic_exact = {"variable", "value", "arg", "flag", "param", "offset", "data"}
    if name_lower in generic_exact:
        return True
    if re.match(r"^(arg|param|var)(?:_?\d+)?$", name_lower):
        return True
    return False


def is_standard_var_pattern(name: str) -> bool:
    """Check if a parameter name follows standard variable naming patterns from decomp."""
    if not name:
        return False
    name_lower = name.lower()
    var_patterns = (
        "varid",
        "var_",
        "destvar",
        "srcvar",
        "retvar",
        "flagid",
        "flag_",
        "msgid",
        "scriptid",
        "eventid",
    )
    return any(pattern in name_lower for pattern in var_patterns)


def update_param_names(db_params: list, macro: ParsedMacro) -> bool:
    """
    Update database parameter names based on decomp macro parameter names.

    Decomp is the source of truth for parameter names. Update when:
    - DB name is generic (variable, value, arg, etc.)
    - Decomp name follows standard naming patterns (destVarID, flagID, etc.)

    This handles cases where the DB has:
    - A generic name like "variable" that should be "destVarID"
    - A wrong specific name like "pokémon" that should be "destVarID"

    Returns True if changes were made.
    """
    if not macro.emitted_params or not db_params:
        return False

    if len(macro.emitted_params) != len(db_params):
        return False

    changes = False

    for i, emitted_name in enumerate(macro.emitted_params):
        if not emitted_name:
            continue
        if is_generic_param_name(emitted_name):
            continue

        current_name = db_params[i].get("name", "")

        should_update = is_generic_param_name(current_name) or is_standard_var_pattern(
            emitted_name
        )

        if should_update and current_name != emitted_name:
            db_params[i]["name"] = emitted_name
            changes = True

    return changes


def update_inferred_param_defaults(
    db_params: list, macro: ParsedMacro, command_name: str, version: str | None = None
) -> bool:
    """
    Update database parameters with inferred default values (e.g., VAR_RESULT for destVar).

    This also removes stale inferred defaults, but only for duplicate default pairs:
    when a command has both a destination var and a separate result var, and both
    currently default to the same value, only the result-style param should keep it.
    Returns True if changes were made.
    """
    if not macro.emitted_params or not db_params:
        return False

    if len(macro.emitted_params) != len(db_params):
        return False

    changes = False

    emitted_names_lower = [
        emitted_name.lower() if emitted_name else ""
        for emitted_name in macro.emitted_params
    ]
    has_dest_var = any(
        name in VAR_RESULT_DEST_PARAM_NAMES for name in emitted_names_lower
    )
    has_result_var = any(
        name in VAR_RESULT_RESULT_PARAM_NAMES for name in emitted_names_lower
    )

    duplicate_default_pairs = set()
    if has_dest_var and has_result_var:
        defaults_to_indexes: dict[str, list[int]] = {}
        for i, default_value in enumerate(p.get("default") for p in db_params):
            if default_value is None:
                continue
            defaults_to_indexes.setdefault(default_value, []).append(i)

        duplicate_default_pairs = {
            i
            for indexes in defaults_to_indexes.values()
            if len(indexes) >= 2
            for i in indexes
        }

    for i, emitted_name in enumerate(macro.emitted_params):
        if not emitted_name:
            continue

        inferred_default = infer_param_default(
            emitted_name, command_name, macro.emitted_params, version
        )
        current_default = db_params[i].get("default")

        if inferred_default is None:
            if (
                i in duplicate_default_pairs
                and emitted_names_lower[i] in VAR_RESULT_DEST_PARAM_NAMES
            ):
                db_params[i].pop("default", None)
                changes = True
            continue

        if current_default != inferred_default:
            db_params[i]["default"] = inferred_default
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

        # Preserve manually-set "flex" type
        if current_type == "flex":
            continue

        # Only update if types differ
        if current_type != actual_type:
            db_params[i]["type"] = actual_type
            changes = True

    return changes


def update_description_from_decomp(command: dict, macro: ParsedMacro) -> bool:
    """Update a command description when the decomp provides one."""
    if not macro.description:
        return False

    if command.get("description") == macro.description:
        return False

    command["description"] = macro.description
    return True


def sync_custom_call_shape_variants(
    db: dict, decomp_macros: dict[str, ParsedMacro]
) -> int:
    """
    Add custom call-shape variants for simple primitive macros with packed args.

    Existing variants are preserved to avoid overwriting manual schema work.
    """
    commands = db.get("commands", {})
    id_to_name = build_id_to_name_map(commands, "script_cmd")
    name_to_id = build_name_to_id_map(commands, "script_cmd")
    updated = 0

    for name, macro in decomp_macros.items():
        variants = build_custom_call_shape_variants(macro)
        if not variants:
            continue

        target_name, _match_mode = resolve_command_name_from_maps(
            id_to_name, name_to_id, macro.opcodes[0], name
        )
        if not target_name or target_name not in commands:
            continue

        command = commands[target_name]
        existing_variants = command.get("variants")

        if existing_variants == variants:
            continue
        if existing_variants:
            continue

        command["variants"] = variants
        updated += 1

    return updated


def build_sync_param_list(
    raw_params: list, existing_params: list[dict] | None = None
) -> list[dict]:
    """Convert sync item params into the v2 parameter representation."""
    params = []

    for i, raw_param in enumerate(raw_params):
        if isinstance(raw_param, dict):
            param_name = raw_param.get("name", f"arg_{i}")
            param_type = raw_param.get("type", "u16")
        else:
            param_name = (
                raw_param.name if hasattr(raw_param, "name") else str(raw_param)
            )
            param_type = infer_param_type(param_name)

        if (
            existing_params
            and i < len(existing_params)
            and existing_params[i].get("type") == "flex"
        ):
            param_type = "flex"

        params.append({"name": param_name, "type": param_type})

    return params


def update_db_from_sync(
    db: dict, missing: list, mismatched: list, cmd_type: str
) -> int:
    """
    Update database based on sync results.
    Returns number of changes made.
    """
    commands = db.get("commands", {})
    changes = 0

    # Handle renames before opcode corrections so split commands like
    # PlaySE/PlayFanfare converge in one pass instead of two.
    ordered_mismatches = [
        *[item for item in mismatched if "db_name" in item],
        *[item for item in mismatched if "db_opcode" in item],
    ]

    for item in ordered_mismatches:
        decomp_name = item["name"]
        decomp_opcode = item["decomp_opcode"]

        current_name, match_mode = resolve_command_name_for_sync(
            commands,
            cmd_type,
            decomp_opcode,
            decomp_name,
            item.get("db_name"),
        )

        if current_name is None:
            entry = {
                "type": cmd_type,
                "id": decomp_opcode,
                "description": item.get("description") or "",
                "params": build_sync_param_list(item.get("params", [])),
            }
            commands = insert_command_key_preserving_order(commands, decomp_name, entry)
            changes += 1
            print(
                f"    Re-added {decomp_name} (0x{decomp_opcode:04X}) after rename collision"
            )
            continue

        old_name = current_name
        data = commands[current_name]
        changed = False

        if current_name != decomp_name:
            commands = rename_command_key_preserving_order(
                commands, current_name, decomp_name
            )
            data = commands[decomp_name]
            if "legacy_name" not in data:
                data["legacy_name"] = old_name
            changed = True
            print(f"    Renamed {old_name} -> {decomp_name}")

        if data.get("id") != decomp_opcode:
            previous_opcode = data.get("id")
            data["id"] = decomp_opcode
            changed = True
            print(
                f"    Updated opcode for {decomp_name}: {previous_opcode} -> {decomp_opcode}"
            )

        if item.get("description") and data.get("description") != item["description"]:
            data["description"] = item["description"]
            changed = True

        if item.get("is_hidden_primitive") and item.get("params"):
            new_params = build_sync_param_list(item["params"], data.get("params", []))
            if data.get("params") != new_params:
                data["params"] = new_params
                changed = True

        if changed:
            changes += 1

    # Handle missing (New Commands)
    for item in missing:
        name = item["name"]

        opcode = item["opcode"]
        current_name, _match_mode = resolve_command_name_for_sync(
            commands, cmd_type, opcode, name
        )

        if current_name is not None:
            data = commands[current_name]
            changed = False

            if current_name != name:
                commands = rename_command_key_preserving_order(
                    commands, current_name, name
                )
                data = commands[name]
                if "legacy_name" not in data:
                    data["legacy_name"] = current_name
                changed = True
                print(f"    Renamed {current_name} -> {name}")

            if data.get("id") != opcode:
                data["id"] = opcode
                changed = True

            if (
                item.get("description")
                and data.get("description") != item["description"]
            ):
                data["description"] = item["description"]
                changed = True

            if item.get("params"):
                new_params = build_sync_param_list(
                    item["params"], data.get("params", [])
                )
                if data.get("params") != new_params:
                    data["params"] = new_params
                    changed = True

            if changed:
                changes += 1
            continue

        entry = {
            "type": cmd_type,
            "id": opcode,
            "description": item.get("description") or "",
            "params": build_sync_param_list(item.get("params", [])),
        }

        if item.get("is_conditional"):
            # TODO: Better handling of conditional variants from sync
            pass

        commands = insert_command_key_preserving_order(commands, name, entry)
        changes += 1
        print(f"    Added new command: {name} (0x{opcode:04X})")

    db["commands"] = commands
    return changes


def _is_simple_sync_macro(macro: ParsedMacro) -> bool:
    return not macro.is_wrapper and not macro.is_conditional and len(macro.opcodes) == 1


def _iter_simple_script_cmd_targets(
    decomp_macros: dict[str, ParsedMacro],
    db_commands: dict,
    id_to_name: dict[int, str],
    name_to_id: dict[str, int],
    *,
    require_description: bool = False,
    require_emitted_values: bool = False,
):
    for name, macro in decomp_macros.items():
        if not _is_simple_sync_macro(macro):
            continue
        if require_description and not macro.description:
            continue
        if require_emitted_values and not macro.all_emitted_values:
            continue

        target_name, _ = resolve_command_name_from_maps(
            id_to_name, name_to_id, macro.opcodes[0], name
        )
        if target_name and target_name in db_commands:
            yield target_name, macro, db_commands[target_name]


def _update_hardcoded_param_defaults(
    db_commands: dict,
    decomp_macros: dict[str, ParsedMacro],
    id_to_name: dict[int, str],
    name_to_id: dict[str, int],
) -> int:
    updated = 0

    for name, macro in decomp_macros.items():
        if not _is_simple_sync_macro(macro) or not macro.all_emitted_values:
            continue

        has_hardcoded = any(v.get("is_literal") for v in macro.all_emitted_values)
        emitted_names = [
            v.get("name") for v in macro.all_emitted_values if not v.get("is_literal")
        ]
        if not has_hardcoded and len(emitted_names) == len(set(emitted_names)):
            continue

        target_name, _ = resolve_command_name_from_maps(
            id_to_name, name_to_id, macro.opcodes[0], name
        )
        if not target_name or target_name not in db_commands:
            continue

        db_params = db_commands[target_name].get("params", [])
        decomp_params = macro.all_emitted_values
        if len(decomp_params) != len(db_params):
            continue

        seen_param_names: dict[str, int] = {}
        changed = False
        for i, decomp_p in enumerate(decomp_params):
            decomp_name = decomp_p.get("name", "")

            if (
                decomp_name
                and not decomp_p.get("is_literal")
                and decomp_name in seen_param_names
            ):
                first_idx = seen_param_names[decomp_name]
                first_param_name = db_params[first_idx].get("name", decomp_name)
                db_params[i]["default"] = f"${first_param_name}"
                changed = True
            elif decomp_name and not decomp_p.get("is_literal"):
                seen_param_names[decomp_name] = i

            if decomp_p.get("is_literal") and decomp_p.get("default") is not None:
                if db_params[i].get("default") != decomp_p["default"]:
                    db_params[i]["default"] = decomp_p["default"]
                    changed = True

        if changed:
            updated += 1

    return updated


def _sync_simple_script_cmd_metadata(
    db: dict, decomp_macros: dict[str, ParsedMacro], version: str | None = None
) -> None:
    db_commands = db.get("commands", {})
    id_to_name = build_id_to_name_map(db_commands, "script_cmd")
    name_to_id = build_name_to_id_map(db_commands, "script_cmd")

    for label, require_description, updater in (
        (
            "defaults",
            False,
            lambda cmd, macro, _: update_param_defaults(cmd.get("params", []), macro),
        ),
        (
            "types",
            False,
            lambda cmd, macro, _: update_param_types(cmd.get("params", []), macro),
        ),
        (
            "names",
            False,
            lambda cmd, macro, _: update_param_names(cmd.get("params", []), macro),
        ),
        (
            "descriptions",
            True,
            lambda cmd, macro, _: update_description_from_decomp(cmd, macro),
        ),
        (
            "inferred defaults",
            False,
            lambda cmd, macro, target: update_inferred_param_defaults(
                cmd.get("params", []), macro, target, version
            ),
        ),
    ):
        count = sum(
            1
            for target, macro, cmd in _iter_simple_script_cmd_targets(
                decomp_macros,
                db_commands,
                id_to_name,
                name_to_id,
                require_description=require_description,
            )
            if updater(cmd, macro, target)
        )
        if count:
            print(f"  Updated {label} for {count} commands")

    hardcoded = _update_hardcoded_param_defaults(
        db_commands, decomp_macros, id_to_name, name_to_id
    )
    if hardcoded:
        print(f"  Updated {hardcoded} commands with hardcoded param defaults")

    shapes = sync_custom_call_shape_variants(db, decomp_macros)
    if shapes:
        print(f"  Added custom call shapes for {shapes} commands")


def sync_database(
    db: dict,
    sources: dict[str, str],
    version: str,
    scrcmd_content: str | None = None,
) -> None:
    """Apply decomp-derived updates to a v2 database in memory."""

    if "scrcmd" in sources:
        content = scrcmd_content
        if content is None:
            print("  Fetching scrcmd.inc...")
            content = fetch_url(sources["scrcmd"])

        if content:
            scrcmd_symbol_to_opcode = (
                get_platinum_scrcmd_symbol_table() if version == "Platinum" else None
            )

            decomp_macros, decomp_primitives = parse_scrcmd_inc(
                content, scrcmd_symbol_to_opcode=scrcmd_symbol_to_opcode
            )

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

            duplicates_removed = repair_duplicate_command_ids(
                db,
                "script_cmd",
                build_canonical_name_by_opcode(decomp_macros, decomp_primitives),
            )
            if duplicates_removed > 0:
                print(
                    f"  Removed {duplicates_removed} duplicate script command "
                    f"entr{'y' if duplicates_removed == 1 else 'ies'} by opcode"
                )

            missing, _extra, mismatched, _wrappers, param_updates = (
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
                if len(missing) > 10:
                    print(f"    ... and {len(missing) - 10} more")

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

            if missing or mismatched:
                print("  Applying updates...")
                count = update_db_from_sync(db, missing, mismatched, "script_cmd")
                if count > 0:
                    print(f"  Applied {count} changes to commands")

            if param_updates:
                db_commands = db.get("commands", {})
                for item in param_updates:
                    target_name, _ = resolve_command_name_for_sync(
                        db_commands, "script_cmd", item.get("opcode"), item["name"]
                    )
                    if target_name:
                        new_params = build_sync_param_list(
                            item["params"], db_commands[target_name].get("params", [])
                        )
                        db_commands[target_name]["params"] = new_params
                        print(
                            f"    Updated params for {target_name}: {len(new_params)} params"
                        )

            _sync_simple_script_cmd_metadata(db, decomp_macros, version)

            removed = apply_param_default_removals(db, version)
            if removed:
                print(
                    f"  Removed {removed} param default(s) via game-specific overrides"
                )

    if "movement" in sources:
        print("  Fetching movement.inc...")
        content = fetch_url(sources["movement"])
        if content:
            movement_constants = (
                get_platinum_movement_action_constants()
                if version == "Platinum"
                else None
            )
            decomp_moves = parse_movement_inc(content, movement_constants)
            print(f"  Parsed {len(decomp_moves)} movements from decomp")

            db_commands = db.get("commands", {})
            db_id_to_name = build_id_to_name_map(db_commands, "movement")
            db_name_to_id = build_name_to_id_map(db_commands, "movement")

            missing = []
            mismatched = []

            for name, (opcode, move_params) in decomp_moves.items():
                if opcode is not None and opcode in db_id_to_name:
                    db_name = db_id_to_name[opcode]
                    if db_name == name:
                        continue
                    if db_name in decomp_moves and decomp_moves[db_name][0] == opcode:
                        continue
                    mismatched.append(
                        {
                            "name": name,
                            "decomp_opcode": opcode,
                            "db_name": db_name,
                            "params": move_params,
                        }
                    )
                elif opcode is not None and name in db_name_to_id:
                    mismatched.append(
                        {
                            "name": name,
                            "decomp_opcode": opcode,
                            "db_opcode": db_name_to_id[name],
                            "params": move_params,
                        }
                    )
                elif opcode is not None:
                    missing.append(
                        {"name": name, "opcode": opcode, "params": move_params}
                    )

            if missing:
                print(f"  Missing movements in DB: {len(missing)}")
                for item in missing[:5]:
                    opcode = item["opcode"]
                    print(f"    - {item['name']} (0x{opcode:02X})")
                if len(missing) > 5:
                    print(f"    ... and {len(missing) - 5} more")

            if mismatched:
                print(f"  Movement mismatches: {len(mismatched)}")

            if missing or mismatched:
                print("  Applying movement updates...")
                count = update_db_from_sync(db, missing, mismatched, "movement")
                if count > 0:
                    print(f"  Applied {count} changes to movements")

            params_updated = 0
            db_commands = db.get("commands", {})
            db_name_to_id = build_name_to_id_map(db_commands, "movement")
            db_id_to_name = build_id_to_name_map(db_commands, "movement")

            for name, (opcode, move_params) in decomp_moves.items():
                target_name, _ = resolve_command_name_from_maps(
                    db_id_to_name, db_name_to_id, opcode, name
                )
                if not target_name or target_name not in db_commands:
                    continue

                decomp_param_list = [
                    {
                        "name": p.name,
                        "type": infer_param_type(p.name),
                        **({"default": p.default} if p.default else {}),
                    }
                    for p in move_params
                ]
                cmd = db_commands[target_name]
                if cmd.get("params", []) != decomp_param_list:
                    cmd["params"] = decomp_param_list
                    params_updated += 1

            if params_updated > 0:
                print(f"  Updated params for {params_updated} movements")

    sync_flags_vars(db, sources, version, fetch_url)


def find_v2_database_paths() -> list[Path]:
    """Find every v2 database the importer should refresh."""
    repo_root = Path(__file__).resolve().parent.parent
    paths = sorted(repo_root.glob("*_v2.json"))

    custom_db_dir = repo_root / "custom_databases"
    if custom_db_dir.is_dir():
        paths.extend(sorted(custom_db_dir.rglob("*_v2.json")))

    return paths


def import_decomp_data(db_path: Path) -> bool:
    """Refresh one v2 database from the configured decomp sources."""
    with open(db_path, "r", encoding="utf-8") as f:
        db = json.load(f)

    version = get_game_version(db)
    print(f"\nSyncing {db_path} ({version})")

    if version not in DECOMP_SOURCES:
        if version == "Diamond/Pearl":
            platinum_path = db_path.with_name("platinum_v2.json")
            if platinum_path.exists():
                with open(platinum_path, "r", encoding="utf-8") as f:
                    platinum_db = json.load(f)

                renamed = backfill_dp_placeholder_names_from_platinum(db, platinum_db)
                if renamed > 0:
                    print(
                        f"  Backfilled {renamed} Diamond/Pearl placeholder "
                        f"command name(s) from Platinum"
                    )
                    return write_db_if_changed(db_path, db)

        print(f"  Skipping: No decomp source configured for {version}")
        return False

    sources = DECOMP_SOURCES[version]
    scrcmd_content = fetch_url(sources["scrcmd"]) if "scrcmd" in sources else None

    sync_database(db, sources, version, scrcmd_content)
    if scrcmd_content:
        inject_macros_into_db(db, scrcmd_content)

    if write_db_if_changed(db_path, db):
        return True

    print("  ✓ Database is in sync with decomp")
    return False


def main() -> int:
    if len(sys.argv) > 1:
        print("sync_from_decomp.py no longer accepts arguments.")
        print("Run `python scripts/sync_from_decomp.py` to refresh every v2 database.")
        return 2

    v2_files = find_v2_database_paths()
    if not v2_files:
        print("No *_v2.json files found in repository")
        return 1

    changed_count = 0
    for path in v2_files:
        if import_decomp_data(path):
            changed_count += 1

    print(
        f"\nProcessed {len(v2_files)} v2 database(s); {changed_count} file(s) changed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
