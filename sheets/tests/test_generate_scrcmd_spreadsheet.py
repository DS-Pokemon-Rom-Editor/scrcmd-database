import sys
import unittest
from pathlib import Path

SHEETS_DIR = Path(__file__).resolve().parents[1]
if str(SHEETS_DIR) not in sys.path:
    sys.path.insert(0, str(SHEETS_DIR))

import generate_scrcmd_spreadsheet as gss  # noqa: E402


class SpreadsheetFormattingTests(unittest.TestCase):
    def test_prettify_param_name_handles_snake_case(self):
        self.assertEqual(gss.prettify_param_name("trainer_id", "u16"), "Trainer ID")

    def test_prettify_param_name_handles_camel_case_var_id(self):
        self.assertEqual(
            gss.prettify_param_name("countdownVarID", "var"), "Countdown Variable"
        )

    def test_prettify_param_name_handles_numbered_var_ids(self):
        self.assertEqual(gss.prettify_param_name("varID1", "var"), "Variable 1")
        self.assertEqual(gss.prettify_param_name("varID2", "var"), "Variable 2")

    def test_prettify_param_type_capitalizes_var(self):
        self.assertEqual(gss.prettify_param_type("var"), "Var")
        self.assertEqual(gss.prettify_param_type("u16"), "u16")

    def test_format_params_uses_semicolons_and_prettified_names(self):
        cmd = {
            "params": [
                {"type": "u16", "name": "time"},
                {"type": "var", "name": "countdownVarID"},
            ]
        }

        formatted, params = gss.format_params(cmd)

        self.assertEqual(formatted, "u16: Time; Var: Countdown Variable")
        self.assertEqual(
            params,
            [
                {"type": "u16", "name": "Time"},
                {"type": "var", "name": "Countdown Variable"},
            ],
        )

    def test_format_params_variants_use_semicolons(self):
        cmd = {
            "variants": [
                {
                    "params": [
                        {"type": "u8", "name": "mode", "const": "0"},
                        {"type": "var", "name": "result"},
                    ],
                    "desc": "Stores answer in Variable",
                }
            ]
        }

        formatted, params = gss.format_params(cmd)

        self.assertEqual(
            formatted,
            "[0] u8: Mode=0; Var: Result - Stores answer in Variable",
        )
        self.assertEqual(params, [])

    def test_extract_highlight_terms_prefers_full_phrase_and_individual_words(self):
        params = [
            {"type": "u16", "name": "Time"},
            {"type": "var", "name": "Countdown Variable"},
        ]

        self.assertEqual(
            gss.extract_highlight_terms(params),
            ["Time", "Countdown Variable", "Countdown", "Variable"],
        )

    def test_build_description_segments_highlights_prettified_param_names(self):
        params = [
            {"type": "u16", "name": "Time"},
            {"type": "var", "name": "Countdown Variable"},
        ]
        description = (
            "Stop script execution for Time frames, storing the remaining time "
            "in the Countdown Variable"
        )

        segments = gss.build_description_segments(description, params)

        self.assertEqual(
            segments,
            [
                ("Stop script execution for ", False),
                ("Time", True),
                (" frames, storing the remaining time in the ", False),
                ("Countdown Variable", True),
            ],
        )

    def test_build_description_segments_handles_trailing_punctuation(self):
        params = [{"type": "u16", "name": "Trainer ID"}]
        description = "Checks if Trainer ID, has been beaten."

        segments = gss.build_description_segments(description, params)

        self.assertEqual(
            segments,
            [
                ("Checks if ", False),
                ("Trainer ID,", True),
                (" has been beaten.", False),
            ],
        )

    def test_build_description_segments_returns_plain_segment_when_no_match(self):
        params = [{"type": "u16", "name": "Trainer ID"}]
        description = "End script execution"

        self.assertEqual(
            gss.build_description_segments(description, params),
            [("End script execution", False)],
        )

    def test_build_description_segments_falls_back_to_case_insensitive_when_needed(
        self,
    ):
        params = [
            {"type": "var", "name": "Var Flag"},
            {"type": "var", "name": "Var Dest"},
        ]
        description = "Check event flag referenced in var, store result in other var"

        segments = gss.build_description_segments(description, params)

        self.assertEqual(
            segments,
            [
                ("Check event ", False),
                ("flag", True),
                (" referenced in ", False),
                ("var,", True),
                (" store result in other ", False),
                ("var", True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
