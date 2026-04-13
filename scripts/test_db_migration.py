#!/usr/bin/env python3
"""Regression tests for legacy-to-v2 migration naming behavior."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from db_migration import get_best_name


def test_get_best_name_keeps_placeholder_decomp_name():
    """A placeholder decomp name should not be replaced by the legacy alias."""
    data = {
        "decomp_name": "ScrCmd_084",
        "name": "DummyTakeGoods",
    }

    assert get_best_name(data) == "ScrCmd_084", get_best_name(data)

    print("  [PASS] Placeholder decomp names stay canonical during migration")


def test_get_best_name_falls_back_to_legacy_name_when_missing():
    """Legacy names should still be used when there is no decomp name at all."""
    data = {
        "decomp_name": "",
        "name": "DummyTakeGoods",
    }

    assert get_best_name(data) == "DummyTakeGoods", get_best_name(data)

    print("  [PASS] Migration still falls back to legacy names when needed")


def run_all_tests():
    """Run all db_migration regression tests."""
    print("Running db_migration tests...\n")
    test_get_best_name_keeps_placeholder_decomp_name()
    test_get_best_name_falls_back_to_legacy_name_when_missing()
    print()
    print("[PASS] All tests passed!")


if __name__ == "__main__":
    run_all_tests()
