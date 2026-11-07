"""Fixed-clock replay of the original SWF in Ruffle Web."""

import argparse
from base64 import b64decode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import blake2b
from io import BytesIO
from pathlib import Path
from time import perf_counter, sleep
from urllib.parse import urlsplit

from PIL import Image, ImageChops
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from direct_start import start_at_level
from ruffle_smoke_test import player_center


ROOT = Path(__file__).resolve().parent
ORIGIN = "http://whg.local"
WIDTH, HEIGHT = 550, 400
FPS = 30
KEYS = ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight")
REPLAY = (((), 60), (("ArrowDown",), 12), (("ArrowRight",), 4))
LEVEL_MENU_CLICKS = ((275, 170, 45), (334, 285, 15))
HOST = f"""<!doctype html>
<style>
body {{ margin: 0; background: black; }}
ruffle-player {{ display: block; width: {WIDTH}px; height: {HEIGHT}px; }}
</style>
<script>
let generation = 0;
const requestFrame = window.requestAnimationFrame.bind(window);
window.requestAnimationFrame = callback => {{
    // A reset may leave a queued callback from a disposed Ruffle instance.
    const owner = generation;
    return requestFrame(time => {{
        if (owner === generation) callback(time);
    }});
}};
</script>
<script src="/ruffle/ruffle.js"></script>
<script>
window.addEventListener("DOMContentLoaded", () => {{
    const movies = new Map();
    let player;
    window.startMovie = async path => {{
      window.movieReady = false;
      window.movieError = null;
      generation++;
      if (player) player.remove();
      localStorage.clear();
      try {{
        if (!movies.has(path)) {{
            movies.set(path, fetch(path).then(response => response.arrayBuffer()));
        }}
        const data = await movies.get(path);
        player = window.RufflePlayer.newest().createPlayer();
        document.body.appendChild(player);
        await player.ruffle().load({{
            data, autoplay: "on", splashScreen: false,
            unmuteOverlay: "hidden", allowScriptAccess: false,
            preferredRenderer: "canvas",
        }});
        window.movieReady = true;
      }} catch (error) {{
        window.movieError = String(error);
      }}
    }};
}});
</script>"""


@dataclass(frozen=True)
class Observation:
    tick: int
    player: tuple[float, float] | None
    frame_signature: str
    level_marker: str | None
    death_marker: str | None
    enemies: tuple[tuple[float, float] | None, ...] = ()
    pellet: tuple[float, float] | None = None


def blue_mask(screenshot, bounds):
    red, green, blue = screenshot.crop(bounds).convert("RGB").split()
    low = [255 if value <= 55 else 0 for value in range(256)]
    high = [255 if value >= 180 else 0 for value in range(256)]
    return ImageChops.multiply(ImageChops.multiply(red.point(low), green.point(low)), blue.point(high))


def level_one_enemies(screenshot):
    # Level 1 has one blue enemy in each of these four horizontal lanes.
    left, top = 150, 150
    mask = blue_mask(screenshot, (left, top, 400, 250))
    centers = []
    for offset in range(0, 100, 25):
        bounds = mask.crop((0, offset, mask.width, offset + 25)).getbbox()
        if bounds is None:
            centers.append(None)
        else:
            x0, y0, x1, y1 = bounds
            centers.append((left + (x0 + x1 - 1) / 2, top + offset + (y0 + y1 - 1) / 2))
    return tuple(centers)


def level_two_objects(screenshot):
    left, top = 125, 125
    mask = blue_mask(screenshot, (left, top, 425, 275))
    enemies = []
    for offset in range(0, 300, 25):
        bounds = mask.crop((offset, 0, offset + 25, mask.height)).getbbox()
        if bounds is None:
            enemies.append(None)
        else:
            x0, y0, x1, y1 = bounds
            enemies.append((left + offset + (x0 + x1 - 1) / 2, top + (y0 + y1 - 1) / 2))

    # Level 2's pellet is stationary at the center of the arena.
    for red, green, blue in screenshot.crop((271, 196, 279, 204)).convert("RGB").getdata():
        if red >= 180 and green >= 180 and blue <= 55:
            return tuple(enemies), (275, 200)
    return tuple(enemies), None


def level_clicks(level):
    if not isinstance(level, int) or not 1 <= level <= 30:
        raise ValueError("level must be between 1 and 30")
    row, column = divmod(level - 1, 7)
    return (*LEVEL_MENU_CLICKS, (65 + column * 70, (96, 139, 184, 229, 274)[row], 75))


def image_signature(image):
    return blake2b(image.tobytes(), digest_size=8).hexdigest()


def is_level_tip(screenshot):
    # Interstitial tips retain the HUD and show a red GO button on a gradient.
    upper = screenshot.getpixel((0, 27))[0]
    middle = screenshot.getpixel((0, 200))[0]
    if middle - upper < 12:
        return False
    colors = screenshot.crop((249, 298, 302, 327)).convert("RGB").getcolors(53 * 29)
    return any(red >= 230 and green <= 60 and blue <= 60 for _, (red, green, blue) in colors)


def add_browser_argument(parser):
    parser.add_argument(
        "--browser", choices=("chromium", "chrome", "edge"), default="chromium",
        help="Browser to launch (default: chromium). Chrome and Edge must be installed.",
    )


def launch_browser(playwright, *, browser="chromium", headless=True):
    """Launch the selected Chromium browser without silently changing engines."""
    channels = {"chromium": None, "chrome": "chrome", "edge": "msedge"}
    options = {"headless": headless, "timeout": 15_000}
    if channels[browser] is not None:
        options["channel"] = channels[browser]
    try:
        launched = playwright.chromium.launch(**options)
    except PlaywrightError as exc:
        message = str(exc)
        if "executable doesn't exist" in message.lower() or "distribution" in message.lower() and "not found" in message.lower():
            hint = (
                "Install it with: python -m playwright install chromium"
                if browser == "chromium" else
                f"Install {'Google Chrome' if browser == 'chrome' else 'Microsoft Edge'}, "
                "or use --browser chromium after running: python -m playwright install chromium"
            )
            raise SystemExit(f"Browser '{browser}' is not installed. {hint}") from None
        raise
    print(f"Browser: {browser} {launched.version}", flush=True)
    return launched


def game_page(browser, *, watch=False):
    viewport = {"no_viewport": True} if watch else {"viewport": {"width": WIDTH, "height": HEIGHT}}
    page = browser.new_page(**viewport, service_workers="block")
    # Ruffle sizes its render buffer from devicePixelRatio. Keep the simulation
    # at one pixel per SWF coordinate even in a resizable Retina watch window.
    # CSS scaling below only changes presentation, never the render resolution.
    page.add_init_script("Object.defineProperty(window, 'devicePixelRatio', {get: () => 1});")
    return page


class RuffleEnv:
    def __init__(self, page, ruffle_dir, boot_clicks=None, *, direct_reset=False, level=1,
                 track_enemies=False, fit_window=False):
        self.page = page
        self.boot_clicks = level_clicks(level) if boot_clicks is None else boot_clicks
        self.direct_reset = direct_reset
        self.level = level
        self.track_enemies = track_enemies
        self.fit_window = fit_window
        self.swf = (ROOT / "WHGOriginal.swf").read_bytes() if direct_reset else None
        self.assets = {"/game.swf": ROOT / "WHGOriginal.swf"}
        self.assets.update(
            {f"/ruffle/{path.name}": path for path in ruffle_dir.iterdir()
             if path.is_file() and path.suffix in {".js", ".wasm"}}
        )
        self.keys = set()
        self.tick = 0
        self.page.route("**/*", self._serve)
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.page.clock.install(time=start)
        self.page.clock.pause_at(start + timedelta(seconds=10))

    def _serve(self, route):
        url = urlsplit(route.request.url)
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()
        elif url.path == "/":
            route.fulfill(body=HOST, content_type="text/html")
        elif self.direct_reset and url.path.startswith("/direct/"):
            level = int(url.path.removeprefix("/direct/").removesuffix(".swf"))
            route.fulfill(body=start_at_level(self.swf, level),
                          content_type="application/x-shockwave-flash")
        elif url.path in self.assets:
            content_type = "application/wasm" if url.path.endswith(".wasm") else None
            route.fulfill(path=self.assets[url.path], content_type=content_type)
        else:
            route.fulfill(status=404)

    def _advance(self, ticks):
        end = self.tick + ticks
        milliseconds = round(end * 1000 / FPS) - round(self.tick * 1000 / FPS)
        self.page.clock.run_for(milliseconds)
        self.tick = end

    def boot(self, level=None):
        selected_level = self.level if level is None else level
        level_clicks(selected_level)  # Validate before loading a direct-start variant.
        self.active_level = selected_level
        path = f"/direct/{selected_level}.swf" if self.direct_reset else "/game.swf"
        for key in KEYS:
            if key in self.keys:
                self.page.keyboard.up(key)
        self.keys.clear()
        if self.page.url == f"{ORIGIN}/":
            # Playwright schedules animation frames on a 16 ms grid.
            # Align each fresh player to the same phase on that grid.
            phase = round(self.page.evaluate("performance.now()")) % 16
            if phase:
                self.page.clock.run_for(16 - phase)
        else:
            self.page.goto(ORIGIN, wait_until="domcontentloaded", timeout=15_000)
            if self.fit_window:
                self.page.add_style_tag(content="""
                    ruffle-player {
                        transform-origin: top left;
                        transform: scale(var(--stage-scale, 1));
                    }
                    body { overflow: hidden; }
                """)
                self.page.evaluate("""([width, height]) => {
                    const fit = () => document.documentElement.style.setProperty(
                        "--stage-scale", Math.min(innerWidth / width, innerHeight / height));
                    addEventListener("resize", fit);
                    fit();
                }""", [WIDTH, HEIGHT])
        self.page.evaluate("path => { void window.startMovie(path); }", path)
        self.page.wait_for_function(
            "window.movieReady || window.movieError", polling=25, timeout=15_000
        )
        error = self.page.evaluate("window.movieError")
        if error:
            raise RuntimeError(f"Ruffle could not load the SWF: {error}")
        self.tick = 0
        self._advance(12)
        if self.direct_reset:
            self._click(5, HEIGHT - 5)  # Focus the stage outside the playfield.
        else:
            clicks = self.boot_clicks if level is None else level_clicks(level)
            for x, y, ticks in clicks:
                self._click(x, y)
                self._advance(ticks)
        self.tick = 0

    def _click(self, x, y):
        if self.fit_window:
            bounds = self.page.locator("ruffle-player").bounding_box()
            x = bounds["x"] + x * bounds["width"] / WIDTH
            y = bounds["y"] + y * bounds["height"] / HEIGHT
        self.page.mouse.click(x, y)

    def reset(self, level=None):
        self.boot(level)
        observation = self.observe()
        if observation.player is None:
            raise RuntimeError("Reset did not reach gameplay. Inspect the starting screen with --inspect.")
        return observation

    def step(self, keys=(), ticks=1, *, observe=True):
        keys = set(keys)
        if not keys.issubset(KEYS):
            raise ValueError(f"Use arrow keys from {KEYS}")
        if not isinstance(ticks, int) or ticks < 1:
            raise ValueError("ticks must be a positive integer")
        for key in KEYS:
            if key in self.keys and key not in keys:
                self.page.keyboard.up(key)
            elif key in keys and key not in self.keys:
                self.page.keyboard.down(key)
        self.keys = keys
        self._advance(ticks)
        return self.observe() if observe else None

    def screenshot(self):
        if self.fit_window:
            png = self.page.locator("ruffle-player canvas").evaluate(
                "canvas => canvas.toDataURL('image/png').split(',')[1]"
            )
            # Retina and other high-DPI displays use a larger canvas backing
            # buffer. Controllers always work in the SWF's logical coordinates,
            # independent of display density or the watch window's CSS scale.
            data = b64decode(png)
            with Image.open(BytesIO(data)) as frame:
                if frame.size == (WIDTH, HEIGHT):
                    return data
                normalized = frame.resize((WIDTH, HEIGHT), Image.Resampling.NEAREST)
                output = BytesIO()
                normalized.save(output, format="PNG")
                return output.getvalue()
        return self.page.screenshot(timeout=5_000)

    def observe(self):
        level_marker = death_marker = None
        enemies = ()
        pellet = None
        with Image.open(BytesIO(self.screenshot())) as screenshot:
            # Ignore the HUD and branding above and below the playfield.
            top = 27
            position = player_center(screenshot, (0, top, WIDTH, HEIGHT - 27))
            signature = image_signature(screenshot)
            if position is not None or is_level_tip(screenshot):
                level_marker = image_signature(screenshot.crop((225, 0, 325, top)))
                death_marker = image_signature(screenshot.crop((440, 0, WIDTH, top)))
            if position is not None:
                position = (position[0], position[1] + top)
                if self.track_enemies and self.active_level == 1:
                    enemies = level_one_enemies(screenshot)
                elif self.track_enemies and self.active_level == 2:
                    enemies, pellet = level_two_objects(screenshot)
        return Observation(self.tick, position, signature, level_marker, death_marker, enemies, pellet)


def benchmark(env, runs):
    start = perf_counter()
    reset_seconds = 0
    replay_seconds = 0
    outcomes = set()
    for run in range(1, runs + 1):
        print(f"Run {run}/{runs}", flush=True)
        phase_start = perf_counter()
        initial = env.reset()
        replay_start = perf_counter()
        reset_seconds += replay_start - phase_start
        for keys, ticks in REPLAY:
            env.step(keys, ticks, observe=False)
        final = env.observe()
        replay_seconds += perf_counter() - replay_start
        outcomes.add((initial, final))
    elapsed = perf_counter() - start
    print(f"{runs} trials in {elapsed:.2f} s ({runs / elapsed:.2f} trials/s), including resets.")
    simulated = runs * sum(ticks for _, ticks in REPLAY) / FPS
    print(f"{simulated / elapsed:.2f}× real-time gameplay, including resets.")
    print(f"Average reset: {reset_seconds / runs:.3f} s. "
          f"Average replay and observation: {replay_seconds / runs:.3f} s.")
    print(f"{len(outcomes)} distinct start/end frame pairs.")
    for outcome in sorted(outcomes, key=repr)[:5]:
        print(f"  {outcome[0]} -> {outcome[1]}")
    if all(end.player is None or end.player == initial.player for initial, end in outcomes):
        print("The replay produced no visible displacement on this level.")


def watch_replay(env, replay=REPLAY):
    initial = env.reset()
    print("Playing replay.", flush=True)
    start = perf_counter()
    for keys, ticks in replay:
        for _ in range(ticks):
            env.step(keys, observe=False)
            sleep(max(0, start + env.tick / FPS - perf_counter()))
    final = env.observe()
    print(f"Player position: {initial.player} -> {final.player}", flush=True)
    sleep(3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ruffle-dir", type=Path, default=ROOT / "ruffle-web" / "package")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--level", type=int, choices=range(1, 31), default=1, metavar="1-30")
    parser.add_argument("--direct-reset", action="store_true",
                        help="Start at the selected level using an in-memory SWF variant.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--inspect", type=Path, help="Save a screenshot after the startup sequence.")
    output.add_argument("--watch", action="store_true", help="Watch one replay in a visible window.")
    parser.add_argument("--click", nargs=3, type=int, action="append", metavar=("X", "Y", "TICKS"),
                        help="Override startup clicks. Repeat to add more clicks.")
    add_browser_argument(parser)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    if not (args.ruffle_dir / "ruffle.js").is_file():
        parser.error("Point --ruffle-dir at the self-hosted Ruffle directory containing ruffle.js.")
    if args.click and any(not (0 <= x < WIDTH and 0 <= y < HEIGHT and ticks >= 1)
                          for x, y, ticks in args.click):
        parser.error("Each click needs stage coordinates and a positive tick count.")
    if args.click and args.direct_reset:
        parser.error("--click is only available for menu-based resets.")

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, browser=args.browser, headless=not args.watch)
        try:
            page = game_page(browser, watch=args.watch)
            env = RuffleEnv(page, args.ruffle_dir, args.click,
                            direct_reset=args.direct_reset, level=args.level, fit_window=args.watch)
            if args.inspect:
                env.boot()
                args.inspect.write_bytes(env.screenshot())
                print(f"Saved {args.inspect.resolve()}")
            elif args.watch:
                watch_replay(env)
            else:
                benchmark(env, args.runs)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
