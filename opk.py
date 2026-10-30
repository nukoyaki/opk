#!/usr/bin/env python3

import argparse
import array
import functools
import struct
import subprocess
import sys
from pathlib import Path

MAGIC = b"OPKT"
VERSION = 2
RATE = 48000
EDGE = 8
HEADER = struct.Struct("<4sBBHIIIIHHII")
REGION = struct.Struct("<IH")

_PCM = 1
_FLOAT = 3
_EXTENSIBLE = 0xFFFE


class PackError(Exception):
    pass


class Wave:
    def __init__(self, rate, channels, fmt, data, loop):
        self.rate = rate
        self.channels = channels
        self.fmt = fmt
        self.data = data
        self.loop = loop

    @property
    def frames(self):
        return len(self.data) // (self.channels * int(self.fmt[1:3]) // 8)


def is_wav(data):
    return data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def parse_fmt(body, name):
    if len(body) < 16:
        raise PackError(f"{name}: fmt chunk is too short")
    tag, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", body)
    if tag == _EXTENSIBLE:
        if len(body) < 40:
            raise PackError(f"{name}: extensible fmt chunk is too short")
        tag = struct.unpack_from("<H", body, 24)[0]
    if tag == _PCM and bits in (16, 24):
        fmt = f"s{bits}le"
    elif tag == _FLOAT and bits == 32:
        fmt = "f32le"
    else:
        raise PackError(f"{name}: unsupported wav format (tag {tag:#x}, {bits} bit), "
                        "only s16, s24 and f32 are supported")
    if not channels or align != channels * bits // 8:
        raise PackError(f"{name}: bad block align {align} for {channels} ch {bits} bit")
    return rate, channels, fmt


def parse_smpl(body):
    if len(body) < 60 or struct.unpack_from("<I", body, 28)[0] == 0:
        return None
    start, end = struct.unpack_from("<II", body, 44)
    return start, end + 1


def parse_wav(data, name):
    data = memoryview(data)
    if not is_wav(data):
        raise PackError(f"{name} is not a wav file")
    pos, fmt, pcm, loop = 12, None, None, None
    while pos + 8 <= len(data):
        cid, size = struct.unpack_from("<4sI", data, pos)
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = parse_fmt(body, name)
        elif cid == b"data":
            pcm = body
        elif cid == b"smpl":
            loop = parse_smpl(body)
        pos += 8 + size + (size & 1)
    if fmt is None:
        raise PackError(f"{name} has no fmt chunk")
    if pcm is None:
        raise PackError(f"{name} has no data chunk")
    rate, channels, kind = fmt
    width = channels * int(kind[1:3]) // 8
    return Wave(rate, channels, kind, pcm[:len(pcm) // width * width], loop)


def ffmpeg(args, stdin=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    io = {"input": bytes(stdin)} if stdin is not None else {"stdin": subprocess.DEVNULL}
    if stdin is None:
        cmd.append("-nostdin")
    try:
        r = subprocess.run(cmd + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **io)
    except FileNotFoundError:
        raise PackError("ffmpeg not found on PATH")
    if r.returncode:
        raise PackError(f"ffmpeg failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


@functools.cache
def resampler():
    conf = subprocess.run(["ffmpeg", "-hide_banner", "-buildconf"], capture_output=True).stdout
    return ["-af", "aresample=resampler=soxr:precision=28"] if b"enable-libsoxr" in conf else []


def load(path):
    """Return (wave, f32le pcm at 48 kHz) for any input file."""
    data = path.read_bytes()
    if is_wav(data):
        wave = parse_wav(data, path.name)
        if wave.fmt == "f32le" and wave.rate == RATE:
            return wave, bytes(wave.data)
        pcm = ffmpeg(["-f", wave.fmt, "-ar", str(wave.rate), "-ac", str(wave.channels),
                      "-i", "-", *resampler(), "-f", "f32le", "-ar", str(RATE), "-"], wave.data)
    else:
        wave = parse_wav(ffmpeg(["-i", str(path), "-vn", "-c:a", "pcm_f32le", "-f", "wav", "-"]),
                         path.name)
        wave.loop = None
        pcm = ffmpeg(["-f", "f32le", "-ar", str(wave.rate), "-ac", str(wave.channels),
                      "-i", "-", *resampler(), "-f", "f32le", "-ar", str(RATE), "-"], wave.data) \
            if wave.rate != RATE else bytes(wave.data)
    if wave.channels > 2:
        raise PackError(f"{path.name} has {wave.channels} channels, only mono and stereo are supported")
    return wave, pcm


def upmix(pcm):
    mono = array.array("f", pcm)
    stereo = array.array("f", bytes(len(pcm) * 2))
    stereo[0::2] = mono
    stereo[1::2] = mono
    return stereo.tobytes()


def edge_s16(pcm, channels, first, count):
    vals = struct.unpack_from(f"<{count * channels}f", pcm, first * channels * 4)
    return struct.pack(f"<{len(vals)}h", *(max(-32768, min(32767, round(v * 32768))) for v in vals))


def resolve_loop(args, wave, total):
    start, end = args.loop_start, args.loop_end
    source = "command line" if start is not None or end is not None else None
    if wave.loop:
        if start is None:
            start = wave.loop[0]
        if end is None:
            end = wave.loop[1]
            # some tools write the smpl end as exclusive, which lands one past the last frame
            if end == wave.frames + 1:
                end = wave.frames
        source = source + " + smpl chunk" if source else "smpl chunk"
    if start is None and end is None:
        return 0, 0, "default (no loop points)"
    start = start or 0
    if end is None:
        end = wave.frames
    if not 0 <= start <= end <= wave.frames:
        raise PackError(f"loop {start}..{end} must satisfy 0 <= start <= end <= {wave.frames}")
    if wave.rate != RATE:
        start, end = round(start * RATE / wave.rate), round(end * RATE / wave.rate)
        source += f", scaled from {wave.rate} Hz"
    end = min(end, total)
    if (start, end) != (0, 0) and start >= end:
        raise PackError(f"loop {start}..{end} is empty")
    return start, end, source


_SILK = [480, 960, 1920, 2880]
_HYBRID = [480, 960]
_CELT = [120, 240, 480, 960]


def ogg_packets(data):
    """Yield (packet_bytes, granule_of_page_it_ended_on) from an Ogg stream."""
    pos, pending, serial = 0, b"", None
    while pos < len(data):
        if data[pos:pos + 4] != b"OggS":
            raise PackError(f"bad Ogg page sync at offset {pos}")
        granule, page_serial = struct.unpack_from("<qI", data, pos + 6)
        if serial is None:
            serial = page_serial
        elif page_serial != serial:
            raise PackError("ffmpeg produced more than one logical Ogg stream")
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


def packet_samples(pkt):
    toc = pkt[0]
    cfg = toc >> 3
    frame = _SILK[cfg % 4] if cfg < 12 else _HYBRID[cfg % 2] if cfg < 16 else _CELT[cfg % 4]
    code = toc & 3
    frames = 1 if code == 0 else 2 if code in (1, 2) else pkt[1] & 0x3F
    return frame * frames


def encode(pcm, channels, bitrate, application):
    """Encode f32le 48 kHz pcm, returning (pre_skip, packets, packet_samples, total)."""
    packets = list(ogg_packets(ffmpeg(
        ["-f", "f32le", "-ar", str(RATE), "-ac", str(channels), "-i", "-",
         "-c:a", "libopus", "-b:a", bitrate, "-vbr", "on", "-compression_level", "10",
         "-application", application, "-frame_duration", "20", "-f", "ogg", "-"], pcm)))

    head = packets[0][0]
    if not head.startswith(b"OpusHead") or head[9] != channels:
        raise PackError("ffmpeg output doesn't start with a matching OpusHead")
    pre_skip = struct.unpack_from("<H", head, 10)[0]

    audio = [p for p, _ in packets[2:]]
    durs = {packet_samples(p) for p in audio}
    if len(durs) != 1:
        raise PackError(f"packet durations vary {sorted(durs)}; this format assumes constant duration")
    per_packet = durs.pop()

    total = len(pcm) // (channels * 4)
    if packets[-1][1] - pre_skip != total:
        raise PackError(f"encoded length {packets[-1][1] - pre_skip} doesn't match source length {total}")
    too_big = [len(p) for p in audio if len(p) > 0xFFFF]
    if too_big:
        raise PackError(f"packet of {too_big[0]} bytes doesn't fit a u16 prefix")
    return pre_skip, audio, per_packet, total


def pack_file(args, path):
    wave, pcm = load(path)
    channels = wave.channels
    total = len(pcm) // (channels * 4)
    if not total:
        raise PackError(f"{path.name} is empty")
    loop_start, loop_end, source = resolve_loop(args, wave, total)

    seam_start, seam_end = (loop_start, loop_end) if loop_end else (0, total)
    edge = min(EDGE, (seam_end - seam_start) // 2)
    extra = edge_s16(pcm, channels, seam_end - edge, edge) + edge_s16(pcm, channels, seam_start, edge)

    info = [f"  loop      {loop_start:,} .. {loop_end:,} "
            f"({loop_start / RATE:.3f} s .. {loop_end / RATE:.3f} s) from {source}",
            f"  edges     {edge} lossless frames each side of {seam_start:,} .. {seam_end:,}"]
    return pcm, channels, loop_start, loop_end, edge, [], extra, info


def pack_folder(args, folder):
    if args.loop_start is not None or args.loop_end is not None:
        raise PackError("--loop-start/--loop-end only apply to single files, not folders")
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        raise PackError(f"{folder} has no files")

    loaded = []
    for p in files:
        wave, pcm = load(p)
        loaded.append((p.stem, wave.channels, pcm))
    names = [n for n, _, _ in loaded]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise PackError(f"more than one file is named {', '.join(dupes)} (ignoring extensions)")

    channels = max(c for _, c, _ in loaded)
    parts, regions, table = [], [], b""
    for name, ch, pcm in loaded:
        if ch < channels:
            pcm = upmix(pcm)
        frames = len(pcm) // (channels * 4)
        raw = name.encode("utf-8")
        if len(raw) > 0xFFFF:
            raise PackError(f"region name {name[:32]}... is too long")
        parts.append(pcm)
        regions.append((name, frames))
        table += REGION.pack(frames, len(raw)) + raw

    info, pos = [f"  {len(regions)} regions"], 0
    for name, frames in regions:
        info.append(f"    {name:<24} {pos:>10,} .. {pos + frames:>10,} ({frames / RATE:.3f} s)")
        pos += frames
    return b"".join(parts), channels, 0, 0, 0, regions, table, info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="audio file, or a folder of audio files to pack as named regions")
    ap.add_argument("output")
    ap.add_argument("--loop-start", type=int, help="loop start in source samples (default: smpl chunk, else 0)")
    ap.add_argument("--loop-end", type=int, help="loop end in source samples, exclusive (default: smpl chunk, else 0)")
    ap.add_argument("--bitrate", default="128k", help="libopus VBR target bitrate (default: 128k)")
    ap.add_argument("--application", default="audio", choices=["audio", "voip", "lowdelay"],
                    help="libopus application: audio for music and effects, voip for speech, "
                         "lowdelay for CELT only (default: audio)")
    args = ap.parse_args()

    src = Path(args.input)
    try:
        pack = pack_folder if src.is_dir() else pack_file
        pcm, channels, loop_start, loop_end, edge, regions, extra, info = pack(args, src)
        pre_skip, audio, per_packet, total = encode(pcm, channels, args.bitrate, args.application)
    except (PackError, OSError) as e:
        sys.exit(f"error: {e}")

    data_offset = HEADER.size + len(extra)
    out = bytearray(HEADER.pack(MAGIC, VERSION, channels, pre_skip, len(audio), total,
                                loop_start, loop_end, per_packet, edge, len(regions), data_offset))
    out += extra
    for p in audio:
        out += struct.pack("<H", len(p))
        out += p
    open(args.output, "wb").write(out)

    back = open(args.output, "rb").read()
    hdr = HEADER.unpack_from(back, 0)
    pos, got = hdr[11], []
    for _ in range(hdr[4]):
        n = struct.unpack_from("<H", back, pos)[0]
        got.append(back[pos + 2:pos + 2 + n])
        pos += 2 + n
    assert pos == len(back) and got == audio and back[HEADER.size:hdr[11]] == extra, "verification failed"

    print(f"wrote {args.output}: {len(out):,} bytes - verified")
    print(f"  {channels} ch, {len(audio):,} packets x {per_packet} samples, pre-skip {pre_skip}")
    print(f"  length    {total:,} samples ({total / RATE:.3f} s)")
    print(f"  bitrate   {(len(out) - data_offset) * 8 * RATE / total / 1000:.1f} kbps actual, "
          f"{args.bitrate} target ({args.application})")
    print("\n".join(info))


if __name__ == "__main__":
    main()
