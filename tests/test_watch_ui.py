"""Integration checks: WHG_TEST_BROWSER=chrome python -m unittest discover -s tests."""

import os
import unittest

from benchmark_levels import CAPTURE
from training_env import ROOT, RuffleEnv, game_page, launch_browser, sync_playwright


class WatchDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "ruffle-web/package/ruffle.js").exists():
            raise unittest.SkipTest("Install the self-hosted Ruffle package first")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = launch_browser(
                cls.playwright, browser=os.environ.get("WHG_TEST_BROWSER", "chromium")
            )
        except BaseException:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = game_page(self.browser, watch=True)
        self.env = RuffleEnv(self.page, ROOT / "ruffle-web/package",
                             direct_reset=True, track_enemies=True, fit_window=True)
        self.env.reset()

    def tearDown(self):
        self.page.context.close()

    def hazards(self):
        self.page.evaluate(CAPTURE)
        return self.page.evaluate("""() => {
            const boxes = window.benchmarkCapture.frames[0][1];
            window.benchmarkCapture = null;
            return boxes;
        }""")

    def test_overlays_and_resizing_preserve_game_pixels_and_hazards(self):
        before = self.env.screenshot()
        boxes = self.hazards()
        self.env.watch_status(phase="Following route", detail="Overlay isolation test",
                              route=[[87, 200], [87, 265], [162, 265]],
                              targets=[[162, 265]], tick=1, player=[87, 200],
                              enemies=[[175, 188]])
        self.page.locator("#show-player").check()
        self.page.locator("#show-enemies").check()
        self.assertGreater(self.page.locator("#watch-overlay > *").count(), 0)
        for width, height in ((640, 850), (1200, 750)):
            self.page.set_viewport_size({"width": width, "height": height})
            self.assertEqual(self.env.screenshot(), before)
            self.assertEqual(self.hazards(), boxes)
            self.assertTrue(self.page.locator("#watch-panel").is_visible())
        self.assertEqual(self.page.locator("ruffle-player canvas").evaluate(
            "c => [c.width, c.height]"), [550, 400])
        # Toggling diagnostics must return keyboard control to the game.
        moved = self.env.step(("ArrowRight",), ticks=6)
        self.assertIsNotNone(moved.player)
        self.assertGreater(moved.player[0], 87)

    def test_frame_hook_matches_separate_animation_recorder(self):
        recordings = []
        for separate_timer in (True, False):
            initial = self.env.reset(3)
            self.page.evaluate(CAPTURE)
            if separate_timer:
                self.page.evaluate("""() => {
                    const capture = window.benchmarkCapture;
                    const sample = capture.sample;
                    delete capture.sample;
                    function frame() {
                        sample();
                        capture.id = requestAnimationFrame(frame);
                    }
                    capture.id = requestAnimationFrame(frame);
                }""")
            self.env.step(ticks=30, observe=False)
            frames = self.page.evaluate("""() => {
                const capture = window.benchmarkCapture;
                if (capture.id !== undefined) cancelAnimationFrame(capture.id);
                window.benchmarkCapture = null;
                return capture.frames;
            }""")
            recordings.append((initial.frame_signature, frames))
        self.assertGreater(len(recordings[0][1]), 30)
        self.assertEqual(recordings[0], recordings[1])

    def test_preparation_cover_is_optional_and_does_not_change_game(self):
        before = self.env.screenshot()
        self.env.watch_status(phase="Observing enemies", detail="Recording enemy movement.")
        self.assertTrue(self.page.locator("#show-preparation").is_checked())
        self.assertTrue(self.page.locator("#watch-preparing").is_hidden())
        self.page.locator("#show-preparation").uncheck()
        self.assertTrue(self.page.locator("#watch-preparing").is_visible())
        self.assertEqual(self.env.screenshot(), before)
        self.env.watch_status(phase="Following route", detail="Playing the route.")
        self.assertTrue(self.page.locator("#watch-preparing").is_hidden())
        self.assertEqual(self.env.screenshot(), before)

    def test_final_banner_uses_traffic_lights_without_changing_game_pixels(self):
        before = self.env.screenshot()
        for phase, tone in (("Cleared", "green"), ("Died", "yellow"),
                            ("Partial route", "yellow"), ("Time limit", "yellow"),
                            ("Exit not confirmed", "yellow"), ("No route", "red"),
                            ("Unmodeled", "red")):
            self.env.watch_status(phase=phase, detail="Outcome explanation.")
            banner = self.page.locator("#watch-result")
            self.assertTrue(banner.is_visible())
            self.assertEqual(banner.get_attribute("data-tone"), tone)
            self.assertEqual(self.page.locator("#watch-result-title").inner_text(), phase)
            self.assertEqual(self.env.screenshot(), before)
        self.env.watch_status(phase="Following partial route")
        self.assertTrue(self.page.locator("#watch-result").is_hidden())
        self.env.watch_status(phase="No route")
        self.env.reset()
        self.assertTrue(self.page.locator("#watch-result").is_hidden())

    def test_phase_is_on_game_and_grid_retains_level_results(self):
        self.assertEqual(self.page.locator("#watch-levels .level-cell").count(), 30)
        self.assertEqual(self.page.locator('.level-cell[data-tone="grey"]').count(), 30)
        self.env.watch_status(level=1, phase="Cleared", detail="Confirmed clear.")
        self.env.watch_status(level=2, phase="Died", detail="A partial run.")
        self.env.watch_status(level=3, phase="No route", detail="No safe route found.")
        for level, tone in ((1, "green"), (2, "yellow"), (3, "red"), (4, "grey")):
            self.assertEqual(self.page.locator(f'.level-cell[data-level="{level}"]').get_attribute("data-tone"), tone)
        self.assertEqual(self.page.locator('.level-cell[aria-current="true"]').inner_text(), "3")
        self.assertEqual(self.page.locator("#watch-panel #watch-phase").count(), 0)
        self.assertEqual(self.page.locator("#game-area #watch-phase").count(), 1)
        self.env.watch_status(level=4, phase="Observing enemies")
        self.assertEqual(self.page.locator('.level-cell[data-level="1"]').get_attribute("data-tone"), "green")
        self.assertTrue(self.page.locator("#watch-result").is_hidden())

    def test_clear_freezes_original_level_but_detection_reads_live_game(self):
        self.env.watch_status(phase="Running controller")
        self.env.step(("ArrowRight",), ticks=6)
        old_frame = self.env.screenshot()
        # Emulate the SWF switching screens after a final gameplay frame.
        self.page.evaluate("""() => {
            const c=document.querySelector('ruffle-player').shadowRoot.querySelector('canvas');
            const ctx=c.getContext('2d'); ctx.fillStyle='black';ctx.fillRect(0,0,550,400);
        }""")
        # The display must freeze immediately, before Python confirms a clear.
        self.page.evaluate("window.watchUI.sampleFrame()")
        self.assertTrue(self.page.locator("#watch-frozen").is_visible())
        self.assertEqual(self.page.locator("#watch-phase").inner_text(), "Confirming outcome")
        self.assertTrue(self.page.locator("#watch-result").is_hidden())
        self.assertEqual(self.page.locator('.level-cell[data-level="1"]').get_attribute("data-tone"), "grey")
        self.env.watch_status(phase="Cleared", detail="Level transition confirmed.")
        self.assertTrue(self.page.locator("#watch-frozen").is_visible())
        frozen = self.page.locator("#watch-frozen").evaluate("c=>c.toDataURL('image/png').split(',')[1]")
        from base64 import b64decode
        self.assertEqual(b64decode(frozen), old_frame)
        self.assertNotEqual(self.env.screenshot(), old_frame)
        self.env.reset(2)
        self.assertTrue(self.page.locator("#watch-frozen").is_hidden())

    def test_transition_hold_recovers_without_claiming_a_clear(self):
        self.env.watch_status(phase="Running controller")
        self.page.evaluate("""() => {
            const c=document.querySelector('ruffle-player').shadowRoot.querySelector('canvas');
            window.originalGameFrame=c.getContext('2d').getImageData(0,0,550,400);
            c.getContext('2d').clearRect(0,0,550,400);
            window.watchUI.sampleFrame();
        }""")
        self.assertTrue(self.page.locator("#watch-frozen").is_visible())
        self.page.evaluate("""() => {
            document.querySelector('ruffle-player').shadowRoot.querySelector('canvas').getContext('2d').putImageData(window.originalGameFrame,0,0);
            window.watchUI.sampleFrame();
        }""")
        self.assertTrue(self.page.locator("#watch-frozen").is_hidden())
        self.assertTrue(self.page.locator("#watch-result").is_hidden())
        self.env.watch_status(phase="Died")
        self.assertTrue(self.page.locator("#watch-frozen").is_hidden())
        self.assertEqual(self.page.locator("#watch-result").get_attribute("data-tone"), "yellow")

    def test_result_and_browser_advance(self):
        self.env.watch_status(phase="No route", detail="Enemy clearance blocks the route.",
                              waiting=True)
        self.assertEqual(self.page.locator("#watch-phase").inner_text(), "No route")
        self.assertIn("Enemy clearance", self.page.locator("#watch-detail").inner_text())
        self.page.locator("#watch-next").click()
        self.assertTrue(self.page.evaluate("window.watchUI.next"))
        self.env.watch_status(waiting=False)
        self.assertTrue(self.page.locator("#watch-next").is_hidden())


if __name__ == "__main__":
    unittest.main()
