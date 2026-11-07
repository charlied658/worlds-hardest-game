"""Build in-memory SWF variants that invoke the original level-select action."""

import re
import struct
import zlib
from functools import lru_cache


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

    # Append a DoAction tag to frame one, after the game's initialization.
    offset = (5 + 4 * (body[0] >> 3) + 7) // 8 + 4
    while offset < len(body):
        tag, = struct.unpack_from("<H", body, offset)
        if tag == 1 << 6:  # ShowFrame
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
