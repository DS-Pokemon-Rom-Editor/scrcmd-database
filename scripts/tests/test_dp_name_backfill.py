#!/usr/bin/env python3
"""Regression tests for Diamond/Pearl placeholder name backfill."""

import os
import sys
import unittest

SCRIPTS_DIR = os.path.dirname(os.path.dirname(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from sync_from_decomp import backfill_dp_placeholder_names_from_platinum


class DPNameBackfillTests(unittest.TestCase):
    def test_backfill_renames_dp_placeholder_when_platinum_has_real_name(self):
        dp_db = {
            "meta": {"version": "Diamond/Pearl"},
            "commands": {
                "CMD_248": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }
        platinum_db = {
            "meta": {"version": "Platinum"},
            "commands": {
                "StartContestCommSync": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }

        changed = backfill_dp_placeholder_names_from_platinum(dp_db, platinum_db)

        self.assertEqual(changed, 1)
        self.assertIn("StartContestCommSync", dp_db["commands"])
        self.assertNotIn("CMD_248", dp_db["commands"])
        self.assertEqual(dp_db["commands"]["StartContestCommSync"]["id"], 248)
        self.assertEqual(
            dp_db["commands"]["StartContestCommSync"]["legacy_name"], "CMD_248"
        )

    def test_backfill_skips_when_dp_name_is_already_real(self):
        dp_db = {
            "meta": {"version": "Diamond/Pearl"},
            "commands": {
                "StartContestCommSync": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }
        platinum_db = {
            "meta": {"version": "Platinum"},
            "commands": {
                "StartContestCommSync": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }

        changed = backfill_dp_placeholder_names_from_platinum(dp_db, platinum_db)

        self.assertEqual(changed, 0)
        self.assertIn("StartContestCommSync", dp_db["commands"])

    def test_backfill_skips_when_platinum_name_is_also_placeholder(self):
        dp_db = {
            "meta": {"version": "Diamond/Pearl"},
            "commands": {
                "CMD_248": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }
        platinum_db = {
            "meta": {"version": "Platinum"},
            "commands": {
                "CMD_248": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }

        changed = backfill_dp_placeholder_names_from_platinum(dp_db, platinum_db)

        self.assertEqual(changed, 0)
        self.assertIn("CMD_248", dp_db["commands"])

    def test_backfill_skips_when_opcode_is_missing_in_platinum(self):
        dp_db = {
            "meta": {"version": "Diamond/Pearl"},
            "commands": {
                "CMD_248": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }
        platinum_db = {
            "meta": {"version": "Platinum"},
            "commands": {},
        }

        changed = backfill_dp_placeholder_names_from_platinum(dp_db, platinum_db)

        self.assertEqual(changed, 0)
        self.assertIn("CMD_248", dp_db["commands"])

    def test_backfill_only_updates_script_commands(self):
        dp_db = {
            "meta": {"version": "Diamond/Pearl"},
            "commands": {
                "CMD_248": {
                    "type": "movement",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                }
            },
        }
        platinum_db = {
            "meta": {"version": "Platinum"},
            "commands": {
                "StartContestCommSync": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }

        changed = backfill_dp_placeholder_names_from_platinum(dp_db, platinum_db)

        self.assertEqual(changed, 0)
        self.assertIn("CMD_248", dp_db["commands"])

    def test_backfill_displaces_existing_conflicting_key(self):
        dp_db = {
            "meta": {"version": "Diamond/Pearl"},
            "commands": {
                "CMD_248": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                },
                "StartContestCommSync": {
                    "type": "macro",
                    "id": 999,
                    "description": "wrapper macro",
                    "params": [],
                },
            },
        }
        platinum_db = {
            "meta": {"version": "Platinum"},
            "commands": {
                "StartContestCommSync": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }

        changed = backfill_dp_placeholder_names_from_platinum(dp_db, platinum_db)

        self.assertEqual(changed, 1)
        self.assertIn("StartContestCommSync", dp_db["commands"])
        self.assertEqual(
            dp_db["commands"]["StartContestCommSync"]["type"], "script_cmd"
        )

        displaced_names = [
            name
            for name, command in dp_db["commands"].items()
            if name.startswith("__tmp__") and command.get("type") == "macro"
        ]
        self.assertEqual(len(displaced_names), 1)

    def test_backfill_preserves_command_order_position(self):
        dp_db = {
            "meta": {"version": "Diamond/Pearl"},
            "commands": {
                "Before": {
                    "type": "script_cmd",
                    "id": 247,
                    "legacy_name": "Before",
                    "description": "nan",
                    "params": [],
                },
                "CMD_248": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                },
                "After": {
                    "type": "script_cmd",
                    "id": 249,
                    "legacy_name": "After",
                    "description": "nan",
                    "params": [],
                },
            },
        }
        platinum_db = {
            "meta": {"version": "Platinum"},
            "commands": {
                "StartContestCommSync": {
                    "type": "script_cmd",
                    "id": 248,
                    "legacy_name": "CMD_248",
                    "description": "nan",
                    "params": [{"name": "arg_0", "type": "u16"}],
                }
            },
        }

        changed = backfill_dp_placeholder_names_from_platinum(dp_db, platinum_db)

        self.assertEqual(changed, 1)
        self.assertEqual(
            list(dp_db["commands"].keys()),
            ["Before", "StartContestCommSync", "After"],
        )


if __name__ == "__main__":
    unittest.main()
