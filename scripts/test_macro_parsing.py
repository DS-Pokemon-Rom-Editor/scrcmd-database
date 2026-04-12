#!/usr/bin/env python3
"""Test cases for macro parsing bugs."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from sync_from_decomp import (
    MacroExpansion,
    ParsedMacro,
    build_id_to_name_map,
    compare_macros_with_db,
    detect_wrapper_macro,
    extract_first_opcode,
    extract_macros_for_db,
    extract_macros,
    extract_opcodes,
    format_expansion_line,
    infer_param_default,
    parse_params,
    parse_scrcmd_inc,
    parse_scrcmd_symbol_table,
    repair_duplicate_command_ids,
    upsert_imported_macro,
    update_db_from_sync,
    update_inferred_param_defaults,
)


def test_parse_params_defaults():
    """Test parsing macros with default values (name = default)."""
    params_str = "frames = FADE_SCREEN_SPEED_FAST, color = COLOR_BLACK"
    params = parse_params(params_str)

    assert len(params) == 2, f"Expected 2 params, got {len(params)}"
    assert params[0].name == "frames", f"Expected 'frames', got '{params[0].name}'"
    assert params[0].default == "FADE_SCREEN_SPEED_FAST", (
        f"Expected 'FADE_SCREEN_SPEED_FAST', got '{params[0].default}'"
    )
    assert params[1].name == "color", f"Expected 'color', got '{params[1].name}'"
    assert params[1].default == "COLOR_BLACK", (
        f"Expected 'COLOR_BLACK', got '{params[1].default}'"
    )

    print("  [PASS] Defaults parsing")


def test_parse_params_space_separated():
    """Test parsing macros with space-separated parameters."""
    params_str = "badge label"
    params = parse_params(params_str)

    assert len(params) == 2, f"Expected 2 params, got {len(params)}"
    assert params[0].name == "badge", f"Expected 'badge', got '{params[0].name}'"
    assert params[0].default is None, (
        f"Expected None default, got '{params[0].default}'"
    )
    assert params[1].name == "label", f"Expected 'label', got '{params[1].name}'"
    assert params[1].default is None, (
        f"Expected None default, got '{params[1].default}'"
    )

    print("  [PASS] Space-separated parsing")


def test_parse_params_comma_separated():
    """Test parsing macros with comma-separated parameters."""
    params_str = "varID, valueOrVarID, offset"
    params = parse_params(params_str)

    assert len(params) == 3, f"Expected 3 params, got {len(params)}"
    assert params[0].name == "varID", f"Expected 'varID', got '{params[0].name}'"
    assert params[1].name == "valueOrVarID", (
        f"Expected 'valueOrVarID', got '{params[1].name}'"
    )
    assert params[2].name == "offset", f"Expected 'offset', got '{params[2].name}'"
    assert all(p.default is None for p in params), "Expected all defaults to be None"

    print("  [PASS] Comma-separated parsing")


def test_parse_params_mixed_defaults():
    """Test parsing macros with some params having defaults and some not."""
    params_str = "requiredParam, optionalParam = DEFAULT_VALUE, anotherRequired"
    params = parse_params(params_str)

    assert len(params) == 3, f"Expected 3 params, got {len(params)}"
    assert params[0].name == "requiredParam", (
        f"Expected 'requiredParam', got '{params[0].name}'"
    )
    assert params[0].default is None, "Expected no default for requiredParam"
    assert params[1].name == "optionalParam", (
        f"Expected 'optionalParam', got '{params[1].name}'"
    )
    assert params[1].default == "DEFAULT_VALUE", (
        f"Expected 'DEFAULT_VALUE', got '{params[1].default}'"
    )
    assert params[2].name == "anotherRequired", (
        f"Expected 'anotherRequired', got '{params[2].name}'"
    )
    assert params[2].default is None, "Expected no default for anotherRequired"

    print("  [PASS] Mixed defaults parsing")


def test_parse_params_empty():
    """Test parsing empty parameter string."""
    params_str = ""
    params = parse_params(params_str)

    assert len(params) == 0, f"Expected 0 params, got {len(params)}"

    print("  [PASS] Empty params parsing")


def test_extract_macros_no_params():
    """Test extracting macros with no parameters (regression test for newline bug)."""
    content = """
    .macro SelectPokemonForUnionRoomBattle
        OpenPartyMenuForUnionRoomBattle
        ReturnToField
        .endm
    """

    macros = extract_macros(content)

    assert len(macros) == 1, f"Expected 1 macro, got {len(macros)}"
    name, params_str, body = macros[0]

    assert name == "SelectPokemonForUnionRoomBattle", (
        f"Expected 'SelectPokemonForUnionRoomBattle', got '{name}'"
    )
    assert params_str == "", f"Expected empty params_str, got '{params_str}'"
    assert "OpenPartyMenuForUnionRoomBattle" in body, (
        f"Expected body to contain 'OpenPartyMenuForUnionRoomBattle', got '{body}'"
    )
    assert "ReturnToField" in body, (
        f"Expected body to contain 'ReturnToField', got '{body}'"
    )

    print("  [PASS] No-params macro extraction")


def test_extract_macros_with_defaults():
    """Test extracting macros with default values."""
    content = """
    .macro FadeScreenOut frames = FADE_SCREEN_SPEED_FAST, color = COLOR_BLACK
        FadeScreen FADE_SCREEN_CMD_STEPS, \\frames, FADE_TYPE_BRIGHTNESS_OUT, \\color
        .endm
    """

    macros = extract_macros(content)

    assert len(macros) == 1, f"Expected 1 macro, got {len(macros)}"
    name, params_str, body = macros[0]

    assert name == "FadeScreenOut", f"Expected 'FadeScreenOut', got '{name}'"
    assert params_str == "frames = FADE_SCREEN_SPEED_FAST, color = COLOR_BLACK", (
        f"Got params_str: '{params_str}'"
    )
    assert "\\frames" in body, f"Expected body to contain '\\frames'"
    assert "\\color" in body, f"Expected body to contain '\\color'"

    print("  [PASS] Default-value macro extraction")


def test_extract_macros_space_separated():
    """Test extracting macros with space-separated parameters."""
    content = """
    .macro GoToIfBadgeAcquired badge label
        CheckBadgeAcquired \\badge, VAR_RESULT
        GoToIfEq VAR_RESULT, TRUE, \\label
        .endm
    """

    macros = extract_macros(content)

    assert len(macros) == 1, f"Expected 1 macro, got {len(macros)}"
    name, params_str, body = macros[0]

    assert name == "GoToIfBadgeAcquired", (
        f"Expected 'GoToIfBadgeAcquired', got '{name}'"
    )
    assert params_str == "badge label", f"Got params_str: '{params_str}'"
    assert "\\badge" in body, f"Expected body to contain '\\badge'"
    assert "\\label" in body, f"Expected body to contain '\\label'"

    print("  [PASS] Space-separated macro extraction")


def test_extract_macros_comma_separated():
    """Test extracting macros with comma-separated parameters."""
    content = """
    .macro GoToIfLt varID, valueOrVarID, offset
        CompareVar \\varID, \\valueOrVarID
        GoToIf 0, \\offset
        .endm
    """

    macros = extract_macros(content)

    assert len(macros) == 1, f"Expected 1 macro, got {len(macros)}"
    name, params_str, body = macros[0]

    assert name == "GoToIfLt", f"Expected 'GoToIfLt', got '{name}'"
    assert params_str == "varID, valueOrVarID, offset", (
        f"Got params_str: '{params_str}'"
    )
    assert "\\varID" in body, f"Expected body to contain '\\varID'"
    assert "\\valueOrVarID" in body, f"Expected body to contain '\\valueOrVarID'"
    assert "\\offset" in body, f"Expected body to contain '\\offset'"

    print("  [PASS] Comma-separated macro extraction")


def test_extract_macros_mode_expansion():
    """Test extracting macros that expand to another macro call with constant."""
    content = """
    .macro PrepareMysteryGiftReceivedMsg destTextBank, destStringID
        MysteryGiftGive MYSTERY_GIFT_RECEIVED, \\destTextBank, \\destStringID
        .endm
    """

    macros = extract_macros(content)

    assert len(macros) == 1, f"Expected 1 macro, got {len(macros)}"
    name, params_str, body = macros[0]

    assert name == "PrepareMysteryGiftReceivedMsg", (
        f"Expected 'PrepareMysteryGiftReceivedMsg', got '{name}'"
    )
    assert params_str == "destTextBank, destStringID", f"Got params_str: '{params_str}'"
    assert "\\destTextBank" in body, f"Expected body to contain '\\destTextBank'"
    assert "\\destStringID" in body, f"Expected body to contain '\\destStringID'"
    assert "MYSTERY_GIFT_RECEIVED" in body, (
        f"Expected body to contain constant 'MYSTERY_GIFT_RECEIVED'"
    )

    print("  [PASS] Mode-expansion macro extraction")


def test_format_expansion_line_preserves_arithmetic_expression():
    """Regression test: arithmetic inside a macro arg stays one arg."""
    params = parse_params("varID, lower, upper, offset")
    line = r"GoToIfInRange \\varID, \\lower + 1, \\upper, \\offset"

    formatted = format_expansion_line(line, params)

    assert formatted == r"GoToIfInRange \$varID, \$lower + 1, \$upper, \$offset", (
        f"Unexpected formatted expansion: '{formatted}'"
    )

    print("  [PASS] Arithmetic expression expansion formatting")


def test_detect_wrapper_macro_preserves_arithmetic_expression():
    """Regression test: wrapper arg parsing keeps '\\lower + 1' together."""
    body = r"""
        GoToIfInRange \\varID, \\lower + 1, \\upper, \\offset
    """
    expansion = detect_wrapper_macro(body, {"GoToIfInRange"})

    assert expansion is not None, "Expected wrapper macro to be detected"
    assert expansion.target_macro == "GoToIfInRange", (
        f"Expected target 'GoToIfInRange', got '{expansion.target_macro}'"
    )
    assert expansion.args == [r"\\varID", r"\\lower + 1", r"\\upper", r"\\offset"], (
        f"Unexpected args: {expansion.args}"
    )

    print("  [PASS] Arithmetic expression wrapper parsing")


def test_format_expansion_line_splits_whitespace_prefixed_constant_args():
    """Regression test: mixed whitespace and comma args are normalized correctly."""
    params = parse_params("bankDestVar, messageDestVar")
    line = r"LoadTVFramingMessage TV_PROGRAM_FRAMING_MESSAGE_FAREWELL_EXTENDED \\bankDestVar, \\messageDestVar"

    formatted = format_expansion_line(line, params)

    assert formatted == (
        r"LoadTVFramingMessage TV_PROGRAM_FRAMING_MESSAGE_FAREWELL_EXTENDED, \$bankDestVar, \$messageDestVar"
    ), f"Unexpected formatted expansion: '{formatted}'"

    print("  [PASS] Mixed whitespace/comma expansion formatting")


def test_parse_scrcmd_symbol_table_from_script_commands_header():
    """Parse ScriptCommand order into SCRCMD_* -> opcode mapping."""
    header = """
    ScriptCommand(SCRCMD_NOOP, ScrCmd_Noop)
    ScriptCommand(SCRCMD_DUMMY, ScrCmd_Dummy)
    ScriptCommand(SCRCMD_END, ScrCmd_End)
    ScriptCommand(SCRCMD_WAITTIME, ScrCmd_WaitTime)
"""
    mapping = parse_scrcmd_symbol_table(header)

    assert mapping["SCRCMD_NOOP"] == 0, (
        f"Expected SCRCMD_NOOP=0, got {mapping.get('SCRCMD_NOOP')}"
    )
    assert mapping["SCRCMD_DUMMY"] == 1, (
        f"Expected SCRCMD_DUMMY=1, got {mapping.get('SCRCMD_DUMMY')}"
    )
    assert mapping["SCRCMD_END"] == 2, (
        f"Expected SCRCMD_END=2, got {mapping.get('SCRCMD_END')}"
    )
    assert mapping["SCRCMD_WAITTIME"] == 3, (
        f"Expected SCRCMD_WAITTIME=3, got {mapping.get('SCRCMD_WAITTIME')}"
    )

    print("  [PASS] SCRCMD symbol table parsing")


def test_extract_symbolic_opcode_and_first_opcode():
    """Resolve .short SCRCMD_* using a parsed symbol table."""
    header = """
    ScriptCommand(SCRCMD_NOOP, ScrCmd_Noop)
    ScriptCommand(SCRCMD_WAITTIME, ScrCmd_WaitTime)
    ScriptCommand(SCRCMD_SETFLAG, ScrCmd_SetFlag)
"""
    mapping = parse_scrcmd_symbol_table(header)

    body = """
    .short SCRCMD_WAITTIME
    .short \\frames
    .short \\countdownVarID
"""
    first = extract_first_opcode(body, mapping)
    all_ops = extract_opcodes(body, mapping)

    assert first == 1, f"Expected first opcode 1, got {first}"
    assert all_ops == [1], f"Expected [1], got {all_ops}"

    print("  [PASS] Symbolic opcode extraction")


def test_parse_scrcmd_inc_with_symbolic_opcode():
    """parse_scrcmd_inc should produce numeric opcodes for symbolic SCRCMD_* emissions."""
    header = """
    ScriptCommand(SCRCMD_NOOP, ScrCmd_Noop)
    ScriptCommand(SCRCMD_WAITTIME, ScrCmd_WaitTime)
"""
    mapping = parse_scrcmd_symbol_table(header)

    content = r"""
    .macro WaitTime frames, countdownVarID
    .short SCRCMD_WAITTIME
    .short \frames
    .short \countdownVarID
    .endm
"""
    parsed, primitives = parse_scrcmd_inc(content, scrcmd_symbol_to_opcode=mapping)

    assert "WaitTime" in parsed, "Expected WaitTime macro to be parsed"
    assert parsed["WaitTime"].opcodes == [1], (
        f"Expected WaitTime opcode [1], got {parsed['WaitTime'].opcodes}"
    )
    assert primitives == {}, f"Expected no primitives, got {primitives}"

    print("  [PASS] parse_scrcmd_inc symbolic opcode resolution")


def test_parse_scrcmd_inc_extracts_preceding_comment_descriptions():
    """Preceding `//` and `;` comment blocks should become decomp descriptions."""
    content = r"""
    // Counts alive mons in the party and stores the result in the destVar,
    // but excludes the one at the party slot provided.
    .macro CountAliveMonsExcept destVarID, partySlot
    .short 100
    .short \destVarID
    .short \partySlot
    .endm

    ; Compares two variables
    .macro CompareVarToVar a, b
    .short 18
    .short \a
    .short \b
    .endm
"""
    parsed, _ = parse_scrcmd_inc(content)

    assert parsed["CountAliveMonsExcept"].description == (
        "Counts alive mons in the party and stores the result in the destVar, "
        "but excludes the one at the party slot provided."
    ), parsed["CountAliveMonsExcept"].description
    assert parsed["CompareVarToVar"].description == "Compares two variables", (
        parsed["CompareVarToVar"].description
    )

    print("  [PASS] Preceding comment descriptions are parsed")


def test_parse_scrcmd_inc_skips_unresolved_symbolic_opcode_instead_of_using_trailing_zero():
    """Regression test: unresolved SCRCMD_* should not fall through to later literal params."""
    content = r"""
    .macro MessageSeenBanlistSpecies banlistMsgStartIdx, numPokemonRequired
    .short SCRCMD_MESSAGESEENBANLISTSPECIES
    .byte \banlistMsgStartIdx
    .short \numPokemonRequired
    .short 0
    .byte 0
    .endm
"""
    parsed, primitives = parse_scrcmd_inc(content)

    assert "MessageSeenBanlistSpecies" not in parsed, (
        "Expected unresolved symbolic opcode macro to be skipped instead of "
        "being misparsed with opcode 0"
    )
    assert primitives == {}, f"Expected no primitives, got {primitives}"

    print("  [PASS] Unresolved symbolic opcode is skipped safely")


def test_parse_scrcmd_inc_skips_script_entry_end_helper_macro():
    """Decomp-only helper macros like ScriptEntryEnd should never enter the database sync."""
    content = """
    .macro ScriptEntryEnd
        .short 0
        .endm
    """
    parsed, primitives = parse_scrcmd_inc(content)

    assert "ScriptEntryEnd" not in parsed, "Expected ScriptEntryEnd to be skipped"
    assert primitives == {}, f"Expected no primitives, got {primitives}"

    print("  [PASS] ScriptEntryEnd is skipped")


def test_compare_macros_with_db_does_not_readd_existing_call_common_script_wrapper():
    """Existing CallCommonScript wrappers should not be reported as missing script commands."""
    db = {
        "commands": {
            "Common_HandleSignpostInput": {
                "type": "macro",
                "params": [],
                "expansion": ["CallCommonScript 0x7D0"],
            }
        }
    }
    decomp_macros = {
        "Common_HandleSignpostInput": ParsedMacro(
            name="Common_HandleSignpostInput",
            params=[],
            opcodes=[],
            expansion=MacroExpansion("CallCommonScript", ["0x7D0"]),
            body="CallCommonScript 0x7D0",
        )
    }

    missing, extra, mismatched, wrappers, param_updates = compare_macros_with_db(
        db, decomp_macros, "script_cmd"
    )

    assert missing == [], f"Expected no missing entries, got {missing}"
    assert mismatched == [], f"Expected no mismatches, got {mismatched}"
    assert extra == [], f"Expected no extras, got {extra}"
    assert len(wrappers) == 1, f"Expected one wrapper, got {wrappers}"
    assert param_updates == [], f"Expected no param updates, got {param_updates}"

    print("  [PASS] Existing CallCommonScript wrappers are not re-added")


def test_extract_macros_for_db_resolves_macro_calls_to_existing_db_command_names():
    """Imported macro expansions should target the command keys that actually exist in the DB."""
    commands = {
        "PlayFanfare": {"type": "script_cmd", "id": 73, "params": []},
        "LockAll": {"type": "script_cmd", "id": 90, "params": []},
        "Message": {"type": "script_cmd", "id": 91, "params": []},
        "WaitABXPadPress": {
            "type": "script_cmd",
            "id": 49,
            "legacy_name": "WaitButton",
            "params": [],
        },
        "CloseMessage": {"type": "script_cmd", "id": 92, "params": []},
        "ReleaseAll": {"type": "script_cmd", "id": 93, "params": []},
    }
    id_to_name = build_id_to_name_map(commands, "script_cmd")
    content = r"""
    .macro PlaySE seqID
    .short 73
    .short \seqID
    .endm

    .macro LockAll
    .short 90
    .endm

    .macro Message messageID
    .short 91
    .short \messageID
    .endm

    .macro WaitButton
    .short 49
    .endm

    .macro CloseMessage
    .short 92
    .endm

    .macro ReleaseAll
    .short 93
    .endm

    .macro EventMessage messageID
    PlaySE SEQ_SE_CONFIRM
    LockAll
    Message \messageID
    WaitButton
    CloseMessage
    ReleaseAll
    .endm
"""
    macros = extract_macros_for_db(content, id_to_name, commands=commands)

    assert macros["EventMessage"]["expansion"] == [
        "PlayFanfare SEQ_SE_CONFIRM",
        "LockAll",
        "Message $messageID",
        "WaitABXPadPress",
        "CloseMessage",
        "ReleaseAll",
    ], macros["EventMessage"]["expansion"]

    print("  [PASS] Imported macros resolve references to existing DB keys")


def test_upsert_imported_macro_reuses_existing_equivalent_macro_alias():
    """Equivalent legacy macro aliases should be updated in place instead of duplicated."""
    commands = {
        "compare_var_to_var": {"type": "script_cmd", "id": 18, "params": []},
        "compare_var_to_value": {"type": "script_cmd", "id": 19, "params": []},
        "compare": {
            "type": "macro",
            "params": [
                {"name": "var", "type": "var"},
                {"name": "arg", "type": "u16"},
            ],
            "variants": [
                {
                    "condition": "((arg >= VARS_START && arg <= VARS_END) || (arg >= SPECIAL_VARS_START && arg <= SPECIAL_VARS_END))",
                    "expansion": ["compare_var_to_var $var, $arg"],
                },
                {
                    "condition": "else",
                    "expansion": ["compare_var_to_value $var, $arg"],
                },
            ],
        },
    }
    id_to_name = build_id_to_name_map(commands, "script_cmd")
    content = r"""
    .macro CompareVarToVar var, arg
    .short 18
    .short \var
    .short \arg
    .endm

    .macro CompareVarToValue var, arg
    .short 19
    .short \var
    .short \arg
    .endm

    .macro Compare var, arg
        .if ((\arg >= VARS_START && \arg <= VARS_END) || (\arg >= SPECIAL_VARS_START && \arg <= SPECIAL_VARS_END))
            CompareVarToVar \var, \arg
        .else
            CompareVarToValue \var, \arg
        .endif
    .endm
"""
    macros = extract_macros_for_db(content, id_to_name, commands=commands)
    action = upsert_imported_macro(commands, "Compare", macros["Compare"])

    assert action == "matched_alias", f"Expected matched_alias, got {action}"
    assert "Compare" not in commands, "Equivalent alias should not create a duplicate"
    assert commands["compare"]["variants"] == macros["Compare"]["variants"], (
        "Existing alias should retain the imported decomp semantics"
    )

    print("  [PASS] Equivalent macro aliases are reused instead of duplicated")


def test_update_db_from_sync_resolves_split_opcode_pairs_in_one_pass():
    """ID-based rename chains should converge without leaving duplicate script command IDs."""
    db = {
        "commands": {
            "PlayFanfare": {"type": "script_cmd", "id": 73, "params": []},
            "StopFanfare": {"type": "script_cmd", "id": 74, "params": []},
            "WaitFanfare": {"type": "script_cmd", "id": 75, "params": []},
            "PlaySound": {"type": "script_cmd", "id": 78, "params": []},
            "WaitSound": {"type": "script_cmd", "id": 79, "params": []},
            "SetMenuYOriginSide": {
                "type": "script_cmd",
                "id": 826,
                "params": [{"name": "bottomSide", "type": "u8"}],
            },
        }
    }
    decomp_macros = {
        "PlaySE": ParsedMacro(name="PlaySE", params=[], opcodes=[73]),
        "StopSE": ParsedMacro(name="StopSE", params=[], opcodes=[74]),
        "PlayFanfare": ParsedMacro(name="PlayFanfare", params=[], opcodes=[78]),
        "WaitSE": ParsedMacro(name="WaitSE", params=[], opcodes=[75]),
        "WaitFanfare": ParsedMacro(name="WaitFanfare", params=[], opcodes=[79]),
        "SetMenuXOriginSide": ParsedMacro(
            name="SetMenuXOriginSide", params=[], opcodes=[826]
        ),
        "SetMenuYOriginSide": ParsedMacro(
            name="SetMenuYOriginSide", params=[], opcodes=[827]
        ),
    }

    missing, extra, mismatched, wrappers, param_updates = compare_macros_with_db(
        db, decomp_macros, "script_cmd"
    )
    changes = update_db_from_sync(db, missing, mismatched, "script_cmd")

    commands = db["commands"]
    assert changes == 7, f"Expected 7 changes, got {changes}"
    assert commands["PlaySE"]["id"] == 73, commands["PlaySE"]
    assert commands["StopSE"]["id"] == 74, commands["StopSE"]
    assert commands["PlayFanfare"]["id"] == 78, commands["PlayFanfare"]
    assert commands["WaitSE"]["id"] == 75, commands["WaitSE"]
    assert commands["WaitFanfare"]["id"] == 79, commands["WaitFanfare"]
    assert commands["SetMenuXOriginSide"]["id"] == 826, commands["SetMenuXOriginSide"]
    assert commands["SetMenuYOriginSide"]["id"] == 827, commands["SetMenuYOriginSide"]
    assert "PlaySound" not in commands, "Expected PlaySound to be renamed by opcode"
    assert "WaitSound" not in commands, "Expected WaitSound to be renamed by opcode"

    seen_ids = {}
    for name, entry in commands.items():
        if entry.get("type") != "script_cmd":
            continue
        opcode = entry["id"]
        assert opcode not in seen_ids, (
            f"Duplicate script command opcode {opcode}: {seen_ids[opcode]} and {name}"
        )
        seen_ids[opcode] = name

    assert extra == [], f"Expected no extras, got {extra}"
    assert wrappers == [], f"Expected no wrappers, got {wrappers}"
    assert param_updates == [], f"Expected no param updates, got {param_updates}"

    print("  [PASS] Split opcode/name pairs converge without duplicate IDs")


def test_compare_macros_with_db_prefers_opcode_owner_for_primitive_sync():
    """Primitive comparison should match by opcode before falling back to the same name."""
    db = {
        "commands": {
            "PlayFanfare": {"type": "script_cmd", "id": 73, "params": []},
            "PlaySound": {"type": "script_cmd", "id": 78, "params": []},
        }
    }
    decomp_macros = {
        "PlaySE": ParsedMacro(name="PlaySE", params=[], opcodes=[73]),
        "PlayFanfare": ParsedMacro(name="PlayFanfare", params=[], opcodes=[78]),
    }

    missing, extra, mismatched, wrappers, param_updates = compare_macros_with_db(
        db, decomp_macros, "script_cmd"
    )

    assert missing == [], f"Expected no missing commands, got {missing}"
    assert extra == [], f"Expected no extra commands, got {extra}"
    assert wrappers == [], f"Expected no wrappers, got {wrappers}"
    assert param_updates == [], f"Expected no param updates, got {param_updates}"
    assert mismatched == [
        {
            "name": "PlaySE",
            "decomp_opcode": 73,
            "db_name": "PlayFanfare",
            "is_conditional": False,
            "all_opcodes": [73],
            "params": [],
            "description": None,
        },
        {
            "name": "PlayFanfare",
            "decomp_opcode": 78,
            "db_name": "PlaySound",
            "is_conditional": False,
            "all_opcodes": [78],
            "params": [],
            "description": None,
        },
    ], mismatched

    print("  [PASS] Primitive comparison prefers opcode ownership")


def test_update_db_from_sync_preserves_order_and_existing_description_on_rename():
    """Renames should not sort the whole object or clobber a good existing description."""
    db = {
        "commands": {
            "Before": {"type": "script_cmd", "id": 1, "description": "before"},
            "OldName": {"type": "script_cmd", "id": 73, "description": "keep me"},
            "After": {"type": "script_cmd", "id": 99, "description": "after"},
        }
    }
    mismatched = [
        {
            "name": "NewName",
            "decomp_opcode": 73,
            "db_name": "OldName",
            "is_conditional": False,
            "all_opcodes": [73],
            "params": [],
            "description": None,
        }
    ]

    changes = update_db_from_sync(db, [], mismatched, "script_cmd")

    assert changes == 1, f"Expected 1 change, got {changes}"
    assert list(db["commands"].keys()) == ["Before", "NewName", "After"], db[
        "commands"
    ]
    assert db["commands"]["NewName"]["description"] == "keep me", db["commands"][
        "NewName"
    ]

    print("  [PASS] Renames preserve order and existing descriptions")


def test_update_db_from_sync_prefers_opcode_owner_over_stale_mismatch_name():
    """Primitive updates should resolve the current entry by opcode, not by a stale compare-time name."""
    db = {
        "commands": {
            "PlaySE": {"type": "script_cmd", "id": 73, "params": []},
            "__tmp__script_cmd__PlayFanfare__73": {
                "type": "script_cmd",
                "id": 78,
                "params": [],
            },
        }
    }
    mismatched = [
        {
            "name": "PlayFanfare",
            "decomp_opcode": 78,
            "db_name": "PlaySound",
            "is_conditional": False,
            "all_opcodes": [78],
            "params": [],
            "description": None,
        }
    ]

    changes = update_db_from_sync(db, [], mismatched, "script_cmd")

    assert changes == 1, f"Expected 1 change, got {changes}"
    assert "PlayFanfare" in db["commands"], db["commands"]
    assert db["commands"]["PlayFanfare"]["id"] == 78, db["commands"]["PlayFanfare"]
    assert "__tmp__script_cmd__PlayFanfare__73" not in db["commands"], db["commands"]

    print("  [PASS] Primitive updates prefer opcode owner over stale names")


def test_update_db_from_sync_preserves_cross_type_name_collisions_until_later_pass():
    """A script command rename must not overwrite an existing movement with the same target name."""
    db = {
        "commands": {
            "Nop": {"type": "script_cmd", "id": 0, "description": "nop"},
            "end": {"type": "script_cmd", "id": 2, "description": "script end"},
            "End": {"type": "movement", "id": 254, "description": "movement end"},
            "Wait": {"type": "script_cmd", "id": 3, "description": "wait"},
        }
    }
    script_mismatched = [
        {
            "name": "End",
            "decomp_opcode": 2,
            "db_name": "end",
            "is_conditional": False,
            "all_opcodes": [2],
            "params": [],
            "description": "Exits script execution and returns control to the player",
        }
    ]

    changes = update_db_from_sync(db, [], script_mismatched, "script_cmd")
    assert changes == 1, f"Expected 1 change, got {changes}"
    assert "End" in db["commands"], "Script command rename should succeed"
    assert db["commands"]["End"]["type"] == "script_cmd", db["commands"]["End"]

    movement_temp_names = [
        name
        for name, entry in db["commands"].items()
        if entry.get("type") == "movement" and entry.get("id") == 254
    ]
    assert len(movement_temp_names) == 1, movement_temp_names
    movement_temp_name = movement_temp_names[0]
    assert movement_temp_name != "End", movement_temp_name

    movement_mismatched = [
        {
            "name": "EndMovement",
            "decomp_opcode": 254,
            "db_name": movement_temp_name,
            "params": [],
        }
    ]
    changes = update_db_from_sync(db, [], movement_mismatched, "movement")
    assert changes == 1, f"Expected 1 movement change, got {changes}"

    assert "End" in db["commands"] and db["commands"]["End"]["type"] == "script_cmd"
    assert "EndMovement" in db["commands"], "Movement should be renamed on its own pass"
    assert db["commands"]["EndMovement"]["type"] == "movement"

    print("  [PASS] Cross-type rename collisions no longer overwrite commands")


def test_repair_duplicate_command_ids_keeps_canonical_decomp_name():
    """Existing duplicate opcode entries should collapse to the canonical decomp name."""
    db = {
        "commands": {
            "PlayFanfare": {
                "type": "script_cmd",
                "id": 78,
                "description": "Imported from decomp: PlayFanfare",
                "params": [],
            },
            "PlaySound": {
                "type": "script_cmd",
                "id": 78,
                "legacy_name": "PlaySound",
                "description": "Pauses current music, then Plays Sound",
                "params": [],
            },
            "WaitFanfare": {
                "type": "script_cmd",
                "id": 79,
                "description": "Imported from decomp: WaitFanfare",
                "params": [],
            },
            "WaitSound": {
                "type": "script_cmd",
                "id": 79,
                "legacy_name": "WaitSound",
                "description": "Waits for Sound to finish, then resumes music",
                "params": [],
            },
        }
    }
    canonical_name_by_opcode = {78: "PlayFanfare", 79: "WaitFanfare"}

    removed = repair_duplicate_command_ids(
        db, "script_cmd", canonical_name_by_opcode
    )

    commands = db["commands"]
    assert removed == 2, f"Expected 2 duplicates removed, got {removed}"
    assert "PlaySound" not in commands, "Expected stale PlaySound alias to be removed"
    assert "WaitSound" not in commands, "Expected stale WaitSound alias to be removed"
    assert commands["PlayFanfare"]["legacy_name"] == "PlaySound", commands[
        "PlayFanfare"
    ]
    assert commands["PlayFanfare"]["description"] == (
        "Pauses current music, then Plays Sound"
    ), commands["PlayFanfare"]
    assert commands["WaitFanfare"]["legacy_name"] == "WaitSound", commands[
        "WaitFanfare"
    ]

    print("  [PASS] Duplicate opcode entries collapse to canonical decomp names")


def test_recursive_macro_condition_becomes_variant():
    """Regression test: recursive macro conditions should be preserved as variants."""
    content = """
    .macro GoToIfInRange varID, lower, upper, offset
        CompareVar \\varID, \\lower
        GoToIf 1, \\offset
        .if \\lower < \\upper
            GoToIfInRange \\varID, \\lower + 1, \\upper, \\offset
        .endif
        .endm
    """

    macros = extract_macros_for_db(content)
    macro = macros["GoToIfInRange"]

    assert "variants" in macro, "Expected recursive macro to be stored as variants"
    assert len(macro["variants"]) == 2, (
        f"Expected 2 variants, got {len(macro['variants'])}"
    )
    assert macro["variants"][0]["condition"] == "lower < upper", (
        f"Unexpected first condition: {macro['variants'][0]['condition']}"
    )
    assert macro["variants"][0]["expansion"] == [
        "CompareVar $varID, $lower",
        "GoToIf 1, $offset",
        "GoToIfInRange $varID, $lower + 1, $upper, $offset",
    ], f"Unexpected first variant expansion: {macro['variants'][0]['expansion']}"
    assert macro["variants"][1]["condition"] == "else", (
        f"Unexpected second condition: {macro['variants'][1]['condition']}"
    )
    assert macro["variants"][1]["expansion"] == [
        "CompareVar $varID, $lower",
        "GoToIf 1, $offset",
    ], f"Unexpected second variant expansion: {macro['variants'][1]['expansion']}"

    print("  [PASS] Recursive macro condition variants")


def test_infer_param_default_only_defaults_result_var_when_dest_and_result_exist():
    """Regression test: destVar should not default when a separate result var exists."""
    emitted_params = ["word", "destVar", "resultVar"]

    assert (
        infer_param_default("destVar", "ChooseCustomMessageWord", emitted_params)
        is None
    )
    assert (
        infer_param_default("resultVar", "ChooseCustomMessageWord", emitted_params)
        == "VAR_RESULT"
    )

    print("  [PASS] Context-aware VAR_RESULT inference for dest/result pair")


def test_update_inferred_param_defaults_only_applies_to_result_var_when_dest_and_result_exist():
    """Regression test: stale destVar default is removed and resultVar keeps VAR_RESULT."""
    macro = ParsedMacro(
        name="ChooseCustomMessageWord",
        params=[],
        opcodes=[579],
        emitted_params=["word", "destVar", "resultVar"],
    )
    db_params = [
        {"name": "arg_0", "type": "u16"},
        {"name": "destVar", "type": "var", "default": "VAR_RESULT"},
        {"name": "resultVar", "type": "var", "default": "VAR_RESULT"},
    ]

    changed = update_inferred_param_defaults(
        db_params, macro, "ChooseCustomMessageWord"
    )

    assert changed is True, "Expected inferred defaults update to report a change"
    assert "default" not in db_params[1], (
        "destVar should have its stale VAR_RESULT default removed"
    )
    assert db_params[2].get("default") == "VAR_RESULT", (
        f"Expected resultVar default to stay VAR_RESULT, got {db_params[2].get('default')}"
    )

    print("  [PASS] Inferred defaults remove stale destVar default")


def test_update_inferred_param_defaults_keeps_unique_destvar_default():
    """Regression test: a unique dest-style default should be preserved."""
    macro = ParsedMacro(
        name="DrawSignpostInstantMessage",
        params=[],
        opcodes=[54],
        emitted_params=["messageID", "signpostType", "signpostNARCMemberIdx", "unused"],
    )
    db_params = [
        {"name": "text_slot", "type": "u8"},
        {"name": "type", "type": "u8"},
        {"name": "icon", "type": "u16", "default": "0"},
        {"name": "unused", "type": "u16", "default": "VAR_RESULT"},
    ]

    changed = update_inferred_param_defaults(
        db_params, macro, "DrawSignpostInstantMessage"
    )

    assert changed is False, "Expected unique destVar-style default to be preserved"
    assert db_params[3].get("default") == "VAR_RESULT", (
        f"Expected unique default to stay VAR_RESULT, got {db_params[3].get('default')}"
    )

    print("  [PASS] Unique destVar-style defaults are preserved")


def run_all_tests():
    """Run all test cases."""
    print("Running macro parsing tests...\n")
    test_parse_scrcmd_symbol_table_from_script_commands_header()
    test_extract_symbolic_opcode_and_first_opcode()
    test_parse_scrcmd_inc_with_symbolic_opcode()
    test_parse_scrcmd_inc_extracts_preceding_comment_descriptions()
    test_parse_scrcmd_inc_skips_unresolved_symbolic_opcode_instead_of_using_trailing_zero()
    test_parse_scrcmd_inc_skips_script_entry_end_helper_macro()
    test_compare_macros_with_db_does_not_readd_existing_call_common_script_wrapper()
    test_extract_macros_for_db_resolves_macro_calls_to_existing_db_command_names()
    test_upsert_imported_macro_reuses_existing_equivalent_macro_alias()
    test_update_db_from_sync_resolves_split_opcode_pairs_in_one_pass()
    test_compare_macros_with_db_prefers_opcode_owner_for_primitive_sync()
    test_update_db_from_sync_preserves_order_and_existing_description_on_rename()
    test_update_db_from_sync_prefers_opcode_owner_over_stale_mismatch_name()
    test_update_db_from_sync_preserves_cross_type_name_collisions_until_later_pass()
    test_repair_duplicate_command_ids_keeps_canonical_decomp_name()
    print()

    # Param parsing tests
    print("Parameter parsing tests:")
    test_parse_params_defaults()
    test_parse_params_space_separated()
    test_parse_params_comma_separated()
    test_parse_params_mixed_defaults()
    test_parse_params_empty()

    print()

    # Macro extraction tests
    print("Macro extraction tests:")
    test_extract_macros_no_params()
    test_extract_macros_with_defaults()
    test_extract_macros_space_separated()
    test_extract_macros_comma_separated()
    test_extract_macros_mode_expansion()
    test_format_expansion_line_preserves_arithmetic_expression()
    test_detect_wrapper_macro_preserves_arithmetic_expression()
    test_format_expansion_line_splits_whitespace_prefixed_constant_args()
    test_recursive_macro_condition_becomes_variant()
    test_infer_param_default_only_defaults_result_var_when_dest_and_result_exist()
    test_update_inferred_param_defaults_only_applies_to_result_var_when_dest_and_result_exist()
    test_update_inferred_param_defaults_keeps_unique_destvar_default()

    print()
    print("[PASS] All tests passed!")


if __name__ == "__main__":
    run_all_tests()
