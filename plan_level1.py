"""Test a reactive route planner against levels 1 and 2 of the original SWF."""

import argparse
from collections import deque
from heapq import heappop, heappush
from itertools import count
from math import hypot
from time import sleep

from evolve import ACTION_TICKS, OPENING, evaluate
from reactive import DIRECTIONS, TEST_DELAYS, report
from training_env import ROOT, RuffleEnv, game_page, sync_playwright


DECISION_TICKS = 3
PLAYER_SPEED = 3
ENEMY_LEFT, ENEMY_RIGHT = 155.5, 394.5
ENEMY_SPAN = ENEMY_RIGHT - ENEMY_LEFT
ENEMY_SPEED = ENEMY_SPAN / 36
FLOOR = ((50, 125, 125, 275), (125, 250, 175, 275), (150, 150, 400, 250),
         (375, 125, 425, 150), (425, 125, 500, 275))
GOAL = (450, 137)
LEVEL_TWO_FLOOR = ((50, 175, 125, 225), (125, 125, 425, 275), (425, 175, 500, 225))
LEVEL_TWO_GOAL = (450, 200)
VERTICAL_TOP, VERTICAL_SPAN = 137.5, 125
VERTICAL_SPEED = VERTICAL_SPAN / 33


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


def safe(x, y, enemies, floor):
    for corner_x in (x - 8, x + 8):
        for corner_y in (y - 8, y + 8):
            if not any(left <= corner_x <= right and top <= corner_y <= bottom
                       for left, top, right, bottom in floor):
                return False
    return all(hypot(max(0, abs(x - ex) - 8), max(0, abs(y - ey) - 8)) > 7
               for ex, ey in enemies if abs(x - ex) <= 15 and abs(y - ey) <= 15)


def move(position, action, elapsed, forecast, floor=FLOOR):
    x, y = position
    dx, dy = DIRECTIONS[action]
    for tick in range(1, DECISION_TICKS + 1):
        x += dx * PLAYER_SPEED
        y += dy * PLAYER_SPEED
        if not safe(x, y, forecast[elapsed + tick], floor):
            return None
    return x, y


def route(position, forecast, *, floor=FLOOR, goal=GOAL, pellet=None):
    order = count()
    start = (*position, 0, pellet is None)
    pending = [(0, next(order), start)]
    parents = {start: None}
    while pending:
        _, _, state = heappop(pending)
        x, y, elapsed, collected = state
        if x >= goal[0] and collected:
            actions = deque()
            while parents[state] is not None:
                state, action = parents[state]
                actions.appendleft(action)
            return actions
        if elapsed + DECISION_TICKS >= len(forecast):
            continue
        for action in range(len(DIRECTIONS)):
            destination = move((x, y), action, elapsed, forecast, floor)
            if destination is None:
                continue
            arrival = elapsed + DECISION_TICKS
            has_pellet = collected or hypot(destination[0] - pellet[0], destination[1] - pellet[1]) <= 8
            successor = (*destination, arrival, has_pellet)
            if successor in parents:
                continue
            parents[successor] = (state, action)
            target = goal if has_pellet else pellet
            remaining = abs(target[0] - destination[0]) + abs(target[1] - destination[1])
            if not has_pellet:
                remaining += abs(goal[0] - pellet[0]) + abs(goal[1] - pellet[1])
            remaining /= PLAYER_SPEED
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


def enemy_y(phase, ticks):
    offset = (phase + VERTICAL_SPEED * ticks) % (2 * VERTICAL_SPAN)
    return VERTICAL_TOP + min(offset, 2 * VERTICAL_SPAN - offset)


class LevelTwoPlanner:
    def __init__(self):
        self.previous = None
        self.actions = deque()
        self.collected = False

    def __call__(self, observation, _target):
        previous, self.previous = self.previous, observation
        if (observation.player is None or previous is None or len(observation.enemies) != 12
                or len(previous.enemies) != 12):
            return 0
        if observation.pellet is None:
            self.collected = True
        if self.collected and observation.player[0] >= LEVEL_TWO_GOAL[0]:
            return 0
        enemies = []
        elapsed = observation.tick - previous.tick
        for enemy, prior in zip(observation.enemies, previous.enemies):
            if enemy is None or prior is None:
                return 0
            x, y = enemy
            offset = max(0, min(VERTICAL_SPAN, y - VERTICAL_TOP))
            phase = min((offset, 2 * VERTICAL_SPAN - offset),
                        key=lambda value: abs(enemy_y(value, -elapsed) - prior[1]))
            enemies.append((x, phase))
        forecast = [tuple((x, enemy_y(phase, tick)) for x, phase in enemies)
                    for tick in range(DECISION_TICKS + 1)]
        if self.actions and move(observation.player, self.actions[0], 0, forecast, LEVEL_TWO_FLOOR) is None:
            self.actions.clear()
        if not self.actions:
            forecast.extend(tuple((x, enemy_y(phase, tick)) for x, phase in enemies)
                            for tick in range(DECISION_TICKS + 1, 241))
            self.actions = route(observation.player, forecast, floor=LEVEL_TWO_FLOOR,
                                 goal=LEVEL_TWO_GOAL, pellet=observation.pellet)
        return self.actions.popleft() if self.actions else 0


def attempt(env, delay, pace=False):
    prefix = ((0, delay),) if delay else ()
    prefix += tuple((action, ACTION_TICKS) for action, ticks in OPENING[:2]
                    for _ in range(ticks // ACTION_TICKS))
    return evaluate(env, prefix, policy=Planner(), decisions=80,
                    decision_ticks=DECISION_TICKS, pace=pace)


def attempt_level_two(env, delay, pace=False):
    planner = LevelTwoPlanner()
    prefix = ((0, delay),) if delay else ()
    result = evaluate(env, prefix, policy=planner, decisions=90,
                      decision_ticks=DECISION_TICKS, pace=pace)
    return result, planner.collected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--level", type=int, choices=(1, 2), default=1)
    parser.add_argument("--delay", type=int, nargs="+", default=list(TEST_DELAYS))
    args = parser.parse_args()
    if any(delay < 0 for delay in args.delay):
        parser.error("Delays must be nonnegative.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=not args.watch, timeout=15_000)
        try:
            page = game_page(browser, watch=args.watch)
            env = RuffleEnv(page, ROOT / "ruffle-web" / "package", direct_reset=True,
                            track_enemies=True, level=args.level, fit_window=args.watch)
            if args.level == 1:
                results = [attempt(env, delay, pace=args.watch) for delay in args.delay]
                report("Route planner", args.delay, results)
            else:
                for delay in args.delay:
                    result, collected = attempt_level_two(env, delay, pace=args.watch)
                    status = "cleared" if result.cleared else "died" if result.died else "time limit"
                    print(f"Level 2 | Delay {delay} ticks | Pellet {'collected' if collected else 'present'}"
                          f" | {status} at tick {result.ticks}", flush=True)
            if args.watch:
                sleep(3)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
