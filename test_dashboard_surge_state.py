import unittest

import dashboard


class DashboardSurgeStateTests(unittest.TestCase):
    def setUp(self):
        dashboard.st.session_state.clear()

    def test_staged_values_are_applied_to_widget_keys(self):
        values = {
            "enabled": False,
            "min_market_cap_usd": 123456.0,
            "alert_cooldown_minutes": 11,
        }

        dashboard._stage_surge_form_values(values, notice_message="Applied")
        dashboard._consume_staged_surge_form_values()

        self.assertEqual(dashboard.st.session_state[dashboard._surge_field_key("enabled")], False)
        self.assertEqual(dashboard.st.session_state[dashboard._surge_field_key("min_market_cap_usd")], 123456.0)
        self.assertEqual(dashboard.st.session_state[dashboard._surge_field_key("alert_cooldown_minutes")], 11)
        self.assertNotIn(dashboard._surge_pending_values_key(), dashboard.st.session_state)

    def test_notice_key_is_staged_and_retrievable(self):
        dashboard._stage_surge_form_values({"enabled": True}, notice_message="Saved")
        self.assertEqual(dashboard.st.session_state[dashboard._surge_notice_key()], "Saved")


if __name__ == "__main__":
    unittest.main()
