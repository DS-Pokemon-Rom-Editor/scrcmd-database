#!/usr/bin/env python3
"""Test cases for macro parsing bugs."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from sync_from_decomp import parse_params, extract_macros


def test_parse_params_defaults():
    """Test parsing macros with default values (name = default)."""
    params_str = "frames = FADE_SCREEN_SPEED_FAST, color = COLOR_BLACK"
    params = parse_params(params_str)
    
    assert len(params) == 2, f"Expected 2 params, got {len(params)}"
    assert params[0].name == "frames", f"Expected 'frames', got '{params[0].name}'"
    assert params[0].default == "FADE_SCREEN_SPEED_FAST", f"Expected 'FADE_SCREEN_SPEED_FAST', got '{params[0].default}'"
    assert params[1].name == "color", f"Expected 'color', got '{params[1].name}'"
    assert params[1].default == "COLOR_BLACK", f"Expected 'COLOR_BLACK', got '{params[1].default}'"
    
    print("  [PASS] Defaults parsing")


def test_parse_params_space_separated():
    """Test parsing macros with space-separated parameters."""
    params_str = "badge label"
    params = parse_params(params_str)
    
    assert len(params) == 2, f"Expected 2 params, got {len(params)}"
    assert params[0].name == "badge", f"Expected 'badge', got '{params[0].name}'"
    assert params[0].default is None, f"Expected None default, got '{params[0].default}'"
    assert params[1].name == "label", f"Expected 'label', got '{params[1].name}'"
    assert params[1].default is None, f"Expected None default, got '{params[1].default}'"
    
    print("  [PASS] Space-separated parsing")


def test_parse_params_comma_separated():
    """Test parsing macros with comma-separated parameters."""
    params_str = "varID, valueOrVarID, offset"
    params = parse_params(params_str)
    
    assert len(params) == 3, f"Expected 3 params, got {len(params)}"
    assert params[0].name == "varID", f"Expected 'varID', got '{params[0].name}'"
    assert params[1].name == "valueOrVarID", f"Expected 'valueOrVarID', got '{params[1].name}'"
    assert params[2].name == "offset", f"Expected 'offset', got '{params[2].name}'"
    assert all(p.default is None for p in params), "Expected all defaults to be None"
    
    print("  [PASS] Comma-separated parsing")


def test_parse_params_mixed_defaults():
    """Test parsing macros with some params having defaults and some not."""
    params_str = "requiredParam, optionalParam = DEFAULT_VALUE, anotherRequired"
    params = parse_params(params_str)
    
    assert len(params) == 3, f"Expected 3 params, got {len(params)}"
    assert params[0].name == "requiredParam", f"Expected 'requiredParam', got '{params[0].name}'"
    assert params[0].default is None, "Expected no default for requiredParam"
    assert params[1].name == "optionalParam", f"Expected 'optionalParam', got '{params[1].name}'"
    assert params[1].default == "DEFAULT_VALUE", f"Expected 'DEFAULT_VALUE', got '{params[1].default}'"
    assert params[2].name == "anotherRequired", f"Expected 'anotherRequired', got '{params[2].name}'"
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
    
    assert name == "SelectPokemonForUnionRoomBattle", f"Expected 'SelectPokemonForUnionRoomBattle', got '{name}'"
    assert params_str == "", f"Expected empty params_str, got '{params_str}'"
    assert "OpenPartyMenuForUnionRoomBattle" in body, f"Expected body to contain 'OpenPartyMenuForUnionRoomBattle', got '{body}'"
    assert "ReturnToField" in body, f"Expected body to contain 'ReturnToField', got '{body}'"
    
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
    assert params_str == "frames = FADE_SCREEN_SPEED_FAST, color = COLOR_BLACK", f"Got params_str: '{params_str}'"
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
    
    assert name == "GoToIfBadgeAcquired", f"Expected 'GoToIfBadgeAcquired', got '{name}'"
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
    assert params_str == "varID, valueOrVarID, offset", f"Got params_str: '{params_str}'"
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
    
    assert name == "PrepareMysteryGiftReceivedMsg", f"Expected 'PrepareMysteryGiftReceivedMsg', got '{name}'"
    assert params_str == "destTextBank, destStringID", f"Got params_str: '{params_str}'"
    assert "\\destTextBank" in body, f"Expected body to contain '\\destTextBank'"
    assert "\\destStringID" in body, f"Expected body to contain '\\destStringID'"
    assert "MYSTERY_GIFT_RECEIVED" in body, f"Expected body to contain constant 'MYSTERY_GIFT_RECEIVED'"
    
    print("  [PASS] Mode-expansion macro extraction")


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
    
    print()
    print("[PASS] All tests passed!")


if __name__ == "__main__":
    run_all_tests()
