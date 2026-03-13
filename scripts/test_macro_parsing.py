#!/usr/bin/env python3
"""Test cases for macro parsing bugs."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from sync_from_decomp import (
    ParsedMacro,
    detect_wrapper_macro,
    extract_macros,
    format_expansion_line,
    infer_param_default,
    parse_params,
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

    from sync_from_decomp import extract_macros_for_db

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
    print("Running macro parsing tests...")
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
