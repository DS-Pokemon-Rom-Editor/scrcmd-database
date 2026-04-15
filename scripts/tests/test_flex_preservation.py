#!/usr/bin/env python3
"""Regression tests for preserving Flex parameter types in migration and sync."""

import os
import sys
import unittest

SCRIPTS_DIR = os.path.dirname(os.path.dirname(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from db_migration import build_migrated_output, parse_param_name_from_value
from sync_from_decomp import build_sync_param_list


class FlexPreservationTests(unittest.TestCase):
    def test_parse_param_name_from_value_preserves_flex_type(self):
        name, param_type = parse_param_name_from_value("Flex: Trainer ID")

        self.assertEqual(name, "trainer_id")
        self.assertEqual(param_type, "flex")

    def test_build_migrated_output_preserves_flex_from_legacy_param_values(self):
        old_data = {
            "scrcmd": {
                "0x0023": {
                    "name": "SetTrainerFlag",
                    "decomp_name": "SetTrainerFlag",
                    "parameters": [2],
                    "parameter_types": ["Trainer"],
                    "parameter_values": ["Flex: Trainer ID"],
                    "description": "Sets the flag of Trainer ID",
                }
            },
            "movements": {},
            "sounds": {},
        }

        migrated = build_migrated_output("platinum_scrcmd_database.json", old_data)
        params = migrated["commands"]["SetTrainerFlag"]["params"]

        self.assertEqual(
            params,
            [
                {
                    "name": "trainer_id",
                    "type": "flex",
                }
            ],
        )

    def test_build_migrated_output_preserves_flex_in_multi_param_commands(self):
        old_data = {
            "scrcmd": {
                "0x0026": {
                    "name": "IncrementVar",
                    "decomp_name": "AddVar",
                    "parameters": [2, 2],
                    "parameter_types": ["Variable", "Flex"],
                    "parameter_values": ["Var: Variable", "Flex: Operand"],
                    "description": "Stores the operation Variable + Operand in Variable",
                }
            },
            "movements": {},
            "sounds": {},
        }

        migrated = build_migrated_output("platinum_scrcmd_database.json", old_data)
        params = migrated["commands"]["AddVar"]["params"]

        self.assertEqual(
            params,
            [
                {"name": "variable", "type": "var"},
                {"name": "operand", "type": "flex"},
            ],
        )

    def test_build_sync_param_list_keeps_existing_flex_type_by_index(self):
        raw_params = [
            {"name": "trainer_id", "type": "u16"},
            {"name": "value", "type": "u16"},
        ]
        existing_params = [
            {"name": "trainer_id", "type": "flex"},
            {"name": "value", "type": "u16"},
        ]

        synced = build_sync_param_list(raw_params, existing_params)

        self.assertEqual(
            synced,
            [
                {"name": "trainer_id", "type": "flex"},
                {"name": "value", "type": "u16"},
            ],
        )

    def test_build_sync_param_list_keeps_existing_flex_type_for_string_params(self):
        raw_params = ["trainer_id", "value"]
        existing_params = [
            {"name": "trainer_id", "type": "flex"},
            {"name": "value", "type": "u16"},
        ]

        synced = build_sync_param_list(raw_params, existing_params)

        self.assertEqual(
            synced,
            [
                {"name": "trainer_id", "type": "flex"},
                {"name": "value", "type": "u16"},
            ],
        )

    def test_build_sync_param_list_does_not_invent_flex_without_existing_override(self):
        raw_params = [
            {"name": "trainer_id", "type": "u16"},
        ]

        synced = build_sync_param_list(raw_params)

        self.assertEqual(
            synced,
            [
                {"name": "trainer_id", "type": "u16"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
