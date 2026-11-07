# Training with the original SWF

The environment runs `WHGOriginal.swf` in a hidden Chromium, Google Chrome, or Microsoft Edge session using Ruffle Web. It advances a virtual browser clock at the game's native 30 fps and lets the original SWF handle movement, collisions, and level progression.

## First-time setup (macOS and Windows)

Use Python 3.10+ and run commands from the project folder. The original
`WHGOriginal.swf` must remain in that folder. Use a virtual environment if desired,
and install dependencies into the same Python interpreter used to run the scripts.

On macOS:

```sh
python3 -m pip install -r requirements-training.txt
python3 -m playwright install chromium
```

On Windows (PowerShell):

```powershell
python -m pip install -r requirements-training.txt
python -m playwright install chromium
```

Examples below use `python`; on macOS substitute `python3` if that is your Python
command. Playwright's Chromium installation is separate from installing the Python
package. If you only want to use an already installed Chrome or Edge, you can skip
`playwright install chromium` and pass the corresponding `--browser` option.

### Install Ruffle Web

The project needs Ruffle's **self-hosted web package**, not the desktop application
or browser extension. You do not need to open the SWF manually: the scripts serve
and load it automatically, without a separate web server.

The build tested here is `@ruffle-rs/ruffle` **0.7.1**. Use the same build on machines
whose results you want to compare. That version is available as a
[package archive](https://registry.npmjs.org/@ruffle-rs/ruffle/-/ruffle-0.7.1.tgz).
Alternatively, obtain a self-hosted build from [Ruffle downloads](https://ruffle.rs/downloads.html);
a different build may change rendering or gameplay behavior and needs validation.

Extract the archive and place the files in this layout, relative to the project
folder (the archive may already contain the `package` directory):

```text
worlds-hardest-game/
    WHGOriginal.swf
    training_env.py
    benchmark_levels.py
    ruffle-web/
        package/
            ruffle.js
            core.ruffle.<build-hash>.js
            <build-hash>.wasm
            ...other files from the same Ruffle package...
```

`ruffle.js` must be directly inside `ruffle-web/package`, not in another nested
`package` folder. Keep all accompanying JavaScript and WASM files together; do not
mix files from different builds. `ruffle-web/` is ignored by Git, so each teammate
must install it locally.

`training_env.py`, `evolve.py`, and `reactive.py` also support a custom location:

```sh
python training_env.py --browser chrome --ruffle-dir "/path/to/ruffle-files" --runs 3
```

On Windows, use a path such as `"C:\path\to\ruffle-files"`. The folder must contain
`ruffle.js`. `plan_level1.py` and `benchmark_levels.py` currently require the default
`ruffle-web/package` location.

### Choose a browser

All five browser-based commands (`training_env.py`, `evolve.py`, `reactive.py`,
`plan_level1.py`, and `benchmark_levels.py`) accept `--browser chromium|chrome|edge`.
The default is now Playwright's bundled **Chromium**; older versions of this project
always launched Edge. Chrome and Edge must already be installed when selected.

```sh
python training_env.py --browser chrome --runs 3
python evolve.py --browser chrome --watch best_replay.json
python benchmark_levels.py --browser edge --watch
```

Each run prints the browser name and version. A missing browser produces installation
guidance; the program does not silently choose a different browser. On your existing
Windows setup, pass `--browser edge` to keep using Edge without installing Chromium.

## Verify the setup and run the benchmark

Start with a short reset/replay test, then check a subset of the level benchmark:

```sh
python training_env.py --runs 3
python training_env.py --direct-reset --runs 3
python benchmark_levels.py --levels 1 2 13 --output local-check.json
python benchmark_levels.py --watch --levels 1 2 13
```

Append `--browser chrome` or `--browser edge` to each command as appropriate.
`local-check.json` is a separate local result file so this check does not overwrite
the committed `level_benchmark.json`. Watch mode does not save results unless you
supply `--output`.

Then run the full level benchmark, or a longer throughput test:

```sh
python benchmark_levels.py --watch
python training_env.py --runs 10
python training_env.py --level 30 --runs 3
```

`benchmark_levels.py` uses built-in route planners and does **not** load the output
of `evolve.py` or `reactive.py`. No training step is required before benchmarking.
Those training commands are separate experiments described below.

### Troubleshooting and comparing machines

- **Missing `ruffle-web/package`:** install the self-hosted web files in the layout
  above. Installing the desktop app alone is insufficient.
- **Missing browser executable:** run `python -m playwright install chromium`, or
  select an installed browser with `--browser chrome` or `--browser edge`.
- **Reset did not reach gameplay:** save the startup screen with
  `python training_env.py --browser chrome --inspect training-preview.png`.
  For a direct-start failure, include `--direct-reset` as well. Verify the Ruffle
  build and that the window shows the actual game.
- **Watch mode on Retina/high-DPI displays:** the game now renders at a fixed
  550 × 400 logical resolution and pixel density of 1. The window scales its visual
  presentation, and screenshots and obstacle forecasting use logical coordinates.
  Earlier code could fail player detection or predict obstacles incorrectly on a
  Retina display. These fixes also apply to Windows display scaling.

On the tested Mac with Chrome 156.0.8078.80 and Ruffle 0.7.1, levels 1, 2, and 13
cleared in both visible and headless runs. Levels 1 and 2 produced identical action
sequences between modes; level 13 took 31 moves in each. Resizing the window retained
the same canvas size and spawn coordinates. This is a subset check, not validation
of every level or a guarantee of identical results on other systems.

For comparisons, use the same Ruffle build, Python dependencies, browser/version,
reset mode, command options, and training seeds. Matching input sequences and game
outcomes are more useful than assuming screenshots or wall-clock throughput will
match across operating systems. Browser and OS details are currently printed or
recorded manually, not automatically included in benchmark JSON. Existing timing
figures below came from the original Edge setup and have not been revalidated on
other browsers.

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

The benchmark starts each level independently and saves its outcome in `level_benchmark.json`. Levels 1 and 2 use the tuned planners. For levels 3–30, it infers the floor and pellets from the screen and uses the last named checkpoint in the SWF to identify the exit. The exit can be the same region as spawn. The benchmark records enemy motion during an idle run and tests the resulting plan in a fresh run. It searches cardinal and diagonal moves, checks collisions every game tick, and searches for a reachable remaining pellet before routing to the exit. The default forecast covers 540 game ticks, including any rotations and reversals during that window. Use `--levels 3 4` to run a subset and `--output other_results.json` to keep it separate.

A clear requires the original game to show the next level number, including on a tip screen. A death is a collision confirmed by the death counter. If a search cannot find a route, the terminal identifies whether the inferred floor disconnects the target, the forecast is too short, enemy clearance blocks the explored routes, or the search hits its state limit. The final win screen would need a separate check for a level 30 clear.

To watch the failed levels one at a time:

```powershell
python benchmark_levels.py --watch --step-through --failures-from level_benchmark.json
```

The game renders at 550 × 400 with a logical pixel density of 1, including on Retina and other high-DPI displays. Watch mode enlarges that surface with CSS; resizing the window does not change simulation resolution or controller coordinates. Both screenshots and obstacle forecasting use the same logical coordinate system.

Watch mode reruns the planner. It plays any full or partial route in a visible window. Resize or maximize the browser window to enlarge the game. If the planner finds no route, it leaves the level at spawn and prints the reason in the terminal. Omit `--step-through` to move on automatically after three seconds. Watch mode saves results only when you pass `--output`.

## Connect a controller

Create a `RuffleEnv` with a fresh Playwright page using the viewport and setup in `training_env.py`.

- `reset()` starts a fresh copy of the SWF and returns the first observation. Use `reset(level=30)` to start at a different level through the game's level-select screen.
- `step(("ArrowRight",), ticks=6)` holds Right for 200 ms of simulated time and returns an observation. An empty tuple releases all directions. Keys remain held until the next action or reset. The tick count is expressed in native-rate time units. Ruffle Web still advances through browser animation callbacks.
- `step(..., observe=False)` skips screenshot capture when the controller needs no observation for that action.
- `observe()` returns the tick count, detected player center in stage pixels, and a fingerprint of the entire screen. When the player is visible, it also returns fingerprints of the level and death counter displays. Compare `level_marker` and `death_marker` across observations to detect changes. `screenshot()` gives the controller the raw PNG if it needs to locate hazards or read the HUD.

Each reset starts from level 1 by default. The original game controls level progression during a run. The controller supplies fitness and episode boundaries. The environment reports HUD changes through fingerprints. Exact counter values and win-screen detection still need decoding.

To inspect the starting screen or test a different menu path, use `--inspect training-preview.png` and add one `--click X Y TICKS` per startup click. Coordinates are relative to the 550 × 400 stage. The third number is the simulated time to advance after the click, measured in 30 fps ticks.
