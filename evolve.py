"""Evolve timed arrow-key sequences on level 1 of the original game."""

import argparse
import json
import random
from itertools import chain, repeat
from dataclasses import dataclass
from math import dist
from pathlib import Path
from time import perf_counter, sleep

from training_env import FPS, HEIGHT, KEYS, ROOT, WIDTH, RuffleEnv, sync_playwright, watch_replay


ACTIONS = ((), *((key,) for key in KEYS))
ACTION_TICKS = 6
OPENING = ((2, 24), (4, 24), (1, 12))
MOVE_DURATIONS = (6, 12, 18, 24, 36)
ENTRY_DELAYS = tuple(range(0, 73, ACTION_TICKS))
# Stage coordinates along the corridor, from spawn to the exit zone.
WAYPOINTS = ((87, 200), (87, 265), (162, 265), (162, 235),
             (225, 222), (290, 205), (350, 182), (388, 162),
             (388, 136), (451, 136))
WAYPOINT_RADII = (0, 14, 14, 14, 24, 24, 24, 18, 12, 14)


@dataclass(frozen=True)
class Result:
    progress: float
    replay: tuple
    died: bool
    cleared: bool

    @property
    def ticks(self):
        return sum(ticks for _, ticks in self.replay)

    @property
    def rank(self):
        if self.cleared:
            return True, -self.ticks, True, 0
        return False, self.progress, not self.died, self.ticks


def evaluate(env, moves, *, policy=None, decisions=0, decision_ticks=ACTION_TICKS, pace=False):
    initial = env.reset()
    observation = initial
    start = perf_counter()
    waypoint = 1
    best_progress = 0.0
    replay = []
    for action, ticks in chain(moves, repeat((None, decision_ticks), decisions)):
        if action is None:
            action = policy(observation, WAYPOINTS[min(waypoint, len(WAYPOINTS) - 1)])
        if pace:
            # Draw each tick in watch mode, but keep the same decision boundaries.
            for _ in range(ticks):
                env.step(ACTIONS[action], observe=False)
                sleep(max(0, start + env.tick / FPS - perf_counter()))
            observation = env.observe()
        else:
            observation = env.step(ACTIONS[action], ticks=ticks)
        replay.append((action, ticks))
        if observation.player is None:
            continue
        if observation.level_marker != initial.level_marker:
            return Result(best_progress, tuple(replay), False, True)
        if observation.death_marker != initial.death_marker:
            return Result(best_progress, tuple(replay), True, False)

        while waypoint < len(WAYPOINTS):
            remaining = dist(observation.player, WAYPOINTS[waypoint])
            if remaining > WAYPOINT_RADII[waypoint]:
                segment = dist(WAYPOINTS[waypoint - 1], WAYPOINTS[waypoint])
                fraction = max(0.0, 1.0 - remaining / segment)
                best_progress = max(best_progress, waypoint - 1 + fraction)
                break
            waypoint += 1
            best_progress = float(waypoint - 1)
    return Result(best_progress, tuple(replay), False, False)


def random_genome(rng, length, strategy):
    if strategy == "classic":
        return tuple((rng.randrange(len(ACTIONS)), ACTION_TICKS) for _ in range(length))
    moves = tuple((rng.choice((0, 1, 2, 3, 4, 4, 4)), rng.choice(MOVE_DURATIONS))
                  for _ in range(max(1, length // 3)))
    return ((0, rng.choice(ENTRY_DELAYS)),) + moves


def mutate(rng, parent, strategy):
    child = list(parent)
    if strategy == "classic":
        edits = rng.randint(1, max(1, len(child) // 8))
        for index in rng.sample(range(len(child)), edits):
            direction, ticks = child[index]
            child[index] = ((direction + rng.randrange(1, len(ACTIONS))) % len(ACTIONS), ticks)
        return tuple(child)

    index = rng.randrange(len(child))
    direction, ticks = child[index]
    if index == 0:
        child[0] = (0, rng.choice([delay for delay in ENTRY_DELAYS if delay != ticks]))
        return tuple(child)
    edit = rng.randrange(4)
    if edit == 0:
        child[index] = ((direction + rng.randrange(1, len(ACTIONS))) % len(ACTIONS), ticks)
    elif edit == 1:
        deltas = [delta for delta in (-12, -6, -3, 3, 6, 12) if 3 <= ticks + delta <= 90]
        child[index] = (direction, ticks + rng.choice(deltas))
    elif edit == 2 or len(child) == 2:
        child.insert(index, (0, rng.choice(MOVE_DURATIONS)))
    else:
        child.pop(index)
    return tuple(child)


def schedule(genome, strategy, budget):
    moves = []
    if strategy == "classic":
        sequence = genome
    else:
        sequence = OPENING[:2] + genome[:1] + OPENING[2:] + genome[1:]
    for direction, ticks in (*sequence, (0, budget)):
        ticks = min(ticks, budget)
        while ticks:
            sample = min(ticks, ACTION_TICKS)
            moves.append((direction, sample))
            ticks -= sample
            budget -= sample
        if not budget:
            break
    return tuple(moves)


def save_best(path, result, generation, args, evaluations):
    replay = {
        "level": 1,
        "direct_reset": True,
        "generation": generation,
        "seed": args.seed,
        "strategy": args.strategy,
        "evaluations": evaluations,
        "progress": result.progress,
        "cleared": result.cleared,
        "died": result.died,
        "ticks": result.ticks,
        "actions": [[ACTIONS[action], ticks] for action, ticks in result.replay],
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(replay, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def evolve(env, args):
    rng = random.Random(args.seed)
    population = [random_genome(rng, args.actions, args.strategy) for _ in range(args.population)]
    if args.strategy == "classic":
        prefix = schedule((), "crossing", sum(ticks for _, ticks in OPENING))[:args.actions]
        population[0] = prefix + population[0][len(prefix):]
    else:
        # One candidate attempts a sustained crossing; the others explore different timings.
        population[0] = ((0, 0), (4, 72), (1, 33), (4, 27))
        # Probe a few entry timings with the same crossing before mutating its moves.
        for index in range(1, min(4, args.population)):
            population[index] = ((0, 18 * index),) + population[0][1:]
    scores = {}
    best = None
    elite_count = max(1, args.population // 4)
    budget = args.actions * ACTION_TICKS

    for generation in range(1, args.generations + 1):
        for index, genome in enumerate(population, start=1):
            print(f"Generation {generation}/{args.generations} | Candidate {index}/{len(population)}",
                  flush=True)
            moves = schedule(genome, args.strategy, budget)
            cached = moves in scores
            if not cached:
                scores[moves] = evaluate(env, moves)
            result = scores[moves]
            status = "cleared" if result.cleared else "died" if result.died else "time limit"
            print(f"  {result.progress:.2f}/{len(WAYPOINTS) - 1} waypoints | {status}"
                  f" at {result.ticks} ticks | {len(scores)} evaluations"
                  f"{' (cached)' if cached else ''}", flush=True)
            if best is None or result.rank > best.rank:
                best = result
                save_best(args.output, result, generation, args, len(scores))
                print(f"  New best: {result.progress:.2f}/{len(WAYPOINTS) - 1} waypoints"
                      f"{' | Level cleared' if result.cleared else ''}", flush=True)
            if result.cleared or len(scores) == args.evaluations:
                return
        ranked = sorted(population, key=lambda genome: scores[schedule(genome, args.strategy, budget)].rank,
                        reverse=True)
        elites = ranked[:elite_count]
        population = elites + [
            mutate(rng, rng.choice(elites), args.strategy)
            for _ in range(args.population - elite_count - 1)
        ] + [random_genome(rng, args.actions, args.strategy)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--population", type=int, default=12)
    parser.add_argument("--generations", type=int, default=8)
    parser.add_argument("--actions", type=int, default=40,
                        help="Time budget in six-tick units per candidate, including the opening.")
    parser.add_argument("--strategy", choices=("crossing", "classic"), default="crossing")
    parser.add_argument("--evaluations", type=int, help="Stop after this many uncached candidates.")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "best_replay.json")
    parser.add_argument("--watch", type=Path, help="Watch a saved replay instead of evolving.")
    parser.add_argument("--ruffle-dir", type=Path, default=ROOT / "ruffle-web" / "package")
    args = parser.parse_args()
    if args.population < 3 or min(args.generations, args.actions) < 1:
        parser.error("Use a population of at least 3 and positive generations and actions.")
    if args.evaluations is not None and args.evaluations < 1:
        parser.error("--evaluations must be positive.")
    if not args.watch and args.strategy == "crossing" and args.actions <= 10:
        parser.error("Crossing needs more than 10 time units to leave room after the opening.")
    if not (args.ruffle_dir / "ruffle.js").is_file():
        parser.error("Point --ruffle-dir at the self-hosted Ruffle directory containing ruffle.js.")
    replay = json.loads(args.watch.read_text(encoding="utf-8")) if args.watch else None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=replay is None, timeout=15_000)
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, service_workers="block")
            if replay is None:
                env = RuffleEnv(page, args.ruffle_dir, direct_reset=True)
                evolve(env, args)
                print(f"Best replay saved to {args.output.resolve()}", flush=True)
            else:
                env = RuffleEnv(page, args.ruffle_dir, direct_reset=replay["direct_reset"],
                                level=replay["level"])
                watch_replay(env, replay["actions"])
        finally:
            browser.close()


if __name__ == "__main__":
    main()
