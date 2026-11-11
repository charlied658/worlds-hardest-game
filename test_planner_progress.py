"""Regression checks for progress around walls and collect-and-return routes."""

import unittest
from types import SimpleNamespace

from PIL import Image, ImageDraw

from evolve_planner import Progress, TaskDistance, distances_to


def chain_graph(cells):
    graph = {cell: [] for cell in cells}
    for left, right in zip(cells, cells[1:]):
        middle = ((left[0] + right[0]) // 2, (left[1] + right[1]) // 2)
        graph[left].append((0, (middle, right)))
        graph[right].append((0, (middle, left)))
    return graph


class ProgressTests(unittest.TestCase):
    def setUp(self):
        # The wall forces a U-shaped route between spawn and the pellet.
        cells = [(0, 0), (0, 2), (0, 4), (2, 4), (4, 4), (4, 2), (4, 0)]
        graph = chain_graph(cells)
        targets = [{cells[-1]}]
        self.distances = [distances_to(graph, target) for target in [*targets, {cells[0]}]]
        self.model = TaskDistance(self.distances, targets)
        self.scenario = SimpleNamespace(
            initial=SimpleNamespace(player=(100, 100)), pellets=[(112, 100)],
            distances=self.distances, task_distance=self.model,
        )
        self.visible = Image.new("RGB", (550, 400))
        ImageDraw.Draw(self.visible).ellipse((109, 97, 115, 103), fill=(255, 255, 0))
        self.collected = Image.new("RGB", (550, 400))

    def test_route_around_wall_counts_as_progress(self):
        self.assertEqual(self.model.initial, 24)
        # This move increases straight-line distance to the pellet.
        self.assertEqual(self.model.remaining((0, 2), set()), 22)
        progress = Progress(self.scenario)
        progress.update(self.visible, (100, 100), 0)
        progress.update(self.visible, (100, 106), 2)
        self.assertGreater(progress.best, 0)

    def test_return_toward_spawn_keeps_earning_progress(self):
        progress = Progress(self.scenario)
        progress.update(self.collected, (112, 100), 12)
        self.assertFalse(progress.collected)  # One missing observation is insufficient.
        progress.update(self.collected, (112, 100), 14)
        self.assertEqual(progress.collected, {0})
        at_pellet = progress.best
        progress.update(self.collected, (112, 106), 16)
        self.assertGreater(progress.best, at_pellet)
        self.assertEqual(progress.remaining_ticks, 10)

    def test_repeated_movement_does_not_accumulate_reward(self):
        progress = Progress(self.scenario)
        progress.update(self.visible, (100, 100), 0)
        progress.update(self.visible, (100, 106), 2)
        best = progress.best
        for tick in range(4, 20, 4):
            progress.update(self.visible, (100, 100), tick)
            progress.update(self.visible, (100, 106), tick + 2)
        self.assertEqual(progress.best, best)
        progress.update(self.collected, (112, 100), 22)
        progress.update(self.collected, (112, 100), 24)
        progress.update(self.collected, (100, 100), 36)
        self.assertLess(progress.best, 1)  # Only the game can confirm a clear.

    def test_many_pellets_use_a_bounded_route_estimate(self):
        cells = [(index * 2, 0) for index in range(13)]
        graph = chain_graph(cells)
        targets = [{cell} for cell in cells[1:-1]]
        distances = [distances_to(graph, target) for target in [*targets, {cells[-1]}]]
        model = TaskDistance(distances, targets)
        self.assertEqual(model.mode, "spanning-tree estimate")
        self.assertEqual(model.initial, 24)
        self.assertEqual(model.remaining(cells[1], {0}), 22)


if __name__ == "__main__":
    unittest.main()
