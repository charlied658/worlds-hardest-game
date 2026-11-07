from argparse import ArgumentParser
from pathlib import Path
from time import sleep

from PIL import Image, ImageChops


# Playfield bounds in the 1920x1080 captures from maximized Ruffle.
PLAYFIELD = (283, 112, 1637, 967)


def player_center(screenshot, playfield=PLAYFIELD):
    red, green, blue = screenshot.crop(playfield).convert("RGB").split()
    # Later levels can have dark-red hazards. The player's fill is pure red.
    red_mask = red.point(lambda value: 255 if value >= 245 else 0)
    green_mask = green.point(lambda value: 255 if value <= 25 else 0)
    blue_mask = blue.point(lambda value: 255 if value <= 25 else 0)
    mask = ImageChops.multiply(ImageChops.multiply(red_mask, green_mask), blue_mask)
    bounds = mask.getbbox()
    if bounds is None:
        return None

    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    red_pixels = mask.histogram()[255]
    # Reject scattered red pixels and shapes that don't resemble a filled square.
    if min(width, height) < 5 or max(width, height) > 1.25 * min(width, height):
        return None
    if red_pixels < 0.8 * width * height:
        return None

    return (left + right - 1) / 2, (top + bottom - 1) / 2


def capture_run(before_path, after_path):
    import pyautogui

    print("Click inside the game on your primary display within five seconds.", flush=True)
    sleep(5)

    pyautogui.screenshot(str(before_path))
    with pyautogui.hold("down"):
        sleep(0.75)
    with pyautogui.hold("right"):
        sleep(1)
    with pyautogui.hold("up"):
        sleep(0.75)
    pyautogui.screenshot(str(after_path))


def main():
    parser = ArgumentParser(description="Track the red square before and after a Ruffle replay.")
    parser.add_argument("--analyze-existing", action="store_true", help="Read the saved screenshots.")
    args = parser.parse_args()
    output_dir = Path(__file__).resolve().parent
    before_path = output_dir / "before.png"
    after_path = output_dir / "after.png"

    if not args.analyze_existing:
        capture_run(before_path, after_path)

    positions = []
    for path in (before_path, after_path):
        with Image.open(path) as screenshot:
            if screenshot.size != (1920, 1080):
                parser.exit(1, f"{path.name} must be 1920x1080 for the configured playfield.\n")
            position = player_center(screenshot)
        positions.append(position)
        if position is None:
            print(f"{path.name} | Red square not found in the configured playfield.")
        else:
            x, y = position
            print(f"{path.name} | Position ({x:.1f}, {y:.1f}) px within the playfield")

    before, after = positions
    if before is None or after is None:
        parser.exit(1, "Open level 1 in maximized Ruffle and check the screenshots.\n")
    dx = after[0] - before[0]
    dy = after[1] - before[1]
    print(f"Movement (dx, dy) = ({dx:+.1f}, {dy:+.1f}) px")


if __name__ == "__main__":
    main()
