#!/usr/bin/env python3
"""Minimal Flashback replay inspector (container level only; packets are not decoded).

Usage: python3 flashback_inspect.py replay.zip
"""
import json, struct, sys, zipfile

MAGIC = 0xD780E884

def read_varint(b, i):
    value = shift = 0
    while True:
        byte = b[i]; i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift >= 35:
            raise ValueError("VarInt too big")

def read_string(b, i):
    n, i = read_varint(b, i)
    return b[i:i + n].decode("utf-8"), i + n

def parse_chunk(b):
    i = 0
    magic, = struct.unpack_from(">I", b, i); i += 4
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic:#x}")
    count, i = read_varint(b, i)
    table = []
    for _ in range(count):
        name, i = read_string(b, i)
        table.append(name)
    snap_size, = struct.unpack_from(">i", b, i); i += 4
    snap_end = i + snap_size
    actions = []  # (in_snapshot, name, payload_bytes)
    while i < len(b):
        in_snap = i < snap_end
        aid, i = read_varint(b, i)
        size, = struct.unpack_from(">i", b, i); i += 4
        actions.append((in_snap, table[aid], b[i:i + size]))
        i += size
    return table, actions

def main(path):
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("metadata.json"))
        print("name:", meta.get("name"), "| uuid:", meta.get("uuid"))
        print("version:", meta.get("version_string"), "protocol", meta.get("protocol_version"),
              "data", meta.get("data_version"), "| total_ticks:", meta.get("total_ticks"))
        print("entries:", z.namelist())
        tick = 0
        for name, cm in meta["chunks"].items():
            table, actions = parse_chunk(z.read(name))
            counts = {}
            ticks = 0
            for in_snap, a, payload in actions:
                key = ("snapshot:" if in_snap else "") + a.split("/")[-1]
                counts[key] = counts.get(key, 0) + 1
                if a == "flashback:action/next_tick":
                    ticks += 1
            print(f"\n{name}: starts at tick {tick}, duration {cm['duration']} "
                  f"(counted next_tick={ticks}), forcePlaySnapshot={cm.get('forcePlaySnapshot')}")
            print("  action table:", table)
            for k, v in sorted(counts.items()):
                print(f"  {k}: {v}")
            tick += cm["duration"]

if __name__ == "__main__":
    main(sys.argv[1])
