"""Evolve planner settings and compare real-game outcomes with a fixed baseline."""

import argparse
import json
import random
from collections import deque
from dataclasses import asdict, dataclass
from io import BytesIO
from math import dist, isfinite
from pathlib import Path
from statistics import mean
from time import perf_counter, sleep

from PIL import Image

from benchmark_levels import (
    HAZARD_PADDING, PLANNER_DIRECTIONS, PLANNER_KEYS, STEP, STRIDE,
    floor_graph, hazard_timeline, layout, pellet_centers, plan, record_hazard_frames,
)
from direct_start import level_exits
from training_env import (
    FPS, ROOT, Observation, RuffleEnv, add_browser_argument, game_page, launch_browser, sync_playwright,
)


@dataclass(frozen=True)
class Genome:
    clearance: int = HAZARD_PADDING
    goal_weight: float = 1.0
    wait_cost: float = 0.0
    turn_cost: float = 0.0


@dataclass
class Scenario:
    level: int
    delay: int
    initial: Observation
    tiles: set
    pellets: list
    exits: set
    frames: list
    task_distance: "TaskDistance"


def distances_to(graph, targets):
    distance = dict.fromkeys(targets & graph.keys(), 0)
    frontier = deque(distance)
    while frontier:
        cell = frontier.popleft()
        for _, points in graph[cell]:
            neighbor = points[-1]
            if neighbor not in distance:
                distance[neighbor] = distance[cell] + STEP
                frontier.append(neighbor)
    return distance


class TaskDistance:
    """Static floor-route cost through the remaining pellets and then the exit."""

    def __init__(self, distances, pellet_targets):
        self.distances = distances
        self.count = len(pellet_targets)
        self.between = [
            [min((distance.get(cell, float("inf")) for cell in targets), default=float("inf"))
             for distance in distances]
            for targets in pellet_targets
        ]
        self.tails = {}
        self.trees = {}
        self.mode = "exact pellet order" if self.count <= 10 else "spanning-tree estimate"
        self.initial = self.remaining((0, 0), set())

    def tail(self, index, mask):
        key = index, mask
        if key not in self.tails:
            if not mask:
                cost = self.between[index][-1]
            else:
                cost = min(
                    self.between[index][other] + self.tail(other, mask ^ (1 << other))
                    for other in range(self.count) if mask & (1 << other)
                )
            self.tails[key] = cost
        return self.tails[key]

    def tree(self, mask):
        if mask not in self.trees:
            pending = {index for index in range(self.count) if mask & (1 << index)}
            # Start the tree at the exit and connect every pending pellet.
            nearest = {index: self.between[index][-1] for index in pending}
            cost = 0
            while pending:
                chosen = min(pending, key=nearest.get)
                cost += nearest[chosen]
                pending.remove(chosen)
                for other in pending:
                    nearest[other] = min(nearest[other], self.between[chosen][other])
            self.trees[mask] = cost
        return self.trees[mask]

    def remaining(self, cell, collected):
        mask = sum(1 << index for index in range(self.count) if index not in collected)
        if not mask:
            return self.distances[-1].get(cell, float("inf"))
        candidates = [index for index in range(self.count) if mask & (1 << index)]
        if self.count <= 10:
            return min(self.distances[index].get(cell, float("inf"))
                       + self.tail(index, mask ^ (1 << index)) for index in candidates)
        return min(self.distances[index].get(cell, float("inf")) for index in candidates) + self.tree(mask)


def reset_scenario(env, level, delay):
    observation = env.reset(level)
    if delay:
        observation = env.step(ticks=delay)
    return observation


def prepare(env, levels, delays, ticks):
    scenarios = []
    exits_by_level = level_exits((ROOT / "WHGOriginal.swf").read_bytes())
    env.reset(levels[0])  # Use the same warm reset phase for calibration and attempts.
    for level in levels:
        for delay in delays:
            print(f"Preparing level {level}, delay {delay}", flush=True)
            initial = reset_scenario(env, level, delay)
            if initial.player is None:
                raise RuntimeError(f"Level {level} has no visible player after delay {delay}")
            with Image.open(BytesIO(env.screenshot())) as image:
                tiles, pellets, exits = layout(image.convert("RGB"), initial.player, exits_by_level[level])
            graph = floor_graph(tiles)
            if (0, 0) not in graph or not exits:
                raise RuntimeError(f"Cannot model the floor or exit on level {level}")
            pellet_targets = [
                {cell for cell in graph
                 if abs(initial.player[0] + cell[0] * STRIDE - point[0]) <= 7
                 and abs(initial.player[1] + cell[1] * STRIDE - point[1]) <= 7}
                for point in pellets
            ]
            distances = [distances_to(graph, targets) for targets in [*pellet_targets, exits]]
            env.watch_status(level=level, phase="Observing enemies",
                             detail=f"Recording {ticks} ticks at starting delay {delay}.",
                             targets=[*pellets, exits_by_level[level]])
            frames = record_hazard_frames(env, ticks)
            after = env.observe()
            if after.death_marker is not None and after.death_marker != initial.death_marker:
                raise RuntimeError(f"Level {level} is unsafe to observe while idle")
            scenarios.append(Scenario(level, delay, initial, tiles, pellets, exits, frames,
                                      TaskDistance(distances, pellet_targets)))
    return scenarios


class Progress:
    def __init__(self, scenario):
        self.scenario = scenario
        self.collected = set()
        self.missing_counts = [0] * len(scenario.pellets)
        self.best = 0.0
        self.trace = []
        self.route_fraction = 0.0
        self.pellet_fraction = 0.0
        self.remaining_ticks = scenario.task_distance.initial

    def update(self, image, player, tick=0):
        if player is None:
            return
        scenario = self.scenario
        visible = pellet_centers(image)
        for index, point in enumerate(scenario.pellets):
            missing = not any(dist(point, current) < 6 for current in visible)
            self.missing_counts[index] = self.missing_counts[index] + 1 if missing else 0
            if self.missing_counts[index] >= 2:
                self.collected.add(index)
        cell = tuple(round((coordinate - origin) / (STEP * STRIDE)) * STEP
                     for coordinate, origin in zip(player, scenario.initial.player))
        remaining = scenario.task_distance.remaining(cell, self.collected)
        initial = scenario.task_distance.initial
        route_fraction = 0.0
        if isfinite(initial) and initial > 0 and isfinite(remaining):
            route_fraction = max(0, min(1, 1 - remaining / initial))
        pellet_fraction = len(self.collected) / len(scenario.pellets) if scenario.pellets else 0.0
        score = (0.4 * pellet_fraction + 0.6 * route_fraction
                 if scenario.pellets else route_fraction)
        # Keep a clear strictly above any unconfirmed partial attempt.
        score = min(0.99, score)
        if score > self.best or not self.trace:
            self.best = score
            self.route_fraction = route_fraction
            self.pellet_fraction = pellet_fraction
            self.remaining_ticks = remaining
            self.trace.append({"tick": tick, "fitness": score, "route_progress": route_fraction,
                               "pellets_collected": len(self.collected),
                               "remaining_route_ticks": remaining if isfinite(remaining) else None})


def evaluate(env, scenario, genome, ticks, *, watch=False):
    env.watch_status(level=scenario.level, phase="Planning route",
                     detail="Searching with the saved planner genome.")
    timeline = hazard_timeline(scenario.frames, scenario.initial.player, ticks, genome.clearance)
    actions, reason = plan(
        scenario.initial.player, scenario.tiles, scenario.pellets, scenario.exits, timeline,
        goal_weight=genome.goal_weight, wait_cost=genome.wait_cost, turn_cost=genome.turn_cost,
    )
    initial = reset_scenario(env, scenario.level, scenario.delay)
    if initial.frame_signature != scenario.initial.frame_signature:
        raise RuntimeError(f"Calibration changed on level {scenario.level}, delay {scenario.delay}")
    points = [initial.player]
    for action in actions:
        x, y = points[-1]
        dx, dy = PLANNER_DIRECTIONS[action]
        points.append((x + dx * STRIDE * STEP, y + dy * STRIDE * STEP))
    env.watch_status(level=scenario.level, phase="Evaluating evolved planner",
                     route=points, targets=scenario.pellets, progress=f"0 / {len(actions)} moves")
    progress = Progress(scenario)
    with Image.open(BytesIO(env.screenshot())) as image:
        progress.update(image.convert("RGB"), initial.player)
    status = "partial route" if actions else "no route"
    observation = initial
    start = perf_counter()
    moved_ticks = 0
    settle = 90
    for index in range(len(actions) + settle // STEP):
        active = index < len(actions)
        keys = PLANNER_KEYS[actions[index]] if active else ()
        if watch:
            for _ in range(STEP):
                env.step(keys, observe=False)
                moved_ticks += 1
                sleep(max(0, start + moved_ticks / FPS - perf_counter()))
        else:
            env.step(keys, ticks=STEP, observe=False)
            moved_ticks += STEP
        png = env.screenshot()
        observation = env.observe(screenshot_bytes=png)
        env.watch_status(progress=f"{min(index + 1, len(actions))} / {len(actions)} moves")
        if observation.level_marker is not None and observation.level_marker != initial.level_marker:
            status = "cleared"
            progress.best = 1.0
            progress.collected.update(range(len(scenario.pellets)))
            progress.route_fraction = 1.0
            progress.pellet_fraction = 1.0 if scenario.pellets else 0.0
            progress.remaining_ticks = 0
            progress.trace.append({"tick": moved_ticks, "fitness": 1.0, "route_progress": 1.0,
                                   "pellets_collected": len(progress.collected), "remaining_route_ticks": 0})
            break
        if observation.death_marker is not None and observation.death_marker != initial.death_marker:
            status = "died"
            break
        with Image.open(BytesIO(png)) as image:
            progress.update(image.convert("RGB"), observation.player, moved_ticks)
        env.watch_status(progress=f"{min(index + 1, len(actions))} / {len(actions)} moves"
                         f" | fitness {progress.best:.1%} | pellets {len(progress.collected)}/{len(scenario.pellets)}")
        if not actions:
            break
    return {"level": scenario.level, "delay": scenario.delay, "status": status,
            "progress": progress.best, "pellets_collected": len(progress.collected),
            "pellets_total": len(scenario.pellets), "ticks": moved_ticks, "reason": reason,
            "route_progress": progress.route_fraction, "pellet_fraction": progress.pellet_fraction,
            "remaining_route_ticks": progress.remaining_ticks if isfinite(progress.remaining_ticks) else None,
            "best_tick": progress.trace[-1]["tick"],
            "pellets_at_best": progress.trace[-1]["pellets_collected"],
            "distance_model": scenario.task_distance.mode, "progress_trace": progress.trace}


def summary(results):
    return {"clears": sum(row["status"] == "cleared" for row in results), "attempts": len(results),
            "mean_progress": mean(row["progress"] for row in results),
            "mean_route_progress": mean(row.get("route_progress", 0) for row in results),
            "mean_pellet_fraction": mean(row.get("pellet_fraction", 0) for row in results),
            "survivors": sum(row["status"] != "died" for row in results),
            "mean_ticks": mean(row["ticks"] for row in results)}


def fitness(results):
    values = summary(results)
    return values["clears"], values["mean_progress"], values["survivors"], -values["mean_ticks"]


def mutate(parent, rng):
    return Genome(
        max(10, min(17, parent.clearance + rng.choice((-2, -1, 0, 1, 2)))),
        round(max(0.8, min(2.5, parent.goal_weight + rng.gauss(0, 0.25))), 3),
        round(max(0, min(1.0, parent.wait_cost + rng.gauss(0, 0.2))), 3),
        round(max(0, min(2.0, parent.turn_cost + rng.gauss(0, 0.3))), 3),
    )


def write_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def print_summary(label, results):
    values = summary(results)
    print(f"{label}: {values['clears']}/{values['attempts']} clears, "
          f"mean fitness {values['mean_progress']:.1%}, "
          f"route progress {values['mean_route_progress']:.1%}", flush=True)


def evaluate_suite(env, scenarios, genome, ticks, label):
    results = []
    for index, scenario in enumerate(scenarios, 1):
        print(f"{label} {index}/{len(scenarios)} | Level {scenario.level}, delay {scenario.delay}",
              flush=True)
        results.append(evaluate(env, scenario, genome, ticks))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", type=int, nargs="+", default=[3, 4, 13])
    parser.add_argument("--train-delays", type=int, nargs="+", default=[0])
    parser.add_argument("--test-delays", type=int, nargs="+", default=[12, 24])
    parser.add_argument("--forecast-ticks", type=int, default=540)
    parser.add_argument("--population", type=int, default=4)
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "evolved_planner.json")
    parser.add_argument("--report", type=Path, default=ROOT / "planner_evolution_report.json")
    parser.add_argument("--watch", type=Path, help="Watch a saved planner genome on --levels.")
    parser.add_argument("--delay", type=int, default=0, help="Starting delay in ticks for --watch.")
    add_browser_argument(parser)
    args = parser.parse_args()
    if (args.population < 3 or args.generations < 1 or args.forecast_ticks < 6
            or any(not 1 <= level <= 29 for level in args.levels)):
        parser.error("Use population >= 3, positive generations, forecast >= 6, and levels 1–29.")
    delays = [*args.train_delays, *args.test_delays]
    if any(delay < 0 or delay % 3 for delay in delays):
        parser.error("Use nonnegative starting delays that are multiples of three ticks.")
    if args.delay < 0 or args.delay % 3:
        parser.error("--delay must be a nonnegative multiple of three ticks.")
    if not args.watch and set(args.train_delays) & set(args.test_delays):
        parser.error("Training and test delays must be disjoint.")
    if (len(set(args.levels)) != len(args.levels)
            or len(set(args.train_delays)) != len(args.train_delays)
            or len(set(args.test_delays)) != len(args.test_delays)):
        parser.error("List each level and each delay once.")
    if args.output.resolve() == args.report.resolve():
        parser.error("The model and report must use different files.")
    model = json.loads(args.watch.read_text(encoding="utf-8")) if args.watch else None
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, browser=args.browser, headless=model is None)
        try:
            page = game_page(browser, watch=model is not None)
            env = RuffleEnv(page, ROOT / "ruffle-web" / "package", direct_reset=True,
                            fit_window=model is not None)
            ticks = model["forecast_ticks"] if model else args.forecast_ticks
            training = prepare(env, args.levels, [args.delay] if model else args.train_delays, ticks)
            if model:
                genome = Genome(**model["genome"])
                for scenario in training:
                    result = evaluate(env, scenario, genome, ticks, watch=True)
                    print_summary(f"Level {scenario.level}, delay {scenario.delay}", [result])
                    env.watch_status(phase=result["status"].capitalize(),
                                     detail=f"Progress: {result['progress']:.1%}",
                                     route=[], targets=[], clearTraces=True)
                    sleep(3)
                return
            baseline = Genome()
            rng = random.Random(args.seed)
            population = [baseline] + [mutate(baseline, rng) for _ in range(args.population - 1)]
            cache = {}
            best = baseline
            report = {"fitness_version": "remaining_task_distance_v2",
                      "progress_definition": {"pellet_weight": 0.4, "route_weight": 0.6,
                                              "sample_ticks": STEP, "exact_pellet_limit": 10},
                      "seed": args.seed, "population": args.population, "generations": args.generations,
                      "browser": args.browser, "browser_version": browser.version,
                      "levels": args.levels, "forecast_ticks": ticks,
                      "train_delays": args.train_delays, "test_delays": args.test_delays,
                      "baseline_genome": asdict(baseline), "history": [], "candidates": []}
            started = perf_counter()
            for generation in range(1, args.generations + 1):
                for index, genome in enumerate(population, 1):
                    print(f"Generation {generation}/{args.generations}, candidate {index}/{len(population)}"
                          f" | {asdict(genome)}", flush=True)
                    if genome not in cache:
                        cache[genome] = []
                        for scenario in training:
                            result = evaluate(env, scenario, genome, ticks)
                            cache[genome].append(result)
                            print(f"  Level {scenario.level}, delay {scenario.delay}: "
                                  f"{result['status']}, fitness {result['progress']:.1%}, "
                                  f"route {result['route_progress']:.1%}, "
                                  f"pellets {result['pellets_collected']}/{result['pellets_total']}",
                                  flush=True)
                        report["candidates"].append({"genome": asdict(genome), "results": cache[genome]})
                    if fitness(cache[genome]) > fitness(cache[best]):
                        best = genome
                    write_json(args.output, {"format": "planner_genome_v1", "genome": asdict(best),
                                             "fitness_version": "remaining_task_distance_v2",
                                             "forecast_ticks": ticks, "seed": args.seed,
                                             "levels": args.levels, "train_delays": args.train_delays})
                print_summary(f"Generation {generation} best (training)", cache[best])
                report["history"].append({"generation": generation, "genome": asdict(best),
                                           **summary(cache[best])})
                write_json(args.report, report)
                elites = sorted(dict.fromkeys(population),
                                key=lambda genome: fitness(cache[genome]), reverse=True)[:2]
                population = elites + [mutate(rng.choice(elites), rng)
                                       for _ in range(args.population - len(elites))]
            testing = prepare(env, args.levels, args.test_delays, ticks)
            baseline_test = evaluate_suite(env, testing, baseline, ticks, "Baseline test")
            evolved_test = (baseline_test if best == baseline else
                            evaluate_suite(env, testing, best, ticks, "Evolved test"))
            report.update({"baseline_training": cache[baseline], "best_training": cache[best],
                           "best_genome": asdict(best), "baseline_test": baseline_test,
                           "evolved_test": evolved_test, "elapsed_seconds": perf_counter() - started})
            report["summary"] = {
                "training": {"baseline": summary(cache[baseline]), "evolved": summary(cache[best])},
                "held_out": {"baseline": summary(baseline_test), "evolved": summary(evolved_test)},
            }
            write_json(args.report, report)
            print_summary("Baseline on held-out delays", baseline_test)
            print_summary("Evolved on held-out delays", evolved_test)
            print(f"Model: {args.output.resolve()}\nReport: {args.report.resolve()}", flush=True)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
