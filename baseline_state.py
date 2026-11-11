"""Read-only ActionScript telemetry for baseline development."""

import struct
from functools import lru_cache
from urllib.parse import urlsplit

from direct_start import append_first_frame_action, start_at_level
from training_env import RuffleEnv


FIELDS = ("native_tick", "frame", "level", "coins", "deaths", "x", "y", "width", "height", "alpha", "ready")
PATHS = (
    ("__baselineTick",), ("_currentframe",), ("currentLevel",), ("currentCoins",), ("deaths",),
    ("player", "_x"), ("player", "_y"), ("player", "_width"), ("player", "_height"),
    ("player", "_alpha"), ("moveReady",),
)


def push(value):
    data = b"\0" + value.encode("ascii") + b"\0"
    return b"\x96" + struct.pack("<H", len(data)) + data


def number(value):
    return b"\x96\x05\0\x07" + struct.pack("<i", value)


def variable(name):
    return push(name) + b"\x1c"


def wall_scan_action():
    code = bytearray()
    labels = {}
    jumps = []

    def branch(opcode, target):
        jumps.append((len(code), target))
        code.extend(bytes([opcode]) + b"\x02\0\0\0")

    def assign(name, value, local=False):
        code.extend(push(name) + value + (b"\x3c" if local else b"\x1d"))

    def root(path):
        result = variable("_root")
        for member in path:
            result += push(member) + b"\x4e"
        return result

    # P is unused by the game's movement controls and requests one static scan.
    code.extend(number(80) + number(1) + variable("Key") + push("isDown") + b"\x52\x12")
    branch(0x9d, "done")
    code.extend(root(("__baselineWallsDone",)))
    branch(0x9d, "done")
    code.extend(variable("_root") + push("__baselineWallsDone") + number(1) + b"\x4f")
    assign("__by", root(("player", "_y")) + number(3) + b"\x3f", local=True)
    assign("__row", number(0), local=True)
    assign("__bx", number(0), local=True)
    assign("__col", number(0), local=True)
    assign("__line", push(""), local=True)
    labels["row"] = len(code)
    code.extend(variable("__row") + number(134) + b"\x48\x12")
    branch(0x9d, "done")
    assign("__bx", root(("player", "_x")) + number(3) + b"\x3f")
    assign("__col", number(0))
    assign("__line", push(""))
    labels["column"] = len(code)
    code.extend(variable("__col") + number(184) + b"\x48\x12")
    branch(0x9d, "row_end")
    code.extend(push("__line") + variable("__line"))
    code.extend(b"\x96\x02\0\x05\x01" + variable("__by") + variable("__bx")
                + number(3) + root(("walls",)) + push("hitTest") + b"\x52")
    branch(0x9d, "hit")
    code.extend(push("0"))
    branch(0x99, "append")
    labels["hit"] = len(code)
    code.extend(push("1"))
    labels["append"] = len(code)
    code.extend(b"\x47\x1d")
    assign("__bx", variable("__bx") + number(3) + b"\x47")
    assign("__col", variable("__col") + number(1) + b"\x47")
    branch(0x99, "column")
    labels["row_end"] = len(code)
    code.extend(push("WALLROW|") + variable("__row") + b"\x47" + push("|") + b"\x47"
                + variable("__line") + b"\x47\x26")
    assign("__by", variable("__by") + number(3) + b"\x47")
    assign("__row", variable("__row") + number(1) + b"\x47")
    branch(0x99, "row")
    labels["done"] = len(code)
    for position, target in jumps:
        code[position + 3:position + 5] = struct.pack("<h", labels[target] - position - 5)
    return bytes(code)


def hide_scene_action():
    code = bytearray()
    labels = {}
    jumps = []

    def branch(opcode, target):
        jumps.append((len(code), target))
        code.extend(bytes([opcode]) + b"\x02\0\0\0")

    code.extend(number(67) + number(1) + variable("Key") + push("isDown") + b"\x52\x12")
    branch(0x9d, "done")
    code.extend(variable("_root") + push("__baselineHidden") + b"\x4e")
    branch(0x9d, "done")
    code.extend(variable("_root") + push("__baselineHidden") + number(1) + b"\x4f")
    code.extend(push("__property") + b"\x96\x01\0\x03\x3c")
    code.extend(variable("_root") + b"\x55")
    labels["property"] = len(code)
    code.extend(push("__property") + b"\x4d\x1d")
    code.extend(variable("__property") + b"\x96\x01\0\x02\x49")
    branch(0x9d, "done")
    clip = variable("_root") + variable("__property") + b"\x4e"
    code.extend(clip + b"\x44" + push("movieclip") + b"\x49\x12")
    branch(0x9d, "property")
    code.extend(clip + push("_name") + b"\x4e" + push("enemies") + b"\x49")
    branch(0x9d, "property")
    code.extend(clip + push("_visible") + b"\x96\x02\0\x05\0\x4f")
    branch(0x99, "property")
    labels["done"] = len(code)
    for position, target in jumps:
        code[position + 3:position + 5] = struct.pack("<h", labels[target] - position - 5)
    return bytes(code)


@lru_cache(maxsize=30)
def instrumented_start(swf, level):
    one = b"\x96\x05\0\x07\x01\0\0\0"
    zero = b"\x96\x05\0\x07\0\0\0\0"
    body = (push("_root") + b"\x1c" + push("__baselineTick") + push("_root")
            + b"\x1c" + push("__baselineTick") + b"\x4e" + one + b"\x47\x4f")
    body += push("BASELINE")
    for path in PATHS:
        body += push("|") + b"\x47" + push("_root") + b"\x1c"
        for member in path:
            body += push(member) + b"\x4e"
        body += b"\x47"
    body += b"\x26" + wall_scan_action() + hide_scene_action() + b"\0"
    function = b"\0" + struct.pack("<HH", 0, len(body)) + body
    action = (push("_root") + b"\x1c" + push("__baselineTick") + zero + b"\x4f"
              + push("_root") + b"\x1c" + push("onEnterFrame")
              + b"\x9b" + struct.pack("<H", 5) + function + b"\x4f\0")
    return append_first_frame_action(start_at_level(swf, level), action)


class StateEnv(RuffleEnv):
    def _serve(self, route):
        path = urlsplit(route.request.url).path
        if self.direct_reset and path.startswith("/direct/"):
            level = int(path.removeprefix("/direct/").removesuffix(".swf"))
            route.fulfill(body=instrumented_start(self.swf, level),
                          content_type="application/x-shockwave-flash")
        else:
            super()._serve(route)

    def reset_state(self, level):
        self.reset(level)
        self.page.evaluate("""() => {
            window.baselineState = null;
            window.baselineWalls = [];
            document.querySelector("ruffle-player").ruffle().traceObserver = message => {
                if (message.startsWith("BASELINE|")) window.baselineState = message.split("|").slice(1);
                if (message.startsWith("WALLROW|")) {
                    const parts=message.split("|");
                    window.baselineWalls[Number(parts[1])]=parts[2];
                }
            };
        }""")
        self.step(ticks=1, observe=False)
        return self.state()

    def state(self):
        values = self.page.evaluate("window.baselineState")
        if values is None:
            raise RuntimeError("No ActionScript telemetry was received")
        result = dict(zip(FIELDS, values))
        for key in FIELDS[:-1]:
            try:
                result[key] = float(result[key])
            except ValueError:
                result[key] = None
        result["ready"] = result["ready"] == "true"
        return result

    def wall_grid(self):
        self.page.keyboard.down("p")
        try:
            self.step_native()
        finally:
            self.page.keyboard.up("p")
        rows = self.page.evaluate("window.baselineWalls")
        if len(rows) != 134 or any(len(row) != 184 for row in rows):
            raise RuntimeError("The SWF wall query did not produce the complete grid")
        return rows

    def step_native(self, keys=(), frames=1):
        self.set_keys(keys)
        self.last_step_ms = 0
        state = self.state()
        target = state["native_tick"] + frames
        for _ in range(frames * 4 + 4):
            if state["native_tick"] >= target:
                if state["native_tick"] != target:
                    raise RuntimeError("A virtual-clock advance skipped a native game frame")
                return state
            time = self.page.evaluate("performance.now()")
            delta = 16 - round(time) % 16
            self.page.clock.run_for(delta)
            self.last_step_ms += delta
            state = self.state()
        raise RuntimeError("The native game frame did not advance")
