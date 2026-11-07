"""Evolve a level 1 policy that reacts to the blue enemies."""

import argparse
import json
import random
from math import dist
from pathlib import Path
from statistics import mean
from time import sleep

from evolve import ACTIONS, ACTION_TICKS, OPENING, WAYPOINTS, evaluate, schedule
from training_env import HEIGHT, ROOT, WIDTH, RuffleEnv, sync_playwright


TRAIN_DELAYS = (0, 12, 24)
TEST_DELAYS = (6, 18, 30)
DIRECTIONS = ((0, 0), (0, -1), (0, 1), (-1, 0), (1, 0))
# The features are progress, predicted collision risk, clearance, and waiting.
INITIAL_WEIGHTS = (1.0, 3.0, 0.25, -0.05)


class Policy:
    def __init__(self, weights):
        self.weights = weights
        self.previous = None

    def __call__(self, observation, target):
        previous = self.previous
        self.previous = observation
        if observation.player is None:
            return 0

        enemies = []
        for lane, enemy in enumerate(observation.enemies):
            if enemy is None:
                continue
            prior = previous.enemies[lane] if previous and previous.enemies else None
            velocity = (enemy[0] - prior[0]) / (observation.tick - previous.tick) if prior else 0
            enemies.append((*enemy, velocity))

        x, y = observation.player
        target_distance = dist((x, y), target)
        scores = []
        for dx, dy in DIRECTIONS:
            final = (x + dx * 3 * ACTION_TICKS, y + dy * 3 * ACTION_TICKS)
            clearance = min(
                (dist((x + dx * 3 * tick, y + dy * 3 * tick),
                      (max(156, min(394, ex + vx * tick)), ey))
                 for ex, ey, vx in enemies for tick in range(1, ACTION_TICKS + 1)),
                default=40,
            )
            features = (
                (target_distance - dist(final, target)) / (3 * ACTION_TICKS),
                -max(0.0, 1 - clearance / 19) ** 2,
                min(clearance, 40) / 40,
                float(dx == dy == 0),
            )
            scores.append(sum(weight * feature for weight, feature in zip(self.weights, features)))
        return max(range(len(scores)), key=scores.__getitem__)


def reactive_attempt(env, weights, delay, decisions, *, pace=False):
    # Reuse the safe downward and rightward turns. The policy decides when to go up.
    prefix = tuple((0, min(ACTION_TICKS, delay - elapsed))
                   for elapsed in range(0, delay, ACTION_TICKS))
    prefix += tuple((action, ACTION_TICKS) for action, ticks in OPENING[:2]
                    for _ in range(ticks // ACTION_TICKS))
    return evaluate(env, prefix, policy=Policy(weights), decisions=decisions, pace=pace)


def rank(results):
    return (sum(result.cleared for result in results),
            mean(result.progress for result in results),
            sum(not result.died for result in results))


def report(label, delays, results):
    clears, progress, survivors = rank(results)
    print(f"{label} | {clears}/{len(results)} clears | mean {progress:.2f}/{len(WAYPOINTS) - 1}"
          f" | {survivors}/{len(results)} survived", flush=True)
    for delay, result in zip(delays, results):
        status = "cleared" if result.cleared else "died" if result.died else "time limit"
        print(f"  Delay {delay:2} ticks | {result.progress:.2f} | {status}", flush=True)


def mutate(weights, rng):
    movement_weights = tuple(max(-6.0, min(8.0, weight + rng.gauss(0, 0.55)))
                             for weight in weights[:3])
    # A large wait bias can drown out the evidence that pausing is safer.
    wait_bias = max(-0.15, min(0.15, weights[3] + rng.gauss(0, 0.06)))
    return (*movement_weights, wait_bias)


def train(env, args):
    rng = random.Random(args.seed)
    population = [INITIAL_WEIGHTS] + [mutate(INITIAL_WEIGHTS, rng) for _ in range(args.population - 1)]
    cache = {}
    best = None
    for generation in range(1, args.generations + 1):
        for index, weights in enumerate(population, 1):
            print(f"Generation {generation}/{args.generations} | Policy {index}/{len(population)}",
                  flush=True)
            if weights not in cache:
                cache[weights] = [reactive_attempt(env, weights, delay, args.decisions)
                                  for delay in TRAIN_DELAYS]
            results = cache[weights]
            if best is None or rank(results) > rank(cache[best]):
                best = weights
                report("New best (training)", TRAIN_DELAYS, results)
                model = {"weights": best, "decisions": args.decisions,
                         "generation": generation, "seed": args.seed}
                temporary = args.output.with_name(args.output.name + ".tmp")
                temporary.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
                temporary.replace(args.output)
        elites = sorted(population, key=lambda weights: rank(cache[weights]), reverse=True)[:2]
        population = elites + [mutate(rng.choice(elites), rng)
                               for _ in range(args.population - len(elites))]
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--population", type=int, default=6)
    parser.add_argument("--generations", type=int, default=4)
    parser.add_argument("--decisions", type=int, default=32, help="Six-tick decisions after the opening.")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "reactive_policy.json")
    parser.add_argument("--watch", type=Path, help="Run a saved policy live in a visible window.")
    parser.add_argument("--delay", type=int, default=6, help="Starting delay in ticks when watching.")
    parser.add_argument("--baseline", type=Path, help="Compare a saved level 1 replay on held-out delays.")
    parser.add_argument("--ruffle-dir", type=Path, default=ROOT / "ruffle-web" / "package")
    args = parser.parse_args()
    if args.population < 3 or min(args.generations, args.decisions) < 1 or args.delay < 0:
        parser.error("Use at least three policies, positive generations and decisions, and a nonnegative delay.")
    if not (args.ruffle_dir / "ruffle.js").is_file():
        parser.error("Point --ruffle-dir at the self-hosted Ruffle directory containing ruffle.js.")
    model = json.loads(args.watch.read_text(encoding="utf-8")) if args.watch else None
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    if baseline and (baseline["level"] != 1 or not baseline["direct_reset"]):
        parser.error("The baseline must be a direct-start level 1 replay.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=model is None, timeout=15_000)
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, service_workers="block")
            env = RuffleEnv(page, args.ruffle_dir, direct_reset=True, track_enemies=True)
            if model:
                result = reactive_attempt(env, model["weights"], args.delay, model["decisions"], pace=True)
                report("Live policy", (args.delay,), (result,))
                sleep(3)
                return
            weights = train(env, args)
            results = [reactive_attempt(env, weights, delay, args.decisions) for delay in TEST_DELAYS]
            report("Reactive policy (held out)", TEST_DELAYS, results)
            if baseline:
                moves = tuple((ACTIONS.index(tuple(keys)), ticks) for keys, ticks in baseline["actions"])
                budget = sum(ticks for _, ticks in OPENING[:2]) + args.decisions * ACTION_TICKS
                moves = schedule(moves, "classic", budget)
                results = [evaluate(env, ((0, delay),) + moves) for delay in TEST_DELAYS]
                report("Fixed replay (held out)", TEST_DELAYS, results)
            print(f"Policy saved to {args.output.resolve()}", flush=True)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
