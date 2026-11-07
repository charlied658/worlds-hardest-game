# Training with the original SWF

The environment runs `WHGOriginal.swf` in a hidden Microsoft Edge session using Ruffle Web. It advances a virtual browser clock at the game's native 30 fps and lets the original SWF handle movement, collisions, and level progression.

## Run the benchmark

From this folder, with Python 3.10+ and Microsoft Edge installed:

```powershell
python -m pip install -r requirements-training.txt
python training_env.py --runs 10
python training_env.py --level 30 --runs 3
```

The local Ruffle Web package is in `ruffle-web/package` (version 0.7.1). If that folder is missing, download the [self-hosted Ruffle build](https://ruffle.rs/downloads.html), extract it, and pass the folder containing `ruffle.js` and its accompanying JavaScript and WASM files:

```powershell
python training_env.py --ruffle-dir 'C:\path\to\ruffle' --runs 10
```

The benchmark reports trials per second, average reset and replay times, and the number of distinct start/end screen pairs. Throughput includes resets and screenshots. The first trial includes browser-side Ruffle initialization. Matching frame pairs indicate repeatability for this replay.

On this machine, 10 level 1 replays produced identical start and end frames in 27.5 seconds. Each replay represented 2.53 seconds of gameplay. The average reset took 1.95 seconds and the average replay plus observation took 0.80 seconds.

## Faster resets for batch training

```powershell
python training_env.py --direct-reset --runs 10
python training_env.py --direct-reset --level 30 --runs 3
```

Use `RuffleEnv(page, ruffle_dir, direct_reset=True)` to select this mode from Python. It builds an in-memory variant that invokes the game's existing level-select action on the first SWF frame. The original file on disk is preserved. Direct resets start at an earlier point in the obstacle cycle, so use the same reset mode for all candidates you compare. The default menu-based mode remains available for validation.

After one warm-up reset, 10 direct-start replays took 9.83 seconds and produced identical start and end frames. The average reset took 0.184 seconds. Direct starts returned a visible player and a distinct HUD level marker for all 30 levels.

## Run a longer benchmark or watch a replay

```powershell
python training_env.py --direct-reset --runs 1000
python training_env.py --direct-reset --watch
python training_env.py --watch
```

The first command benchmarks the same built-in input sequence 1,000 times. The second shows one paced replay at the direct-start obstacle phase. The third shows the replay from the original SWF's menu-based start. The watch window closes three seconds after the sequence ends.

The benchmark is a throughput test. Use the evolutionary trainer below to try different input sequences.

## Evolve a level 1 controller

```powershell
python evolve.py
python evolve.py --watch best_replay.json
```

The default trainer keeps the 60 ticks of opening movement fixed. It evolves the pause at the safe corner before entering the corridor and the moves after entry. Mutations change direction or duration, insert waits, or remove moves. Each candidate has the same tick budget, including the opening, and the trainer samples the game at least every six ticks during its moves. Each generation keeps the highest-scoring candidates, mutates copies, and adds one random candidate.

Fitness gives credit for the farthest ordered checkpoint reached and partial credit for distance toward the next one. Survival breaks progress ties, so waiting at spawn cannot beat an attempt that moves farther. A detected level advance outranks any partial run and ends training.

The best replay is saved as soon as it improves. The default output is `best_replay.json` in this project folder. Use `--output` to keep separate experiments. The replay stores the starting level, reset mode, and actual input sequence, so you can watch it after training or after stopping a batch early.

For a shorter first experiment:

```powershell
python evolve.py --population 6 --generations 3 --actions 30 --output first_replay.json
python evolve.py --watch first_replay.json
```

Use `--strategy classic` to compare the old six-tick search against the new crossing search under the same fitness function. For a matched comparison, use the same `--seed`, `--actions`, and `--evaluations` for both and allow enough generations to hit the evaluation cap. The terminal counts actual game evaluations and labels cached repeats. A level clear ends either run early.

Level 1 has no pellets. On collection levels, a fitness function should count distinct pellets collected in the current attempt and give progress credit toward a remaining pellet. Once all required pellets are collected, use progress toward the exit. The game’s level-advance signal remains the completion criterion.

## Evolve a reactive level 1 policy

```powershell
python reactive.py --population 6 --generations 4 --baseline crossing_replay.json
python reactive.py --watch reactive_policy.json --delay 18
```

Omit `--baseline crossing_replay.json` if you do not have a saved fixed replay to compare. The reactive policy uses the fixed opening to reach the safe corner, then chooses a direction or waits every six ticks based on the next waypoint and the blue enemies' estimated motion. Training uses three starting delays per candidate. The final comparison uses three different delays and gives the fixed replay and the policy the same tick budget.

The trainer saves the best weights as soon as they improve. Watch mode runs the saved policy live. Use different `--delay` values to see how it handles different enemy positions. Enemy tracking uses the four horizontal lanes on level 1.

## Level 1 route-planning baseline

```powershell
python plan_level1.py
python plan_level1.py --watch --delay 18
python plan_level1.py --level 2
python plan_level1.py --level 2 --watch --delay 18
```

The planner estimates the enemies' current phase from two screenshots, predicts their bounces, and searches for a route through a hand-coded map of level 1. It checks the next move against live observations every three ticks. In the initial test, it cleared all three held-out starting delays in the original SWF. This is a hand-built baseline for comparison with the evolved controller.

Level 2 uses the same search with a different map and a forecast for the twelve vertical enemy lanes. The search requires collecting the yellow pellet before reaching the exit. The terminal reports pellet collection and whether the game advanced to level 3.

### Full level benchmark

```powershell
python benchmark_levels.py
```

The benchmark starts each level independently and saves its outcome in `level_benchmark.json`. Levels 1 and 2 use the tuned planners. For levels 3–30, the benchmark infers a tile map, pellets, and green zones from the screen, records enemy motion during an idle run, and tests the resulting plan in a fresh run. The default forecast covers 300 game ticks. Use `--levels 3 4` to run a subset and `--output other_results.json` to keep it separate.

A clear requires the original game to show the next level. A death is a collision confirmed by the death counter. The other outcomes distinguish a missing route within the search budget, a partial route, an unconfirmed exit, or a layout the screen-based planner could not model. The final win screen would need a separate check for a level 30 clear.

To watch the failed levels one at a time:

```powershell
python benchmark_levels.py --watch --step-through --failures-from level_benchmark.json
```

Watch mode reruns the planner. It plays any full or partial route in a visible window. If the planner finds no route, it leaves the level at spawn and prints the reason in the terminal. Omit `--step-through` to move on automatically after three seconds. Watch mode saves results only when you pass `--output`.

## Connect a controller

Create a `RuffleEnv` with a fresh Playwright page using the viewport and setup in `training_env.py`.

- `reset()` starts a fresh copy of the SWF and returns the first observation. Use `reset(level=30)` to start at a different level through the game's level-select screen.
- `step(("ArrowRight",), ticks=6)` holds Right for 200 ms of simulated time and returns an observation. An empty tuple releases all directions. Keys remain held until the next action or reset. The tick count is expressed in native-rate time units. Ruffle Web still advances through browser animation callbacks.
- `step(..., observe=False)` skips screenshot capture when the controller needs no observation for that action.
- `observe()` returns the tick count, detected player center in stage pixels, and a fingerprint of the entire screen. When the player is visible, it also returns fingerprints of the level and death counter displays. Compare `level_marker` and `death_marker` across observations to detect changes. `screenshot()` gives the controller the raw PNG if it needs to locate hazards or read the HUD.

Each reset starts from level 1 by default. The original game controls level progression during a run. The controller supplies fitness and episode boundaries. The environment reports HUD changes through fingerprints. Exact counter values and win-screen detection still need decoding.

To inspect the starting screen or test a different menu path, use `--inspect training-preview.png` and add one `--click X Y TICKS` per startup click. Coordinates are relative to the 550 × 400 stage. The third number is the simulated time to advance after the click, measured in 30 fps ticks.
