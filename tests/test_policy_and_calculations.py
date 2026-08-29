from __future__ import annotations

import unittest
from decimal import Decimal

from assistant.scotty_business.calculations import preliminary_analysis
from assistant.scotty_business.config import ConfigError, RuntimeConfig
from assistant.scotty_business.policy import (
    ADDON_CAP_RESPONSE,
    CODING_REFUSAL,
    FIXED_WIZARD_COMMAND,
    Principal,
    Role,
    authorize_source,
    can_approve,
    enforce_addon_cap,
)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maintainer = Principal("100", "200", "300", Role.MAINTAINER)
        self.operator = Principal("100", "201", "301", Role.MAIN_OPERATOR)
        self.employee = Principal("100", "202", "302", Role.EMPLOYEE)
        self.principals = (self.maintainer, self.operator, self.employee)

    def test_exact_principal_tuple_is_required(self) -> None:
        self.assertEqual(
            authorize_source(self.principals, "100", "201", "301", None),
            self.operator,
        )
        mixed = [("100", "201", "302"), ("100", "202", "301"), ("999", "201", "301")]
        for guild_id, channel_id, user_id in mixed:
            with self.subTest(tuple=(guild_id, channel_id, user_id)):
                self.assertIsNone(
                    authorize_source(self.principals, guild_id, channel_id, user_id, None)
                )

    def test_threads_require_the_configured_parent_and_tuple_channel(self) -> None:
        self.assertEqual(
            authorize_source(self.principals, "100", "900", "301", "201"),
            self.operator,
        )
        self.assertIsNone(authorize_source(self.principals, "100", "900", "301", "202"))
        self.assertIsNone(authorize_source(self.principals, "100", "900", "302", "201"))

    def test_role_approval_policy_is_fail_closed(self) -> None:
        self.assertTrue(can_approve(self.maintainer, "trello_write"))
        self.assertTrue(can_approve(self.operator, "ghl_sms"))
        self.assertTrue(can_approve(self.operator, "discord_announcement"))
        self.assertFalse(can_approve(self.employee, "trello_write"))
        self.assertFalse(can_approve(self.operator, "permanent_delete"))
        self.assertFalse(can_approve(self.maintainer, "unknown"))

    def test_addon_cap_has_exact_response(self) -> None:
        self.assertEqual(
            enforce_addon_cap(["discord", "trello", "ghl", "rentcast"], "fifth"),
            ["discord", "trello", "ghl", "rentcast", "fifth"],
        )
        with self.assertRaisesRegex(ValueError, ADDON_CAP_RESPONSE):
            enforce_addon_cap(["a", "b", "c", "d", "e", "f"], "g")

    def test_fixed_public_text_is_exact(self) -> None:
        self.assertEqual(FIXED_WIZARD_COMMAND, "Scotty, send Trent the setup wizard.")
        self.assertEqual(
            CODING_REFUSAL,
            "I don’t build code, extensions, or integrations. Please contact Marco for that work.",
        )


class ConfigTests(unittest.TestCase):
    def test_synthetic_config_builds_three_unique_principals(self) -> None:
        config = RuntimeConfig.from_mapping(
            {
                "version": 1,
                "addons": ["discord", "trello", "ghl", "rentcast"],
                "principals": {
                    "maintainer": {"guild_id": "100", "channel_id": "200", "user_id": "300"},
                    "main_operator": {"guild_id": "100", "channel_id": "201", "user_id": "301"},
                    "employee": {"guild_id": "100", "channel_id": "202", "user_id": "302"},
                },
                "discord": {"announcement_channel_ids": ["210"]},
                "trello": {
                    "board_id": "board-1",
                    "list_ids": ["list-1", "list-2"],
                    "label_ids": ["label-1"],
                    "custom_field_ids": ["field-1"],
                },
                "ghl": {"location_id": "location-1"},
                "rentcast": {
                    "endpoints": ["/v1/properties", "/v1/avm/value", "/v1/avm/rent/long-term"]
                },
            }
        )
        self.assertEqual(len(config.principals), 3)
        self.assertEqual(config.addons, ("discord", "trello", "ghl", "rentcast"))

    def test_more_than_six_addons_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            RuntimeConfig.from_mapping(
                {"version": 1, "addons": ["a", "b", "c", "d", "e", "f", "g"]}
            )


class CalculationTests(unittest.TestCase):
    def test_preliminary_analysis_uses_decimal_and_discloses_verification(self) -> None:
        result = preliminary_analysis(
            asking_price=Decimal("150000.00"),
            estimated_value=Decimal("210000.00"),
            estimated_monthly_rent=Decimal("1800.00"),
        )
        self.assertEqual(result["value_gap"], "60000.00")
        self.assertEqual(result["gross_rent_yield_percent"], "10.29")
        self.assertIn("preliminary", result["disclaimer"].lower())
        self.assertIn("qualified professional", result["disclaimer"].lower())

    def test_calculations_reject_float_and_nonpositive_value(self) -> None:
        with self.assertRaises(TypeError):
            preliminary_analysis(150000.0, Decimal("1"), Decimal("1"))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            preliminary_analysis(Decimal("1"), Decimal("0"), Decimal("1"))


if __name__ == "__main__":
    unittest.main()
