import unittest

from event_policy import automation_enabled


class AutomationEnabledTests(unittest.TestCase):
    def test_master_switch_false_disables_all_named_switches(self):
        config = {"automation": {
            "enabled": False,
            "welcome": True,
            "ai_welcome": True,
            "auto_poke": True,
            "like_back": True,
            "file_notice": True,
            "ai_admin_intent": True,
        }}
        for name in ("welcome", "ai_welcome", "auto_poke",
                     "like_back", "file_notice", "ai_admin_intent"):
            self.assertFalse(automation_enabled(config, name), name)

    def test_master_switch_true_keeps_per_name_logic(self):
        config = {"automation": {"enabled": True, "welcome": False, "auto_poke": True}}
        self.assertFalse(automation_enabled(config, "welcome"))
        self.assertTrue(automation_enabled(config, "auto_poke"))
        self.assertTrue(automation_enabled(config, "missing_uses_default"))
        self.assertFalse(automation_enabled(config, "missing_uses_default", default=False))

    def test_master_switch_missing_keeps_per_name_logic(self):
        config = {"automation": {"welcome": False}}
        self.assertFalse(automation_enabled(config, "welcome"))
        self.assertTrue(automation_enabled(config, "auto_poke"))
        self.assertTrue(automation_enabled({}, "welcome"))

    def test_master_switch_false_overrides_explicit_default(self):
        config = {"automation": {"enabled": False}}
        self.assertFalse(automation_enabled(config, "ai_admin_intent", default=False))
        self.assertFalse(automation_enabled(config, "welcome", default=True))


if __name__ == "__main__":
    unittest.main()
