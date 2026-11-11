"""Regression tests for safe waiting and route reconstruction."""

import unittest

from PIL import Image, ImageDraw

from baseline_solver import compress, joint_pickup_search, neighbors, search
from training_env import is_level_tip


class SafeIntervalTests(unittest.TestCase):
    def setUp(self):
        self.cells = [(x, 0) for x in range(5)]
        self.edges = neighbors(self.cells)

    def test_waits_until_the_gate_is_safe(self):
        intervals = [[(0, 20)], [(5, 20)], [(0, 20)], [(0, 20)], [(0, 20)]]
        plans, reason = search(0, 0, {4}, self.edges, intervals)
        self.assertIsNone(reason)
        self.assertEqual(plans[0][1], 8)
        self.assertEqual(plans[0][2], [(0, 4), (4, 1), (4, 1), (4, 1), (4, 1)])

    def test_cannot_wait_through_a_collision(self):
        intervals = [[(0, 2)], [(5, 20)], [(0, 20)], [(0, 20)], [(0, 20)]]
        plans, reason = search(0, 0, {4}, self.edges, intervals)
        self.assertFalse(plans)
        self.assertEqual(reason, "safe interval search exhausted")

    def test_considers_later_safe_intervals(self):
        intervals = [[(0, 20)], [(1, 1), (5, 20)], [(6, 20)], [(0, 20)], [(0, 20)]]
        plans, _ = search(0, 0, {4}, self.edges, intervals)
        self.assertTrue(plans)
        self.assertEqual(plans[0][1], 8)

    def test_compression_preserves_input_duration(self):
        self.assertEqual(compress([(0, 4), (4, 1), (4, 1), (0, 2)]),
                         [[0, 4], [4, 2], [0, 2]])

    def test_joint_pickups_allow_returning_to_the_start_exit(self):
        intervals = [[(0, 20)]] * 5
        moves, reason = joint_pickup_search(2, [{0}, {4}], {2}, self.edges, intervals)
        self.assertIsNone(reason)
        self.assertEqual(sum(duration for _, duration in moves), 8)
        position, visited = 2, {2}
        for action, duration in moves:
            position += {3: -1, 4: 1, 0: 0}[action] * duration
            visited.add(position)
        self.assertTrue({0, 4} <= visited)
        self.assertEqual(position, 2)

    def test_red_level_tip_has_a_non_red_gradient(self):
        image = Image.new("RGB", (550, 400), (250, 210, 210))
        ImageDraw.Draw(image).rectangle((0, 190, 549, 220), fill=(252, 235, 235))
        ImageDraw.Draw(image).rectangle((260, 305, 290, 320), fill=(255, 0, 0))
        self.assertTrue(is_level_tip(image))
        ImageDraw.Draw(image).rectangle((0, 190, 549, 220), fill=(250, 210, 210))
        self.assertFalse(is_level_tip(image))


if __name__ == "__main__":
    unittest.main()
