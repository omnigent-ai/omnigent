import unittest

from omnigent.runner.skill_load_guard import SkillLoadGuard


class SkillToolDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = SkillLoadGuard()

    def test_load_skill_is_idempotent_within_one_turn(self) -> None:
        self.assertFalse(self.guard.is_loaded("conv_1", "turn_1", "research"))

        self.guard.record("conv_1", "turn_1", "research")

        self.assertTrue(self.guard.is_loaded("conv_1", "turn_1", "research"))

    def test_load_skill_can_be_reused_on_a_later_turn(self) -> None:
        self.guard.record("conv_1", "turn_1", "research")

        self.assertFalse(self.guard.is_loaded("conv_1", "turn_2", "research"))

    def test_old_turns_are_evicted(self) -> None:
        guard = SkillLoadGuard(max_scopes=2)
        guard.record("conv_1", "turn_1", "research")
        guard.record("conv_1", "turn_2", "research")
        guard.record("conv_1", "turn_3", "research")

        self.assertFalse(guard.is_loaded("conv_1", "turn_1", "research"))
        self.assertTrue(guard.is_loaded("conv_1", "turn_2", "research"))


if __name__ == "__main__":
    unittest.main()
