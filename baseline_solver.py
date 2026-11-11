"""Plan and verify saved, non-evolutionary routes for the original 30 levels."""

import argparse
import base64
import hashlib
import json
from bisect import bisect_left
from collections import deque
from heapq import heappop, heappush
from io import BytesIO
from itertools import count
from pathlib import Path
from time import perf_counter, sleep

import numpy as np
from PIL import Image

from baseline_state import StateEnv
from benchmark_levels import PLANNER_DIRECTIONS, PLANNER_KEYS, STRIDE, layout
from direct_start import level_exits
from training_env import FPS, ROOT, RuffleEnv, add_browser_argument, game_page, launch_browser, sync_playwright


PIXEL_CAPTURE = """() => {
    const source = document.querySelector("ruffle-player").shadowRoot.querySelector("canvas");
    const canvas = document.createElement("canvas");
    canvas.width = 550; canvas.height = 400;
    const context = canvas.getContext("2d", {willReadFrequently:true});
    context.imageSmoothingEnabled = false;
    const frames = [], start = Number(window.baselineState[0]);
    let previous = "";
    function sample() {
        context.drawImage(source, 0, 0, 550, 400);
        const image = context.getImageData(0,26,550,348).data;
        const packed = new Uint8Array(Math.ceil(275*174/8));
        let cell = 0;
        for (let y=0; y<348; y+=2) {
            for (let x=0; x<550; x+=2,cell++) {
                const p=(y*550+x)*4, r=image[p],g=image[p+1],b=image[p+2];
                if ((r<30 && g<30 && b>210) ||
                    (r===100 && g===43 && b===187) || (r===201 && g===29 && b===29)) {
                    packed[cell>>3] |= 1 << (cell&7);
                }
            }
        }
        const encoded=btoa(String.fromCharCode(...packed));
        if (encoded!==previous) frames.push([Number(window.baselineState[0])-start,encoded]);
        previous=encoded;
    }
    sample();
    window.benchmarkCapture={sample,frames};
}"""


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def neighbors(cells):
    index = {cell: number for number, cell in enumerate(cells)}
    edges = []
    for x, y in cells:
        edges.append([(index[x + dx, y + dy], action)
                      for action, (dx, dy) in enumerate(PLANNER_DIRECTIONS)
                      if action and (x + dx, y + dy) in index])
    return edges


def dilate(mask, radius):
    horizontal = mask.copy()
    for offset in range(1, radius + 1):
        horizontal[:, offset:] |= mask[:, :-offset]
        horizontal[:, :-offset] |= mask[:, offset:]
    result = horizontal.copy()
    for offset in range(1, radius + 1):
        result[offset:] |= horizontal[:-offset]
        result[:-offset] |= horizontal[offset:]
    return result


def safe_positions(frames, cells, origin, ticks, radius, time_guard=1):
    positions = np.asarray(cells) * STRIDE + np.asarray(origin)
    probes = [(0, 0), (-9, 0), (9, 0), (0, -9), (0, 9)]
    coordinates = []
    for dx, dy in probes:
        x = np.floor((positions[:, 0] + dx) / 2).astype(int)
        y = np.floor((positions[:, 1] + dy - 26) / 2).astype(int)
        coordinates.append((np.clip(y, 0, 173), np.clip(x, 0, 274)))
    rendered = []
    for _, encoded in frames:
        packed = np.frombuffer(base64.b64decode(encoded), dtype=np.uint8)
        mask = np.unpackbits(packed, bitorder="little")[:275 * 174].reshape(174, 275).astype(bool)
        mask = dilate(mask, radius)
        blocked = np.zeros(len(cells), dtype=bool)
        for y, x in coordinates:
            blocked |= mask[y, x]
        rendered.append(~blocked)
    times = np.asarray([time for time, _ in frames])
    indices = np.clip(np.searchsorted(times, np.arange(ticks + 1), side="right") - 1, 0, len(frames) - 1)
    safe = np.asarray(rendered)[indices].T.copy()
    if time_guard:
        guarded = safe.copy()
        for delta in range(1, time_guard + 1):
            guarded[:, delta:] &= safe[:, :-delta]
            guarded[:, :-delta] &= safe[:, delta:]
        return guarded
    return safe


def safe_intervals(safe):
    result = []
    for row in safe:
        changes = np.diff(np.concatenate(([False], row, [False])).astype(np.int8))
        result.append(list(zip(np.flatnonzero(changes == 1).tolist(),
                               (np.flatnonzero(changes == -1) - 1).tolist())))
    return result


def distance_map(edges, targets):
    distances = {target: 0 for target in targets}
    pending = deque(targets)
    while pending:
        node = pending.popleft()
        for other, _ in edges[node]:
            if other not in distances:
                distances[other] = distances[node] + 1
                pending.append(other)
    return distances


def search(start_node, start_tick, targets, edges, intervals, *, solutions=1, limit=300_000,
           static_distances=None):
    distances = static_distances if static_distances is not None else distance_map(edges, targets)
    if start_node not in distances:
        return [], "static floor disconnects target"
    initial_interval = next((i for i, (begin, end) in enumerate(intervals[start_node])
                             if begin <= start_tick <= end), None)
    if initial_interval is None:
        return [], "start is unsafe in the recorded forecast"
    start = start_node, initial_interval
    arrivals = {start: start_tick}
    parents = {start: None}
    order = count()
    pending = [(start_tick + distances[start_node], next(order), start, start_tick)]
    found = []
    for _ in range(limit):
        if not pending:
            break
        _, _, state, tick = heappop(pending)
        if arrivals[state] != tick:
            continue
        node, interval = state
        if node in targets:
            moves = []
            cursor = state
            while parents[cursor] is not None:
                previous, action, departure = parents[cursor]
                moves.append((action, 1))
                wait = departure - arrivals[previous]
                if wait:
                    moves.append((0, wait))
                cursor = previous
            moves.reverse()
            found.append((node, tick, moves))
            if len(found) >= solutions:
                return found, None
        end = intervals[node][interval][1]
        for other, action in edges[node]:
            if other not in distances:
                continue
            other_intervals = intervals[other]
            first = bisect_left(other_intervals, (tick + 1,)) - 1
            for index in range(max(0, first), len(other_intervals)):
                begin, finish = other_intervals[index]
                arrival = max(tick + 1, begin)
                if arrival > end + 1:
                    break
                if arrival > finish:
                    continue
                next_state = other, index
                if arrival < arrivals.get(next_state, float("inf")):
                    arrivals[next_state] = arrival
                    parents[next_state] = state, action, arrival - 1
                    heappush(pending, (arrival + distances[other], next(order), next_state, arrival))
    return found, "safe interval search exhausted" if not pending else "search state budget exhausted"


def joint_pickup_search(start, targets, exits, edges, intervals, limit=2_000_000):
    """Keep pickup history in the search state for small, tightly coupled levels."""
    maps = [distance_map(edges, target) for target in targets]
    exit_map = distance_map(edges, exits)
    pickups = {}
    for bit, target in enumerate(targets):
        for node in target:
            pickups[node] = pickups.get(node, 0) | (1 << bit)
    full = (1 << len(targets)) - 1
    first = next((i for i, (a, b) in enumerate(intervals[start]) if a <= 0 <= b), None)
    if first is None:
        return [], "start is unsafe in the recorded forecast"
    initial = start, first, full & ~pickups.get(start, 0)
    arrivals, parents = {initial: 0}, {initial: None}
    order = count()
    def heuristic(node, remaining):
        return max([exit_map.get(node, float('inf'))] +
                   [maps[i].get(node, float('inf')) for i in range(len(targets)) if remaining & (1 << i)])
    pending = [(heuristic(start, initial[2]), next(order), initial, 0)]
    for _ in range(limit):
        if not pending:
            break
        _, _, state, tick = heappop(pending)
        if arrivals[state] != tick:
            continue
        node, interval, remaining = state
        if not remaining and node in exits:
            moves = []
            while parents[state] is not None:
                previous, action, departure = parents[state]
                moves.append((action, 1))
                if departure > arrivals[previous]:
                    moves.append((0, departure - arrivals[previous]))
                state = previous
            return list(reversed(moves)), None
        end = intervals[node][interval][1]
        for other, action in edges[node]:
            other_intervals = intervals[other]
            first = max(0, bisect_left(other_intervals, (tick + 1,)) - 1)
            for index in range(first, len(other_intervals)):
                begin, finish = other_intervals[index]
                arrival = max(tick + 1, begin)
                if arrival > end + 1:
                    break
                if arrival > finish:
                    continue
                following = other, index, remaining & ~pickups.get(other, 0)
                if arrival < arrivals.get(following, float('inf')):
                    arrivals[following] = arrival
                    parents[following] = state, action, arrival - 1
                    heappush(pending, (arrival + heuristic(other, following[2]), next(order), following, arrival))
    return [], "joint pickup search exhausted"


def make_plan(cells, edges, intervals, origin, pellets, exits, beam_width, pickup_radius=12):
    coordinates = np.asarray(cells) * STRIDE + origin
    targets = [{i for i, (x, y) in enumerate(coordinates)
                if abs(x - px) <= pickup_radius and abs(y - py) <= pickup_radius} for px, py in pellets]
    target_distances = [distance_map(edges, target) for target in targets]
    exit_nodes = {i for i, cell in enumerate(cells) if cell in exits}
    node_indices = {cell: index for index, cell in enumerate(cells)}
    pickups = {}
    for index, target in enumerate(targets):
        for node in target:
            pickups.setdefault(node, set()).add(index)
    start = cells.index((0, 0))
    if 1 <= len(pellets) <= 4:
        return joint_pickup_search(start, targets, exit_nodes, edges, intervals)
    # Branch over several pickup arrivals instead of committing to the first coin route.
    beam = [(start, 0, frozenset(range(len(pellets))), [])]
    best = beam[0]
    for _ in range(len(pellets)):
        if all(not state[2] for state in beam):
            break
        next_beam = []
        for node, tick, remaining, moves in beam:
            if not remaining:
                next_beam.append((node, tick, remaining, moves))
                continue
            choices = sorted(remaining, key=lambda index: target_distances[index].get(node, float("inf")))
            candidates = []
            for index in choices[:max(2, min(4, beam_width))]:
                found, _ = search(node, tick, targets[index], edges, intervals, solutions=2,
                                  static_distances=target_distances[index])
                candidates.extend(found)
            for destination, arrival, segment in candidates:
                collected = set(pickups.get(destination, ()))
                x, y = cells[node]
                for action, duration in segment:
                    dx, dy = PLANNER_DIRECTIONS[action]
                    for _ in range(duration):
                        x, y = x + dx, y + dy
                        collected.update(pickups.get(node_indices[x, y], ()))
                next_beam.append((destination, arrival, remaining - collected, moves + segment))
        if not next_beam:
            return best[3], "no further pellet route"
        next_beam.sort(key=lambda state: (len(state[2]), state[1]))
        unique = {}
        for state in next_beam:
            key = state[0], state[1], state[2]
            unique.setdefault(key, state)
        diversified = {}
        for state in unique.values():
            diversified.setdefault(state[2], state)
        selected = list(diversified.values())[:beam_width]
        for state in unique.values():
            if len(selected) >= beam_width:
                break
            if state not in selected:
                selected.append(state)
        beam = selected
        best = beam[0]
    complete = []
    exit_distances = distance_map(edges, exit_nodes)
    for node, tick, _, moves in beam:
        candidates, _ = search(node, tick, exit_nodes, edges, intervals, static_distances=exit_distances)
        for _, arrival, segment in candidates:
            complete.append((arrival, moves + segment))
    return (min(complete, key=lambda value: value[0])[1], None) if complete else (best[3], "no exit route")


def compress(moves):
    result = []
    for action, ticks in moves:
        if result and result[-1][0] == action:
            result[-1][1] += ticks
        else:
            result.append([action, ticks])
    return result


def validate_plan(cells, safe, moves):
    indices = {cell: index for index, cell in enumerate(cells)}
    x = y = tick = 0
    for action, duration in moves:
        dx, dy = PLANNER_DIRECTIONS[action]
        for _ in range(duration):
            x, y, tick = x + dx, y + dy, tick + 1
            if ((x, y) not in indices or tick >= safe.shape[1]
                    or not safe[indices[x, y], tick]):
                raise RuntimeError(f"Search reconstructed an unsafe move at tick {tick}")


def calibration(env, level, ticks, cache_dir):
    env.reset_state(level)
    env.reset_state(level)
    state = env.step_native()
    signature = env.observe().frame_signature
    origin = (state["x"], state["y"])
    key = hashlib.sha256(env.swf).hexdigest()[:12]
    cache_path = cache_dir / f"unoccluded-v2-level-{level}-{ticks}-{key}.json"
    data = {}
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached["initial"] == state and cached.get("signature") == signature:
            data = cached
    walls = data.get("walls")
    if walls is None:
        walls = env.wall_grid()
        env.reset_state(level)
        if env.step_native() != state:
            raise RuntimeError("The wall query changed the reset state")
    with Image.open(BytesIO(env.screenshot())) as image:
        tiles, pellets, exits = layout(image.convert("RGB"), origin,
                                      level_exits(env.swf)[level], wall_padding=0)
    start_x, start_y = int(origin[0] // STRIDE), int(origin[1] // STRIDE)
    wall_probes = ((0, 0), (-2, 0), (2, 0), (0, -2), (0, 2))
    tiles = {cell for cell in tiles
             if all(0 <= start_x + cell[0] + dx < 184 and 0 <= start_y + cell[1] + dy < 134
                    and walls[start_y + cell[1] + dy][start_x + cell[0] + dx] == "0"
                    for dx, dy in wall_probes)}
    exits &= tiles
    frames = data.get("frames")
    if frames is None:
        env.reset_state(level)
        env.page.keyboard.down("c")
        try:
            hidden_state = env.step_native()
        finally:
            env.page.keyboard.up("c")
        if hidden_state != state:
            raise RuntimeError("Hiding the observation graphics changed game state")
        if env.observe().player is not None:
            raise RuntimeError("The player remained visible in the enemy-only calibration")
        env.page.evaluate(PIXEL_CAPTURE)
        try:
            env.step(ticks=ticks + 3, observe=False)
        finally:
            frames = env.page.evaluate("() => { const frames=window.benchmarkCapture.frames; "
                                       "window.benchmarkCapture=null; return frames; }")
        after = env.state()
        if after["deaths"] != state["deaths"]:
            raise RuntimeError("Idle calibration died before the forecast ended")
    atomic_json(cache_path, {"initial": state, "signature": signature, "frames": frames, "walls": walls})
    return origin, tiles, pellets, exits, frames, state


def verify(env, level, moves, initial, *, watch=False):
    if env.page.url == "about:blank":
        env.reset_state(level)
    current = env.reset_state(level)
    startup_clocks = []
    while current["native_tick"] < initial["native_tick"]:
        current = env.step_native()
        startup_clocks.append((0, env.last_step_ms))
    if current != initial:
        raise RuntimeError("Calibration and replay start states differ")
    played = []
    clock_frames = list(startup_clocks)
    start = perf_counter()
    elapsed = 0
    expected = (current["x"], current["y"])
    drift = None
    total_ticks = sum(ticks for _, ticks in moves)
    points = [expected]
    for action, ticks in moves:
        dx, dy = PLANNER_DIRECTIONS[action]
        x, y = points[-1]
        points.append((x + dx * 3 * ticks, y + dy * 3 * ticks))
    env.watch_status(level=level, phase="Following route", detail="Replaying native-frame inputs.",
                     route=points, targets=[], progress=f"0 / {total_ticks} ticks")
    for action, ticks in [*moves, (0, 90)]:
        for _ in range(ticks):
            current = env.step_native(PLANNER_KEYS[action])
            clock_frames.append((action, env.last_step_ms))
            played.append((action, 1))
            elapsed += 1
            if watch:
                sleep(max(0, start + elapsed / FPS - perf_counter()))
                env.watch_status(tick=elapsed, player=(current["x"], current["y"])
                                 if current["x"] is not None else None,
                                 progress=f"{min(elapsed, total_ticks)} / {total_ticks} ticks")
            dx, dy = PLANNER_DIRECTIONS[action]
            expected = expected[0] + dx * 3, expected[1] + dy * 3
            if (drift is None and current["x"] is not None and current["y"] is not None
                    and max(abs(current["x"] - expected[0]), abs(current["y"] - expected[1])) > 0.1):
                drift = {"tick": elapsed, "expected": expected, "actual": (current["x"], current["y"])}
            current["first_drift"] = drift
            if current["level"] != initial["level"] or (level == 30 and current["frame"] == 52):
                env.watch_status(phase="Cleared", detail="The original game advanced past the level.",
                                 route=[], targets=[], clearTraces=True)
                return "cleared", compress(played), current, clock_frames
            if current["deaths"] != initial["deaths"] or not current["ready"]:
                env.watch_status(phase="Died", detail="The native collision state changed.", clearTraces=True)
                return "collision", compress(played), current, clock_frames
    env.watch_status(phase="Time limit", detail="The saved inputs ended without a confirmed clear.")
    return "incomplete", compress(played), current, clock_frames


def verify_original(env, level, clock_frames, finish_signature=None, *, watch=False):
    if env.page.url == "about:blank":
        env.reset(level)
    initial = env.reset(level)
    env.step(ticks=1, observe=False)  # The telemetry solver begins after this same extra frame.
    start = perf_counter()
    elapsed = 0
    for index, (action, milliseconds) in enumerate(clock_frames, 1):
        env.set_keys(PLANNER_KEYS[action])
        env.page.clock.run_for(milliseconds)
        elapsed += milliseconds
        if watch:
            sleep(max(0, start + elapsed / 1000 - perf_counter()))
        if index % 6 == 0:
            observation = env.observe()
            if observation.level_marker is not None and observation.level_marker != initial.level_marker:
                return True
            if observation.death_marker is not None and observation.death_marker != initial.death_marker:
                return False
    for _ in range(30):
        observation = env.step(ticks=3)
        if observation.level_marker is not None and observation.level_marker != initial.level_marker:
            return True
        if finish_signature is not None and observation.frame_signature == finish_signature:
            return True
        if observation.death_marker is not None and observation.death_marker != initial.death_marker:
            return False
    return False


def forbid_collision(safe, cells, origin, initial, final):
    if final.get("first_drift") is not None or final["x"] is None:
        return False
    tick = int(final["native_tick"] - initial["native_tick"])
    if tick >= safe.shape[1]:
        return False
    x = round((final["x"] - origin[0]) / STRIDE)
    y = round((final["y"] - origin[1]) / STRIDE)
    changed = False
    for index, (cx, cy) in enumerate(cells):
        if abs(cx - x) <= 1 and abs(cy - y) <= 1:
            start, end = max(0, tick - 2), min(safe.shape[1], tick + 3)
            changed |= bool(safe[index, start:end].any())
            safe[index, start:end] = False
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", type=int, nargs="+", default=list(range(1, 31)))
    parser.add_argument("--ticks", type=int, default=1200)
    parser.add_argument("--radii", type=int, nargs="+", default=[2, 3, 1])
    parser.add_argument("--time-guard", type=int, default=1)
    parser.add_argument("--beam", type=int, default=4)
    parser.add_argument("--pickup-radius", type=int, default=12)
    parser.add_argument("--repairs", type=int, default=6)
    parser.add_argument("--output", type=Path, default=ROOT / "baseline_routes.json")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--retry", action="store_true", help="Retry levels already verified as cleared.")
    parser.add_argument("--replay", action="store_true", help="Replay saved routes instead of solving.")
    parser.add_argument("--verify-original", action="store_true",
                        help="Verify saved clears against the SWF without telemetry.")
    add_browser_argument(parser)
    args = parser.parse_args()
    if (any(not 1 <= level <= 30 for level in args.levels) or args.ticks < 30 or args.beam < 1
            or args.time_guard < 0 or any(radius < 0 for radius in args.radii)
            or args.repairs < 0 or args.pickup_radius < 1):
        parser.error("Use levels 1–30, at least 30 forecast ticks, and a positive beam width.")
    cache = ROOT / ".baseline-cache"
    cache.mkdir(exist_ok=True)
    bank = json.loads(args.output.read_text()) if args.output.exists() else {"levels": {}}
    swf_hash = hashlib.sha256((ROOT / "WHGOriginal.swf").read_bytes()).hexdigest()
    if bank.get("swf_sha256", swf_hash) != swf_hash:
        parser.error("The saved routes belong to a different SWF.")
    bank.update({"format": "baseline_routes_v1", "swf_sha256": swf_hash})
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, browser=args.browser, headless=not args.watch)
        try:
            page = game_page(browser, watch=args.watch)
            env = StateEnv(page, ROOT / "ruffle-web" / "package", direct_reset=True, fit_window=args.watch)
            original = None
            if args.verify_original:
                original = RuffleEnv(game_page(browser), ROOT / "ruffle-web" / "package", direct_reset=True)
            for level in args.levels:
                saved = bank["levels"].get(str(level), {})
                if args.replay or args.verify_original or (args.watch and saved.get("status") == "cleared"):
                    if saved.get("status") != "cleared":
                        print(f"Level {level}: no cleared route saved", flush=True)
                        continue
                    status, played, state, clocks = verify(env, level, saved["moves"], saved["initial"],
                                                           watch=args.watch)
                    saved.update({"replay_status": status, "clock_frames": clocks})
                    if original:
                        saved["original_verified"] = False
                    if status == "cleared":
                        env.step_native(frames=3)
                        signature = env.observe().frame_signature if level == 30 else None
                        saved["finish_signature"] = signature
                        if original:
                            saved["original_verified"] = verify_original(original, level, clocks, signature)
                    print(f"Level {level}: replay {status}"
                          + (f", original SWF {saved.get('original_verified')}" if original else ""), flush=True)
                    atomic_json(args.output, bank)
                    if args.watch:
                        sleep(3)
                    continue
                if not args.retry and saved.get("status") == "cleared":
                    print(f"Level {level}: already cleared", flush=True)
                    continue
                print(f"Level {level}: calibrating {args.ticks} ticks", flush=True)
                origin, tiles, pellets, exits, frames, initial = calibration(env, level, args.ticks, cache)
                cells = sorted(tiles)
                edges = neighbors(cells)
                last = None
                history = list(saved.get("attempts", []))
                for radius in args.radii:
                    print(f"  Planning with shape margin {radius}", flush=True)
                    safe = safe_positions(frames, cells, origin, args.ticks, radius, args.time_guard)
                    tried_plans = set()
                    for repair in range(args.repairs + 1):
                        intervals = safe_intervals(safe)
                        moves, reason = make_plan(cells, edges, intervals, origin, pellets, exits,
                                                  args.beam, args.pickup_radius)
                        validate_plan(cells, safe, moves)
                        fingerprint = tuple(moves)
                        if fingerprint in tried_plans:
                            print("  The search returned the same route; trying the next margin", flush=True)
                            break
                        tried_plans.add(fingerprint)
                        print(f"  Planned {sum(t for _,t in moves)} ticks; {reason or 'complete route'}", flush=True)
                        status, played, state, clocks = verify(env, level, moves, initial, watch=args.watch)
                        if status == "collision":
                            (cache / f"collision-{level}.png").write_bytes(env.screenshot())
                        print(f"  {status}: coins {state['coins']}/{len(pellets)}, at {state['x']},{state['y']}", flush=True)
                        history.append({"status": status, "shape_margin": radius, "repair": repair,
                                        "reason": reason, "final": state})
                        last = {"status": status, "reason": reason, "shape_margin": radius,
                                "time_guard": args.time_guard, "pickup_radius": args.pickup_radius,
                                "beam_width": args.beam,
                                "forecast_ticks": args.ticks, "initial": initial, "final": state,
                                "moves": played, "clock_frames": clocks, "attempts": list(history)}
                        if saved.get("status") == "cleared" and status != "cleared":
                            saved["last_retry"] = {"status": status, "reason": reason, "final": state}
                        else:
                            bank["levels"][str(level)] = last
                        atomic_json(args.output, bank)
                        if (repair == args.repairs or status != "collision"
                                or (reason and sum(t for _, t in played) > sum(t for _, t in moves))
                                or not forbid_collision(safe, cells, origin, initial, state)):
                            break
                        print("  Replanning around the observed collision", flush=True)
                    if status == "cleared":
                        break
                if args.watch:
                    sleep(3)
        finally:
            browser.close()
    cleared = sorted(int(level) for level, result in bank["levels"].items() if result["status"] == "cleared")
    verified = sorted(int(level) for level, result in bank["levels"].items() if result.get("original_verified"))
    print(f"Saved clears: {len(cleared)}/30 {cleared}", flush=True)
    print(f"Original SWF verified: {len(verified)}/30 {verified}", flush=True)


if __name__ == "__main__":
    main()
