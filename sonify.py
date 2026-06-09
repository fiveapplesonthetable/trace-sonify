#!/usr/bin/env python3
"""sonify.py — turn any Perfetto trace into audio you can listen to in the UI.

Every track in the trace (a thread's slices, or a counter) becomes one audio
stream in the android.audio wire format (TracePacket.audio_frame), plus a MASTER
stream that mixes them all. Open the output in ui.perfetto.dev with the
dev.perfetto.Audio plugin and each track is a playable waveform.

Mapping (deterministic, so similar traces sound similar):
  - slice name      -> pitch   (name is hashed into a fixed musical scale)
  - slice depth     -> octave  (deeper nesting = higher octave)
  - slice duration  -> note length
  - counter value   -> a drone whose loudness tracks the normalised value
  - trace time      -> compressed to a fixed audio length, so traces of
                       different durations are directly comparable by ear

Because pitch comes from names and timing is normalised, two similar app
startups produce nearly the same sound — an "aural signature" for trace
-similarity research and for labelling regions of a trace by how they sound.

Requires: numpy, ffmpeg, and a trace_processor_shell binary.
"""
import argparse
import csv
import hashlib
import io
import math
import os
import struct
import subprocess
import sys
import tempfile

import numpy as np

# --------------------------------------------------------------------------
# android.audio wire format (TracePacket.audio_frame = field 1002)
# --------------------------------------------------------------------------
AAC_LC = 1
CLOCK_BOOTTIME = 6
SR_TABLE = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
            16000, 12000, 11025, 8000, 7350]


def varint(n):
    o = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            o.append(b | 0x80)
        else:
            o.append(b)
            return bytes(o)


def tag(f, wt):
    return varint((f << 3) | wt)


def fv(f, v):
    return tag(f, 0) + varint(v)


def fb(f, d):
    return tag(f, 2) + varint(len(d)) + d


def fs(f, s):
    return fb(f, s.encode())


def parse_adts(buf):
    """Split an ADTS AAC stream into raw access units; return (frames, srIdx, ch, aot)."""
    i, n = 0, len(buf)
    frames = []
    srIdx = ch = aot = None
    while i + 7 <= n:
        if buf[i] != 0xFF or (buf[i + 1] & 0xF0) != 0xF0:
            i += 1
            continue
        protection_absent = buf[i + 1] & 1
        profile = (buf[i + 2] >> 6) & 0x3
        srIdx = (buf[i + 2] >> 2) & 0xF
        ch = ((buf[i + 2] & 1) << 2) | (buf[i + 3] >> 6)
        aot = profile + 1
        frame_len = ((buf[i + 3] & 0x3) << 11) | (buf[i + 4] << 3) | (buf[i + 5] >> 5)
        if frame_len < 7 or i + frame_len > n:
            break
        hdr = 7 if protection_absent else 9
        frames.append(buf[i + hdr:i + frame_len])
        i += frame_len
    return frames, srIdx, ch, aot


# --------------------------------------------------------------------------
# trace_processor querying
# --------------------------------------------------------------------------
def tp_query(shell, trace, sql):
    """Run one SQL statement via trace_processor_shell and return list[dict]."""
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
        f.write(sql)
        qpath = f.name
    try:
        out = subprocess.run([shell, "-q", qpath, trace],
                             capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(qpath)
    rows = list(csv.reader(io.StringIO(out)))
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r)) for r in rows[1:] if len(r) == len(header)]


# --------------------------------------------------------------------------
# name -> pitch (deterministic, consistent across traces -> aural signature).
#
# A name is hashed (md5) and the hash indexes a fixed palette of MIDI notes.
# The palette is a major-pentatonic scale spanning several octaves, so:
#   - distinct names land on distinct, well-separated, *consonant* pitches
#     (no random dissonance, and far more than the old 10-note space);
#   - the SAME name always gives the SAME pitch, in any trace.
# Slice depth is rendered as TIMBRE (added harmonics), not pitch, so depth and
# name are independent — depth never collides a name onto another's note.
# --------------------------------------------------------------------------
def _palette(lo_oct, hi_oct):
    # Chromatic (all 12 semitones) per octave: prioritise *distinguishability*
    # (many distinct pitches) over consonance — it doesn't need to sound pretty,
    # it needs different operations to sound clearly different.
    return [12 * o + d for o in range(lo_oct, hi_oct + 1) for d in range(12)]


SLICE_PALETTE = _palette(3, 8)    # 72 notes, ~130 Hz .. 3.3 kHz
COUNTER_PALETTE = _palette(1, 3)  # 36 low notes, beneath the slices


def _hash(name):
    return int(hashlib.md5(name.encode("utf-8", "replace")).hexdigest(), 16)


def midi_to_freq(midi):
    return 440.0 * 2 ** ((midi - 69) / 12.0)


def name_to_freq(name, palette):
    return midi_to_freq(palette[_hash(name) % len(palette)])


# --------------------------------------------------------------------------
# synthesis
# --------------------------------------------------------------------------
def adsr(n, sr):
    env = np.ones(n, dtype=np.float32)
    a = min(int(0.008 * sr), n // 2)
    r = min(int(0.040 * sr), n - a)
    if a > 0:
        env[:a] = np.linspace(0, 1, a, dtype=np.float32)
    if r > 0:
        env[n - r:] = np.linspace(1, 0, r, dtype=np.float32)
    return env


def render_slice_track(events, total_samples, sr):
    """events: list of (start_s, dur_s, depth, name) already in audio time."""
    buf = np.zeros(total_samples, dtype=np.float32)
    for start_s, dur_s, depth, name in events:
        f = name_to_freq(name, SLICE_PALETTE)           # pitch from name
        start = int(start_s * sr)
        length = max(int(dur_s * sr), int(0.006 * sr))  # >=6ms; resolves 16ms frames
        end = min(start + length, total_samples)
        if end <= start:
            continue
        n = end - start
        t = np.arange(n, dtype=np.float32) / sr
        # depth -> timbre: deeper nesting adds harmonics (brighter), leaving
        # pitch free to encode the name alone.
        wave = np.sin(2 * np.pi * f * t)
        for h in range(2, 2 + min(depth, 5)):
            wave += (0.5 / h) * np.sin(2 * np.pi * h * f * t)
        buf[start:end] += 0.45 * adsr(n, sr) * wave
    return buf


def render_counter_track(samples, total_samples, sr, name):
    """samples: list of (ts_s, value) in audio time, sorted by ts."""
    if not samples:
        return np.zeros(total_samples, dtype=np.float32)
    vals = [v for _, v in samples]
    vmin, vmax = min(vals), max(vals)
    rng = (vmax - vmin) or 1.0
    amp_env = np.zeros(total_samples, dtype=np.float32)
    for i, (ts_s, val) in enumerate(samples):
        s = int(ts_s * sr)
        e = int(samples[i + 1][0] * sr) if i + 1 < len(samples) else total_samples
        s = max(0, min(s, total_samples))
        e = max(0, min(e, total_samples))
        if e > s:
            amp_env[s:e] = 0.08 + 0.55 * float((val - vmin) / rng)
    f = name_to_freq(name, COUNTER_PALETTE)  # low drone, pitch from counter name
    t = np.arange(total_samples, dtype=np.float32) / sr
    wave = np.sin(2 * np.pi * f * t) + 0.4 * np.sin(2 * np.pi * 2 * f * t)
    return amp_env * wave


def soft_limit(buf, ceiling=0.9):
    peak = float(np.max(np.abs(buf))) if buf.size else 0.0
    if peak > ceiling:
        buf = buf * (ceiling / peak)
    return buf


# --------------------------------------------------------------------------
# encode one float buffer -> audio_frame packets for a stream
# --------------------------------------------------------------------------
def encode_stream(buf, sr, stream_id, stream_name, base_ns, tmpdir):
    pcm16 = np.clip(buf, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype("<i2")
    wav = os.path.join(tmpdir, f"s{stream_id}.wav")
    aac = os.path.join(tmpdir, f"s{stream_id}.aac")
    _write_wav(wav, pcm16, sr)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", wav, "-ac", "1", "-ar", str(sr), "-c:a", "aac",
                    "-b:a", "96k", "-f", "adts", aac], check=True)
    frames, srIdx, ch, aot = parse_adts(bytearray(open(aac, "rb").read()))
    if not frames:
        return b""
    sample_rate = SR_TABLE[srIdx]
    asc = struct.pack(">H", (aot << 11) | (srIdx << 7) | (ch << 3))
    cstr = "mp4a.40.%d" % aot
    spf = 1024

    # peak (0..1000) per 1024-sample block, parallel to the AAC frames
    n = len(pcm16)
    peaks = []
    for k in range(0, n, spf):
        block = np.abs(pcm16[k:k + spf].astype(np.int32))
        peaks.append(int(block.max()) * 1000 // 32768 if block.size else 0)

    def packet(ts, payload):
        return fb(1, fv(8, ts) + fv(58, CLOCK_BOOTTIME) + fb(1002, payload))

    out = bytearray()
    out += packet(base_ns, fv(1, stream_id) + fv(3, AAC_LC) + fb(4, asc)
                  + fv(7, sample_rate) + fv(8, ch) + fs(9, stream_name)
                  + fs(10, cstr))
    for i, au in enumerate(frames):
        pts_us = round(i * spf / sample_rate * 1e6)
        peak = peaks[i] if i < len(peaks) else 0
        af = (fv(1, stream_id) + fv(2, i) + fv(3, AAC_LC) + fb(5, bytes(au))
              + fv(6, pts_us) + fv(11, peak))
        out += packet(base_ns + round(i * spf / sample_rate * 1e9), af)
    return bytes(out)


def _write_wav(path, pcm16, sr):
    data = pcm16.tobytes()
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(data)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace", help="input .perfetto-trace")
    ap.add_argument("out", help="output sonified .perfetto-trace")
    ap.add_argument("--tp-shell", default=os.environ.get("TP_SHELL", "trace_processor_shell"),
                    help="path to trace_processor_shell")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="audio length = trace length / speed (default 1.0 = real "
                         "time). Linear & absolute, so a 1.1s startup sounds longer "
                         "than a 0.9s one and jank shows up as a rhythm break.")
    ap.add_argument("--duration", type=float, default=None,
                    help="force a fixed audio length (s); normalises trace length "
                         "away. Prefer --speed when you care about latency.")
    ap.add_argument("--spectrogram", default=None,
                    help="also write a Shazam-style spectrogram PNG of the master")
    ap.add_argument("--rate", type=int, default=32000, help="sample rate")
    ap.add_argument("--max-tracks", type=int, default=24,
                    help="cap number of busiest tracks/processes sonified (default 24)")
    ap.add_argument("--per-process", action="store_true",
                    help="one stream per PROCESS (mix all its threads' slices) "
                         "instead of one per thread track")
    ap.add_argument("--no-master", action="store_true", help="skip the master mix")
    ap.add_argument("--merge", action="store_true",
                    help="append the audio to the ORIGINAL trace (keeps every "
                         "process/thread/track descriptor) and time-align it to "
                         "the original timeline. Default writes audio only.")
    ap.add_argument("--colormap", default="intensity",
                    help="spectrogram color mode (intensity, magma, viridis, "
                         "fire, rainbow, cool, ...); default intensity")
    a = ap.parse_args()

    sr = a.rate

    print("querying trace bounds...", file=sys.stderr)
    b = tp_query(a.tp_shell, a.trace, "SELECT start_ts AS s, end_ts AS e FROM trace_bounds")
    t0, t1 = int(b[0]["s"]), int(b[0]["e"])
    span = (t1 - t0) or 1
    if a.merge:
        # Real-time, absolute placement so the waveform lines up with the
        # original slices/counters on the same timeline.
        if a.duration or a.speed != 1.0:
            print("note: --merge ignores --speed/--duration (aligns to the "
                  "original timeline)", file=sys.stderr)
        D, scale, base_ns = span / 1e9, 1e-9, t0
    else:
        D = a.duration if a.duration else (span / 1e9) / a.speed
        scale, base_ns = D / span, 0
    total = int(D * sr)

    # Busiest slice tracks (by slice count), named by their descriptor
    # hierarchy (process / thread) so streams map back to their source track.
    slice_tracks = tp_query(a.tp_shell, a.trace, f"""
      SELECT track_id, cnt,
        COALESCE(
          (SELECT COALESCE(p.name||' / ','')||th.name
             FROM thread_track tt JOIN thread th USING(utid)
             LEFT JOIN process p USING(upid) WHERE tt.id=s.track_id),
          (SELECT 'tid '||th.tid FROM thread_track tt JOIN thread th USING(utid) WHERE tt.id=s.track_id),
          (SELECT p.name FROM process_track pt JOIN process p USING(upid) WHERE pt.id=s.track_id),
          (SELECT name FROM track WHERE id=s.track_id),
          'track '||s.track_id) AS name
      FROM (SELECT track_id, COUNT(*) cnt FROM slice GROUP BY track_id) s
      ORDER BY cnt DESC LIMIT {a.max_tracks}
    """)
    counter_tracks = tp_query(a.tp_shell, a.trace, f"""
      SELECT c.track_id, cnt, COALESCE(ct.name,'counter '||c.track_id) AS name
      FROM (SELECT track_id, COUNT(*) cnt FROM counter GROUP BY track_id) c
      JOIN counter_track ct ON ct.id=c.track_id
      ORDER BY cnt DESC LIMIT {a.max_tracks}
    """)
    print(f"trace span {span/1e9:.2f}s -> {D:.0f}s audio; "
          f"{len(slice_tracks)} slice tracks, {len(counter_tracks)} counter tracks",
          file=sys.stderr)

    tmpdir = tempfile.mkdtemp(prefix="sonify_")
    streams = []          # (name, float buffer)

    if a.per_process:
        groups = tp_query(a.tp_shell, a.trace, f"""
          SELECT p.upid AS gid,
                 COALESCE(p.name, 'pid '||p.pid, 'upid '||p.upid) AS name,
                 COUNT(*) cnt
          FROM slice s JOIN thread_track tt ON tt.id=s.track_id
          JOIN thread th USING(utid) JOIN process p USING(upid)
          GROUP BY p.upid ORDER BY cnt DESC LIMIT {a.max_tracks}
        """)
        print(f"per-process: {len(groups)} processes", file=sys.stderr)
    else:
        groups = slice_tracks

    for row in groups:
        if a.per_process:
            ev = tp_query(a.tp_shell, a.trace,
                          f"SELECT s.ts,s.dur,s.depth,COALESCE(s.name,'?') AS name "
                          f"FROM slice s JOIN thread_track tt ON tt.id=s.track_id "
                          f"JOIN thread th USING(utid) WHERE th.upid={row['gid']} "
                          f"ORDER BY s.ts")
            label = f"{row['name']} [process]"
        else:
            ev = tp_query(a.tp_shell, a.trace,
                          f"SELECT ts,dur,depth,COALESCE(name,'?') AS name "
                          f"FROM slice WHERE track_id={row['track_id']} ORDER BY ts")
            label = f"{row['name']} [slices]"
        events = [((int(e["ts"]) - t0) * scale, max(int(e["dur"]), 0) * scale,
                   int(e["depth"]), e["name"]) for e in ev]
        buf = soft_limit(render_slice_track(events, total, sr))
        streams.append((label, buf))

    for row in counter_tracks:
        tid = row["track_id"]
        cs = tp_query(a.tp_shell, a.trace,
                      f"SELECT ts,value FROM counter WHERE track_id={tid} ORDER BY ts")
        samples = [((int(c["ts"]) - t0) * scale, float(c["value"])) for c in cs]
        buf = render_counter_track(samples, total, sr, row["name"])
        streams.append((f"{row['name']} [counter]", buf))

    if not streams:
        print("no slice/counter tracks found", file=sys.stderr)
        sys.exit(1)

    master = np.zeros(total, dtype=np.float32)
    for _, buf in streams:
        master += buf
    master = soft_limit(master, 0.95)

    out = bytearray()
    if a.merge:
        out += open(a.trace, "rb").read()   # keep the whole original trace
    sid = 0
    if not a.no_master:
        out += encode_stream(master, sr, sid, "MASTER (mix)", base_ns, tmpdir)
        sid += 1
    for name, buf in streams:
        out += encode_stream(buf, sr, sid, name[:80], base_ns, tmpdir)
        sid += 1
    open(a.out, "wb").write(out)
    print(f"wrote {a.out}: {sid} audio streams ({D:.1f}s)"
          + (" merged into the original trace" if a.merge else ""), file=sys.stderr)

    if a.spectrogram:
        mw = os.path.join(tmpdir, "master.wav")
        _write_wav(mw, (np.clip(master, -1, 1) * 32767).astype("<i2"), sr)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", mw, "-lavfi",
                        f"showspectrumpic=s=1600x600:legend=1:scale=log:color={a.colormap}",
                        "-frames:v", "1", a.spectrogram], check=True)
        print(f"wrote spectrogram {a.spectrogram}", file=sys.stderr)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
