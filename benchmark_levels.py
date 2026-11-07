"""Benchmark the two tuned planners and a screen-derived planner on the full SWF."""

import argparse
import json
from collections import Counter, deque
from heapq import heappop, heappush
from io import BytesIO
from itertools import count
from math import ceil, floor
from pathlib import Path
from time import perf_counter, sleep

from PIL import Image

from evolve import ACTIONS
from plan_level1 import attempt, attempt_level_two
from reactive import DIRECTIONS
from training_env import FPS, HEIGHT, ROOT, WIDTH, RuffleEnv, sync_playwright


TILE = 25
STRIDE = 3
STEP = 3
GREEN = (181, 254, 180)
FLOOR_COLORS = {GREEN, (230, 230, 255), (247, 247, 255), (235, 225, 251), (252, 233, 233)}
CAPTURE = """() => {
    const canvas = document.querySelector("ruffle-player").shadowRoot.querySelector("canvas");
    const context = canvas.getContext("2d");
    const width = canvas.width;
    const top = 26;
    function sample() {
        const pixels = context.getImageData(0, top, width, 348).data;
        const hits = new Set();
        for (let y = 0; y < 348; y += 2) {
            for (let x = 0; x < width; x += 2) {
                const i = (y * width + x) * 4;
                const [r, g, b] = [pixels[i], pixels[i + 1], pixels[i + 2]];
                if ((r < 30 && g < 30 && b > 210) ||
                    (r === 100 && g === 43 && b === 187) ||
                    (r === 201 && g === 29 && b === 29)) hits.add(y * width + x);
            }
        }
        const boxes = [];
        for (const point of hits) {
            hits.delete(point);
            const queue = [point];
            let left = width, right = 0, first = 348, last = 0;
            while (queue.length) {
                const cell = queue.pop();
                const x = cell % width, y = Math.floor(cell / width);
                left = Math.min(left, x); right = Math.max(right, x);
                first = Math.min(first, y); last = Math.max(last, y);
                for (const neighbor of [cell - 2, cell + 2, cell - width * 2, cell + width * 2]) {
                    if (hits.delete(neighbor)) queue.push(neighbor);
                }
            }
            boxes.push([left, first + top, right, last + top]);
        }
        frames.push([performance.now() - start, boxes]);
    }
    const start = performance.now(), frames = [];
    sample();
    function frame() {
        sample();
        window.benchmarkCapture.id = requestAnimationFrame(frame);
    }
    window.benchmarkCapture = {frames, id: requestAnimationFrame(frame)};
}"""


def components(points):
    remaining = set(points)
    while remaining:
        first = min(remaining)
        remaining.remove(first)
        group = [first]
        for x, y in group:
            for dx, dy in DIRECTIONS[1:]:
                neighbor = (x + dx, y + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    group.append(neighbor)
        yield group


def layout(image, origin):
    floor_tiles = set()
    green_tiles = set()
    for row in range(1, HEIGHT // TILE - 1):
        for column in range(WIDTH // TILE):
            x, y = column * TILE, row * TILE
            colors = {color: amount for amount, color in
                      image.crop((x, y, x + TILE, y + TILE)).getcolors(TILE * TILE)}
            if sum(colors.get(color, 0) for color in FLOOR_COLORS) >= 125:
                floor_tiles.add((column, row))
            if colors.get(GREEN, 0) >= 125:
                green_tiles.add((column, row))

    yellow = set()
    for y in range(26, 374):
        for x in range(WIDTH):
            r, g, b = image.getpixel((x, y))
            if r >= 220 and g >= 220 and b <= 35:
                yellow.add((x, y))
    pellets = [(sum(x for x, _ in group) / len(group), sum(y for _, y in group) / len(group))
               for group in components(yellow) if len(group) >= 4]

    origin_x, origin_y = origin
    spawn_tile = (int(origin_x // TILE), int(origin_y // TILE))
    zones = list(components(green_tiles))
    other_zones = [zone for zone in zones if spawn_tile not in zone]
    if not other_zones and not pellets:
        return set(), pellets, set()
    # If there are several green regions, use the farthest one as the exit candidate.
    target_zone = max(other_zones or zones, default=[], key=lambda zone: max(
        abs(column - spawn_tile[0]) + abs(row - spawn_tile[1]) for column, row in zone))
    floor_positions = set()
    exit_positions = set()
    for gy in range(ceil((26 - origin_y) / STRIDE), floor((373 - origin_y) / STRIDE) + 1):
        y = origin_y + gy * STRIDE
        for gx in range(ceil(-origin_x / STRIDE), floor((WIDTH - 1 - origin_x) / STRIDE) + 1):
            x = origin_x + gx * STRIDE
            if all((int(px // TILE), int(py // TILE)) in floor_tiles
                   for px in (x - 9, x + 9) for py in (y - 9, y + 9)):
                floor_positions.add((gx, gy))
                if all((int(px // TILE), int(py // TILE)) in target_zone
                       for px in (x - 6, x + 6) for py in (y - 6, y + 6)):
                    exit_positions.add((gx, gy))
    return floor_positions, pellets, exit_positions


def record_hazards(env, origin, ticks):
    env.page.evaluate(CAPTURE)
    env.step(ticks=ticks, observe=False)
    frames = env.page.evaluate("""() => {
        cancelAnimationFrame(window.benchmarkCapture.id);
        return window.benchmarkCapture.frames;
    }""")
    timeline = []
    frame_index = 0
    origin_x, origin_y = origin
    blocked = set()
    for tick in range(ticks + 1):
        elapsed = round(tick * 1000 / FPS)
        if tick == 0 or (frame_index + 1 < len(frames) and frames[frame_index + 1][0] <= elapsed):
            while frame_index + 1 < len(frames) and frames[frame_index + 1][0] <= elapsed:
                frame_index += 1
            blocked = set()
            for left, top, right, bottom in frames[frame_index][1]:
                # Include the enemy outline, the player's extent, and the sampling margin.
                for y in range(ceil((top - 12 - origin_y) / STRIDE),
                               floor((bottom + 12 - origin_y) / STRIDE) + 1):
                    for x in range(ceil((left - 12 - origin_x) / STRIDE),
                                   floor((right + 12 - origin_x) / STRIDE) + 1):
                        blocked.add((x, y))
        timeline.append(blocked)
    return timeline


def search(start, targets, tiles, timeline, limit=35_000):
    if not targets:
        return None
    min_x, max_x = min(x for x, _ in targets), max(x for x, _ in targets)
    min_y, max_y = min(y for _, y in targets), max(y for _, y in targets)

    def distance(x, y):
        return max(0, min_x - x, x - max_x) + max(0, min_y - y, y - max_y)

    order = count()
    queue = [(0, next(order), start)]
    parents = {start: None}
    for _ in range(limit):
        if not queue:
            break
        _, _, state = heappop(queue)
        x, y, tick = state
        if (x, y) in targets:
            end = state
            actions = deque()
            while parents[state] is not None:
                state, action = parents[state]
                actions.appendleft(action)
            return end, list(actions)
        if tick + STEP >= len(timeline):
            continue
        for action, (dx, dy) in enumerate(DIRECTIONS):
            next_state = (x + dx * STEP, y + dy * STEP, tick + STEP)
            if next_state in parents:
                continue
            for offset in range(1, STEP + 1):
                point = (x + dx * offset, y + dy * offset)
                if point not in tiles or point in timeline[tick + offset]:
                    break
            else:
                parents[next_state] = (state, action)
                heappush(queue, (next_state[2] + distance(*next_state[:2]), next(order), next_state))
    return None


def plan(origin, tiles, pellets, exits, timeline):
    state = (0, 0, 0)
    actions = []
    remaining = list(pellets)
    while remaining:
        # Limit the search to the nearest pending pellet to keep the batch bounded.
        pellet = min(remaining, key=lambda point: abs(point[0] - origin[0] - state[0] * STRIDE)
                     + abs(point[1] - origin[1] - state[1] * STRIDE))
        px, py = pellet
        targets = {(x, y) for x, y in tiles if abs(origin[0] + x * STRIDE - px) <= 7
                   and abs(origin[1] + y * STRIDE - py) <= 7}
        segment = search(state, targets, tiles, timeline)
        if segment is None:
            return actions, "pellet route unavailable"
        state, moves = segment
        actions.extend(moves)
        remaining.remove(pellet)
    segment = search(state, exits, tiles, timeline)
    if segment is None:
        return actions, "exit route unavailable"
    return actions + segment[1], None


def attempt_screen_planner(env, level, ticks, *, watch=False):
    if env.page.url == "about:blank":
        # Match the warm reset phase used for the replay when starting with a subset.
        env.reset(level)
    start = env.reset(level)
    with Image.open(BytesIO(env.screenshot())) as image:
        tiles, pellets, exits = layout(image.convert("RGB"), start.player)
    details = {"pellets_visible": len(pellets), "moves": 0}
    if (0, 0) not in tiles or not exits:
        return {**details, "status": "unmodeled", "reason": "could not infer spawn floor or exit"}
    if watch:
        print("  Recording enemy movement, then planning the route.", flush=True)
    timeline = record_hazards(env, start.player, ticks)
    actions, reason = plan(start.player, tiles, pellets, exits, timeline)
    if not actions:
        if watch:
            env.reset(level)
        return {**details, "status": "no route", "reason": reason}
    initial = env.reset(level)
    if initial.frame_signature != start.frame_signature:
        return {**details, "status": "unmodeled", "reason": "idle forecast and replay starts differ"}
    if watch:
        print(f"  Watching the {'partial' if reason else 'planned'} route from spawn.", flush=True)
    started = perf_counter()

    def step(keys=()):
        if not watch:
            return env.step(keys, ticks=STEP)
        for _ in range(STEP):
            env.step(keys, observe=False)
            sleep(max(0, started + env.tick / FPS - perf_counter()))
        return env.observe()

    status = "partial route" if reason else "exit not confirmed"
    for index, action in enumerate(actions, 1):
        observation = step(ACTIONS[action])
        details["moves"] = index
        if observation.level_marker is not None and observation.level_marker != initial.level_marker:
            status = "cleared"
            break
        if observation.death_marker is not None and observation.death_marker != initial.death_marker:
            status = "died"
            break
    if status not in {"cleared", "died"}:
        for _ in range(15):
            observation = step()
            if observation.level_marker is not None and observation.level_marker != initial.level_marker:
                status = "cleared"
                break
            if observation.death_marker is not None and observation.death_marker != initial.death_marker:
                status = "died"
                break
    return {**details, "status": status, "reason": reason}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--levels", type=int, nargs="+")
    selection.add_argument("--failures-from", type=Path,
                           help="Select non-clears from a previous benchmark JSON.")
    parser.add_argument("--forecast-ticks", type=int, default=300)
    parser.add_argument("--output", type=Path, help="Save results to this file.")
    parser.add_argument("--watch", action="store_true", help="Show each attempt in a visible window.")
    parser.add_argument("--step-through", action="store_true",
                        help="In watch mode, wait for Enter between levels.")
    args = parser.parse_args()
    if args.step_through and not args.watch:
        parser.error("--step-through requires --watch.")
    levels = args.levels or list(range(1, 31))
    if args.failures_from:
        try:
            previous = json.loads(args.failures_from.read_text(encoding="utf-8"))
            levels = [row["level"] for row in previous if row["status"] != "cleared"]
        except (OSError, ValueError, TypeError, KeyError) as error:
            parser.error(f"Could not read benchmark results: {error}")
        if not levels:
            print("There are no failed attempts in the selected results.")
            return
    if (any(not isinstance(level, int) or not 1 <= level <= 30 for level in levels)
            or args.forecast_ticks < STEP):
        parser.error("Use levels 1–30 and at least three forecast ticks.")
    output = args.output
    if output is None and not args.watch and args.failures_from is None:
        output = ROOT / "level_benchmark.json"
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=not args.watch, timeout=15_000)
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, service_workers="block")
            env = RuffleEnv(page, ROOT / "ruffle-web" / "package", direct_reset=True, track_enemies=True)
            for index, level in enumerate(levels, 1):
                print(f"Testing level {level}/30", flush=True)
                if level <= 2:
                    env.level = level
                    result = (attempt(env, 0, pace=args.watch) if level == 1
                              else attempt_level_two(env, 0, pace=args.watch)[0])
                    result = {"status": "cleared" if result.cleared else "died" if result.died else "time limit",
                              "ticks": result.ticks}
                else:
                    result = attempt_screen_planner(env, level, args.forecast_ticks, watch=args.watch)
                results.append({"level": level, **result})
                if output:
                    output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
                print(f"  {result['status']}" + (f" ({result['reason']})" if result.get("reason") else ""),
                      flush=True)
                if args.watch:
                    if args.step_through and index < len(levels):
                        input("  Press Enter in this terminal for the next level. ")
                    else:
                        sleep(3)
        finally:
            browser.close()
    print(dict(Counter(result["status"] for result in results)), flush=True)


if __name__ == "__main__":
    main()
