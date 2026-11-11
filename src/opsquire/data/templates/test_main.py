import unittest

import main


class Decisions(unittest.TestCase):
    def test_a_target_already_as_intended_needs_no_change(self) -> None:
        self.assertEqual(main.plan_for({"target": "web1"}), [])


if __name__ == "__main__":
    unittest.main()
