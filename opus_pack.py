#!/usr/bin/env python3

import argparse
import struct
import sys

MAGIC = b"OPKT"
VERSION = 1
HEADER = struct.Struct("<4sBBHIIIIHH")

def ogg_packets(data):
    """Yield (packet_bytes, granule_of_page_it_ended_on) from an Ogg stream."""
    pos, pending, serial = 0, b"", None
    while pos < len(data):
        if data[pos:pos + 4] != b"OggS":
            raise ValueError(f"bad Ogg page sync at offset {pos}")
        granule, page_serial = struct.unpack_from("<qI", data, pos + 6)
        if serial is None:
            serial = page_serial
        elif page_serial != serial:
            raise ValueError("file has more than one logical stream (chained/multiplexed Ogg)")
        nsegs = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nsegs]
        body = pos + 27 + nsegs
        for lace in lacing:
            pending += data[body:body + lace]
            body += lace
            if lace < 255:
                yield pending, granule
                pending = b""
        pos = body
    if pending:
        yield pending, -1


_SILK = [480, 960, 1920, 2880]
_HYBRID = [480, 960]
_CELT = [120, 240, 480, 960]


def packet_samples(pkt):
    toc = pkt[0]
    cfg = toc >> 3
    frame = _SILK[cfg % 4] if cfg < 12 else _HYBRID[cfg % 2] if cfg < 16 else _CELT[cfg % 4]
    code = toc & 3
    frames = 1 if code == 0 else 2 if code in (1, 2) else pkt[1] & 0x3F
    return frame * frames


def parse_tags(pkt):
    vlen = struct.unpack_from("<I", pkt, 8)[0]
    pos = 12 + vlen
    count = struct.unpack_from("<I", pkt, pos)[0]
    pos += 4
    tags = {}
    for _ in range(count):
        n = struct.unpack_from("<I", pkt, pos)[0]
        pos += 4
        key, _, val = pkt[pos:pos + n].decode("utf-8", "replace").partition("=")
        tags[key.upper()] = val
        pos += n
    return tags


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--loop-start", type=int)
    ap.add_argument("--loop-end", type=int)
    args = ap.parse_args()

    data = open(args.input, "rb").read()
    packets = list(ogg_packets(data))

    head = packets[0][0]
    if not head.startswith(b"OpusHead"):
        sys.exit("error: first packet is not OpusHead")
    channels = head[9]
    pre_skip = struct.unpack_from("<H", head, 10)[0]
    if head[18] != 0:
        sys.exit(f"error: channel mapping family {head[18]} not supported (only mono/stereo, family 0)")
    tags = parse_tags(packets[1][0]) if packets[1][0].startswith(b"OpusTags") else {}

    audio = [p for p, _ in packets[2:]]
    final_granule = packets[-1][1]
    if final_granule < 0:
        sys.exit("error: last packet has no granule position (truncated file?)")

    durs = {packet_samples(p) for p in audio}
    if len(durs) != 1:
        sys.exit(f"error: packet durations vary {sorted(durs)}; this format assumes constant duration")
    per_packet = durs.pop()

    total = final_granule - pre_skip
    if total <= 0 or total > len(audio) * per_packet - pre_skip:
        sys.exit(f"error: implausible length {total} from granule {final_granule}")

    loop_start = args.loop_start
    loop_end = args.loop_end
    source = "command line"
    if loop_start is None and "LOOPSTART" in tags:
        loop_start = int(tags["LOOPSTART"])
        source = "file tags"
        if loop_end is None:
            if "LOOPLENGTH" in tags:
                loop_end = loop_start + int(tags["LOOPLENGTH"])
            elif "LOOPEND" in tags:
                loop_end = int(tags["LOOPEND"])
    if loop_start is None:
        loop_start, source = 0, "default (whole track)"
    if loop_end is None:
        loop_end = total
    if not 0 <= loop_start < loop_end <= total:
        sys.exit(f"error: loop {loop_start}..{loop_end} must satisfy 0 <= start < end <= {total}")

    too_big = [len(p) for p in audio if len(p) > 0xFFFF]
    if too_big:
        sys.exit(f"error: packet of {too_big[0]} bytes doesn't fit a u16 prefix")

    out = bytearray(HEADER.pack(MAGIC, VERSION, channels, pre_skip, len(audio),
                                total, loop_start, loop_end, per_packet, 0))
    for p in audio:
        out += struct.pack("<H", len(p))
        out += p
    open(args.output, "wb").write(out)

    back = open(args.output, "rb").read()
    hdr = HEADER.unpack_from(back, 0)
    pos, got = HEADER.size, []
    for _ in range(hdr[4]):
        n = struct.unpack_from("<H", back, pos)[0]
        got.append(back[pos + 2:pos + 2 + n])
        pos += 2 + n
    assert pos == len(back) and got == audio, "verification failed"

    pk = lambda s: (s + pre_skip) // per_packet
    print(f"wrote {args.output}: {len(out):,} bytes "
          f"(input {len(data):,}, {100 * len(out) / len(data):.1f}%) - verified")
    print(f"  {channels} ch, {len(audio):,} packets x {per_packet} samples, pre-skip {pre_skip}")
    print(f"  length    {total:,} samples ({total / 48000:.3f} s)")
    print(f"  loop      {loop_start:,} .. {loop_end:,} "
          f"({loop_start / 48000:.3f} s .. {loop_end / 48000:.3f} s) from {source}")
    print(f"  loop start is in packet {pk(loop_start)}, loop end in packet {pk(loop_end - 1)}")


if __name__ == "__main__":
    main()
