"""Build in-memory SWF variants that invoke the original level-select action."""

import re
import struct
import zlib
from functools import lru_cache


def matrix_translation(data):
    bits = "".join(f"{byte:08b}" for byte in data)
    offset = 0

    def read(size, *, signed=False):
        nonlocal offset
        field = bits[offset:offset + size]
        offset += size
        if not field:
            return 0
        value = int(field, 2)
        return value - (1 << size) if signed and field[0] == "1" else value

    for _ in range(2):  # Optional scale and rotation fields.
        if read(1):
            size = read(5)
            offset += 2 * size
    size = read(5)
    return read(size, signed=True) / 20, read(size, signed=True) / 20


@lru_cache(maxsize=1)
def level_exits(swf):
    """Read the last named checkpoint in each level's initial SWF frame."""
    body = zlib.decompress(swf[8:])
    offset = (5 + 4 * (body[0] >> 3) + 7) // 8 + 4
    level = None
    checkpoints = {}
    while offset < len(body):
        tag, = struct.unpack_from("<H", body, offset)
        code, size = tag >> 6, tag & 63
        offset += 2
        if size == 63:
            size, = struct.unpack_from("<I", body, offset)
            offset += 4
        data = body[offset:offset + size]
        offset += size
        if code == 43:  # FrameLabel
            match = re.fullmatch(rb"L(\d+)\0", data)
            level = int(match[1]) if match else None
        elif code == 1:  # ShowFrame
            level = None
        elif code == 26 and level is not None and data[:1] == b"\x36":
            # PlaceObject2 with a character, matrix, ratio, and name.
            match = re.search(rb"check(\d+)\0$", data)
            if match:
                number = int(match[1])
                if level not in checkpoints or number > checkpoints[level][0]:
                    checkpoints[level] = (number, matrix_translation(data[5:]))
    return {level: point for level, (_, point) in checkpoints.items()}


@lru_cache(maxsize=30)
def start_at_level(swf, level):
    if swf[:4] != b"CWS\x08" or not 1 <= level <= 30:
        raise ValueError("Expected the original compressed Flash 8 game and level 1–30")
    body = zlib.decompress(swf[8:])
    # The level buttons share a constant pool and use the same action sequence.
    pool = (b"\x06\0_root\0resetA\0currentLevel\0justCameSelect\0"
            + f"L{level}".encode() + b"\0gotoAndStop\0")
    prefix = b"\x88" + struct.pack("<H", len(pool)) + pool
    matches = list(re.finditer(re.escape(prefix), body))
    if len(matches) != 1:
        raise ValueError(f"Could not identify the original action for level {level}")
    start = matches[0].start()
    end = matches[0].end()
    while body[end]:
        opcode = body[end]
        end += 1
        if opcode >= 0x80:
            size, = struct.unpack_from("<H", body, end)
            end += 2 + size
    action = body[start:end + 1]

    return append_first_frame_action(swf, action)


def append_first_frame_action(swf, action):
    body = zlib.decompress(swf[8:])
    offset = (5 + 4 * (body[0] >> 3) + 7) // 8 + 4
    while offset < len(body):
        tag, = struct.unpack_from("<H", body, offset)
        if tag == 1 << 6:
            break
        size = tag & 63
        offset += 2
        if size == 63:
            size, = struct.unpack_from("<I", body, offset)
            offset += 4
        offset += size
    else:
        raise ValueError("Could not locate the first SWF frame")
    tag = struct.pack("<HI", (12 << 6) | 63, len(action))
    body = body[:offset] + tag + action + body[offset:]
    return swf[:4] + struct.pack("<I", len(body) + 8) + zlib.compress(body)
