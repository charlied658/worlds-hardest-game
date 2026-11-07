"""Test a reactive route planner against level 1 of the original SWF."""

import argparse
from collections import deque
from heapq import heappop, heappush
from itertools import count
from math import hypot
from time import sleep

from evolve import ACTION_TICKS, OPENING, evaluate
from reactive import DIRECTIONS, TEST_DELAYS, report
from training_env import HEIGHT, ROOT, WIDTH, RuffleEnv, sync_playwright


DECISION_TICKS = 3
PLAYER_SPEED = 3
ENEMY_LEFT, ENEMY_RIGHT = 155.5, 394.5
ENEMY_SPAN = ENEMY_RIGHT - ENEMY_LEFT
ENEMY_SPEED = ENEMY_SPAN / 36
FLOOR = ((50, 125, 125, 275), (125, 250, 175, 275), (150, 150, 400, 250),
         (375, 125, 425, 150), (425, 125, 500, 275))
GOAL = (450, 137)


def enemy_x(phase, ticks):
    offset = (phase + ENEMY_SPEED * ticks) % (2 * ENEMY_SPAN)
    return ENEMY_LEFT + min(offset, 2 * ENEMY_SPAN - offset)


def predict_enemies(observation, previous):
    enemies = []
    for lane, enemy in enumerate(observation.enemies):
        if enemy is None or previous.enemies[lane] is None:
            continue
        x, y = enemy
        offset = max(0, min(ENEMY_SPAN, x - ENEMY_LEFT))
        old_x = previous.enemies[lane][0]
        elapsed = observation.tick - previous.tick
        phase = min((offset, 2 * ENEMY_SPAN - offset),
                    key=lambda value: abs(enemy_x(value, -elapsed) - old_x))
        enemies.append((phase, y))
    return enemies


def safe(x, y, enemies):
    for corner_x in (x - 8, x + 8):
        for corner_y in (y - 8, y + 8):
            if not any(left <= corner_x <= right and top <= corner_y <= bottom
                       for left, top, right, bottom in FLOOR):
                return False
    return all(hypot(max(0, abs(x - ex) - 8), max(0, abs(y - ey) - 8)) > 7
               for ex, ey in enemies)


def move(position, action, elapsed, forecast):
    x, y = position
    dx, dy = DIRECTIONS[action]
    for tick in range(1, DECISION_TICKS + 1):
        x += dx * PLAYER_SPEED
        y += dy * PLAYER_SPEED
        if not safe(x, y, forecast[elapsed + tick]):
            return None
    return x, y


def route(position, forecast):
    order = count()
    start = (*position, 0)
    pending = [(0, next(order), start)]
    parents = {start: None}
    while pending:
        _, _, state = heappop(pending)
        x, y, elapsed = state
        if x >= GOAL[0]:
            actions = deque()
            while parents[state] is not None:
                state, action = parents[state]
                actions.appendleft(action)
            return actions
        if elapsed + DECISION_TICKS >= len(forecast):
            continue
        for action in range(len(DIRECTIONS)):
            destination = move((x, y), action, elapsed, forecast)
            if destination is None:
                continue
            arrival = elapsed + DECISION_TICKS
            successor = (*destination, arrival)
            if successor in parents:
                continue
            parents[successor] = (state, action)
            remaining = (abs(GOAL[0] - destination[0]) + abs(GOAL[1] - destination[1])) / PLAYER_SPEED
            heappush(pending, (arrival + remaining, next(order), successor))
    return deque()


class Planner:
    def __init__(self):
        self.previous = None
        self.actions = deque()

    def __call__(self, observation, _target):
        previous, self.previous = self.previous, observation
        if observation.player is None or previous is None or not previous.enemies:
            return 0
        enemies = predict_enemies(observation, previous)
        if len(enemies) != 4:
            return 0
        if observation.player[0] >= GOAL[0]:
            return 0
        horizon = DECISION_TICKS if self.actions else 210
        forecast = [tuple((enemy_x(phase, tick), y) for phase, y in enemies)
                    for tick in range(horizon + 1)]
        if self.actions and move(observation.player, self.actions[0], 0, forecast) is None:
            self.actions.clear()
        if not self.actions and horizon == DECISION_TICKS:
            forecast.extend(tuple((enemy_x(phase, tick), y) for phase, y in enemies)
                            for tick in range(horizon + 1, 211))
        if not self.actions:
            self.actions = route(observation.player, forecast)
        return self.actions.popleft() if self.actions else 0


def attempt(env, delay, pace=False):
    prefix = ((0, delay),) if delay else ()
    prefix += tuple((action, ACTION_TICKS) for action, ticks in OPENING[:2]
                    for _ in range(ticks // ACTION_TICKS))
    return evaluate(env, prefix, policy=Planner(), decisions=80,
                    decision_ticks=DECISION_TICKS, pace=pace)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--delay", type=int, nargs="+", default=list(TEST_DELAYS))
    args = parser.parse_args()
    if any(delay < 0 for delay in args.delay):
        parser.error("Delays must be nonnegative.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=not args.watch, timeout=15_000)
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, service_workers="block")
            env = RuffleEnv(page, ROOT / "ruffle-web" / "package", direct_reset=True, track_enemies=True)
            results = [attempt(env, delay, pace=args.watch) for delay in args.delay]
            report("Route planner", args.delay, results)
            if args.watch:
                sleep(3)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
