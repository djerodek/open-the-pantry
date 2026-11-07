"""Test helper: a zip that is only a central directory of n tiny entries, plus its end record."""
import struct, sys
def make(path, n, zip64=False, lie=None):
    entry = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0) + b"a"
    with open(path, "wb") as f:
        f.write(entry * n)
        cd = len(entry) * n
        count = lie if lie is not None else n
        if zip64:
            z64_at = f.tell()
            f.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, count, count, cd, 0))
            f.write(struct.pack("<IIQI", 0x07064B50, 0, z64_at, 1))
            f.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0))
        else:
            f.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, min(count, 0xFFFF), min(count, 0xFFFF), cd, 0, 0))
if __name__ == "__main__":
    make(sys.argv[1], int(sys.argv[2]), sys.argv[3] == "64")
