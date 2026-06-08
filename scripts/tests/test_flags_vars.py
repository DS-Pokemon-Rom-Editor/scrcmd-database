#!/usr/bin/env python3
"""Tests for flags/vars decomp parsing."""

import os
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.dirname(__file__))
REPO_ROOT = Path(__file__).resolve().parents[2]
METANG_SCRIPT = REPO_ROOT / "metang" / "metang.py"
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from flags_vars import (
    merge_symbol_section,
    parse_c_header_symbols,
    parse_hgss_flags_header,
    parse_hgss_vars_header,
    parse_metang_enum,
    parse_platinum_vars_flags,
)

POKEPLATINUM_VARS_FLAGS = (
    Path(__file__).resolve().parents[3] / "pokeplatinum" / "generated" / "vars_flags.txt"
)
POKEHEARTGOLD_FLAGS = (
    Path(__file__).resolve().parents[3]
    / "pokeheartgold"
    / "include"
    / "constants"
    / "flags.h"
)
POKEHEARTGOLD_VARS = (
    Path(__file__).resolve().parents[3]
    / "pokeheartgold"
    / "include"
    / "constants"
    / "vars.h"
)


@unittest.skipUnless(METANG_SCRIPT.is_file(), "metang submodule not initialized")
class FlagsVarsTests(unittest.TestCase):
    def test_metang_enum_villa_furniture_alias(self):
        content = "\n".join(
            [
                "FLAG_VILLA_FURNITURE_START",
                "FLAG_VILLA_FURNITURE_TABLE = FLAG_VILLA_FURNITURE_START",
                "FLAG_VILLA_FURNITURE_BIG_SOFA",
            ]
        )
        self.assertEqual(
            parse_metang_enum(content),
            [
                ("FLAG_VILLA_FURNITURE_START", 0),
                ("FLAG_VILLA_FURNITURE_TABLE", 0),
                ("FLAG_VILLA_FURNITURE_BIG_SOFA", 1),
            ],
        )

    def test_platinum_section_keeps_both_villa_aliases(self):
        content = "\n".join(
            [
                "FLAG_VILLA_FURNITURE_START",
                "FLAG_VILLA_FURNITURE_TABLE = FLAG_VILLA_FURNITURE_START",
                "FLAG_VILLA_FURNITURE_BIG_SOFA",
            ]
        )
        flags, _vars = parse_platinum_vars_flags(content)
        self.assertEqual(flags["FLAG_VILLA_FURNITURE_START"]["id"], 0)
        self.assertEqual(flags["FLAG_VILLA_FURNITURE_TABLE"]["id"], 0)
        self.assertEqual(flags["FLAG_VILLA_FURNITURE_BIG_SOFA"]["id"], 1)

    def test_sections_sorted_by_id(self):
        flags, _vars = parse_platinum_vars_flags(
            "FLAG_Z\nFLAG_A\nFLAG_B = FLAG_A\n"
        )
        self.assertEqual(list(flags.keys()), ["FLAG_Z", "FLAG_A", "FLAG_B"])

    def test_hgss_vars_end_resolves_with_num_vars(self):
        values = parse_c_header_symbols(
            "\n".join(
                [
                    "#define VAR_BASE 0x4000",
                    "#define NUM_VARS 0x170",
                    "#define VARS_START VAR_BASE",
                    "#define VARS_END (VAR_BASE + NUM_VARS - 1)",
                ]
            )
        )
        self.assertEqual(values["VARS_START"], 0x4000)
        self.assertEqual(values["VARS_END"], 0x4000 + 0x170 - 1)

    @unittest.skipUnless(POKEHEARTGOLD_VARS.exists(), "pokeheartgold not available")
    def test_hgss_vars_header_includes_range_meta(self):
        section = parse_hgss_vars_header(POKEHEARTGOLD_VARS.read_text(encoding="utf-8"))
        self.assertIn("VARS_START", section)
        self.assertIn("VARS_END", section)
        self.assertIn("NUM_VARS", section)
        self.assertEqual(section["VARS_START"]["id"], 0x4000)
        names = list(section.keys())
        self.assertEqual(names, sorted(names, key=lambda n: (section[n]["id"], n)))

    @unittest.skipUnless(POKEHEARTGOLD_FLAGS.exists(), "pokeheartgold not available")
    def test_hgss_flags_header_excludes_action_helpers(self):
        section = parse_hgss_flags_header(POKEHEARTGOLD_FLAGS.read_text(encoding="utf-8"))
        self.assertNotIn("FLAG_ACTION_CLEAR", section)
        self.assertIn("NUM_MAPTEMP_FLAGS", section)
        self.assertIn("MAPTEMP_FLAG_BASE", section)

    @unittest.skipUnless(POKEPLATINUM_VARS_FLAGS.exists(), "pokeplatinum not available")
    def test_platinum_vars_flags_from_decomp(self):
        flags, vars_ = parse_platinum_vars_flags(
            POKEPLATINUM_VARS_FLAGS.read_text(encoding="utf-8")
        )
        self.assertEqual(list(flags.keys())[0], "FLAG_UNK_0x0000")
        self.assertEqual(flags["FLAG_UNK_0x0000"]["id"], 0)
        self.assertIn("VARS_START", vars_)


class MergeSymbolSectionTests(unittest.TestCase):
    def test_rename_replaces_unk_entry(self):
        existing = {"VAR_UNK_0x4080": {"id": 16512}}
        new = {"VAR_ENTERED_WIFI_PLAZA": {"id": 16512}}
        result = merge_symbol_section(existing, new)
        self.assertIn("VAR_ENTERED_WIFI_PLAZA", result)
        self.assertNotIn("VAR_UNK_0x4080", result)

    def test_rename_preserves_metadata(self):
        existing = {"VAR_UNK_0x4080": {"id": 16512, "description": "some note"}}
        new = {"VAR_ENTERED_WIFI_PLAZA": {"id": 16512}}
        result = merge_symbol_section(existing, new)
        self.assertEqual(result["VAR_ENTERED_WIFI_PLAZA"]["description"], "some note")

    def test_rename_does_not_overwrite_existing_metadata(self):
        existing = {"VAR_UNK_0x4080": {"id": 16512, "description": "old note"}}
        new = {"VAR_ENTERED_WIFI_PLAZA": {"id": 16512, "description": "new note"}}
        result = merge_symbol_section(existing, new)
        self.assertEqual(result["VAR_ENTERED_WIFI_PLAZA"]["description"], "new note")

    def test_db_only_entry_preserved(self):
        existing = {"VAR_CUSTOM": {"id": 99999}}
        new = {"VAR_OTHER": {"id": 16512}}
        result = merge_symbol_section(existing, new)
        self.assertIn("VAR_CUSTOM", result)
        self.assertIn("VAR_OTHER", result)

    def test_unchanged_name_preserves_metadata(self):
        existing = {"VAR_KEPT": {"id": 100, "description": "kept note"}}
        new = {"VAR_KEPT": {"id": 100}}
        result = merge_symbol_section(existing, new)
        self.assertEqual(result["VAR_KEPT"]["description"], "kept note")


if __name__ == "__main__":
    unittest.main()
