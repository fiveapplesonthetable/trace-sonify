# trace-sonify

Turn any [Perfetto](https://perfetto.dev) trace into **audio you can listen to** —
and into a Shazam-style **spectrogram** you can look at.

Every track in the trace (a thread's slices, or a counter) becomes one audio
stream; a `MASTER` stream mixes them all. The output is itself a
`.perfetto-trace` in the **`android.audio`** wire format, so you open it in
`ui.perfetto.dev` (with the `dev.perfetto.Audio` plugin) and **each track is a
playable waveform** — press play on a track, or select a range and play just
that part.

The point: give a trace an **aural signature**. Because pitch is derived from
names and time is mapped linearly, *similar traces sound similar* — two similar
app startups produce nearly the same sound, and a regression you can *hear*.
A trained ear (or an audio-fingerprinting pipeline) can then find/label regions
and do real research on trace similarity.

## What maps to what

| Trace feature | Sound |
|---|---|
| slice **name** | **pitch** — the name is hashed into a fixed major-pentatonic scale, so the same operation is always the same note (the basis of the signature) |
| slice **depth** (nesting) | **octave** — deeper call stacks ring higher |
| slice **duration** | **note length** |
| **instant**/short slice | a short blip (floored at ~6 ms so 16 ms frames still resolve) |
| counter **value** | a **drone** at the counter's pitch whose loudness tracks the normalised value — a swelling pad as the counter rises/falls |
| **time** | mapped **linearly and absolutely** (see below) |

### Latency-sensitive by design
Time is mapped linearly with `--speed` (default `1.0` = real time:
`audio_length = trace_length / speed`). It is **not** normalised per trace, so a
**1.1 s** startup sounds audibly longer than a **0.9 s** one, and a **100 ms**
stall is a clearly placed ~100 ms gap. Use a higher `--speed` (e.g. `10`) to
fast-forward long traces while keeping every latency proportionally intact.
(`--duration` forces a fixed length instead — handy for length-normalised
comparison, but it throws absolute latency away.)

### Jank you can hear
Frame work (`Choreographer#doFrame`, `DrawFrame`, `RenderThread`) recurs on a
~16.6 ms cadence → a steady rhythmic pulse. A janky frame is a long note plus a
gap — the rhythm stutters. Periodic jank in a CUJ becomes an audible,
spectrogram-visible beat irregularity.

### Shazam-style fingerprint
Deterministic name→pitch means the spectrogram of a given workload is stable
across runs. `--spectrogram out.png` renders one (via ffmpeg `showspectrumpic`):
horizontal bands are counter drones, vertical striations are slice onsets/jank,
and the overall texture is the trace's fingerprint — ready for constellation /
peak-pair fingerprinting à la Shazam.

## Usage

```bash
# real time, with a spectrogram
TP_SHELL=/path/to/trace_processor_shell \
  python3 sonify.py in.perfetto-trace out.perfetto-trace \
    --spectrogram out.png

# fast-forward a 2-minute trace to ~12s, keeping latencies proportional
python3 sonify.py in.perfetto-trace out.perfetto-trace --speed 10

# busiest 8 tracks only, no master
python3 sonify.py in.perfetto-trace out.perfetto-trace --max-tracks 8 --no-master
```

Then drag `out.perfetto-trace` into <https://ui.perfetto.dev> → the **Audio**
group has one waveform per track plus **MASTER (mix)**. Play any of them.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--speed` | `1.0` | `audio_length = trace_length / speed` (absolute, latency-preserving) |
| `--duration S` | – | force a fixed audio length (normalises trace length away) |
| `--spectrogram PNG` | – | also write a spectrogram of the master |
| `--rate HZ` | `32000` | sample rate |
| `--max-tracks N` | `24` | sonify the N busiest slice tracks and N busiest counters |
| `--no-master` | off | skip the mixed master stream |
| `--tp-shell PATH` | `$TP_SHELL` or `trace_processor_shell` | trace_processor binary |

## Requirements

- `python3` + `numpy`
- `ffmpeg` (AAC encode + `showspectrumpic`)
- a `trace_processor_shell` binary ([build](https://perfetto.dev/docs/contributing/build-instructions)
  or download from the Perfetto releases), pointed to via `--tp-shell` / `$TP_SHELL`

## How it works (pipeline)

1. Query the trace with `trace_processor_shell` for bounds, the busiest slice
   tracks, their slices, and counter tracks + samples.
2. Synthesise a mono PCM buffer per track (numpy): slices → enveloped tones at
   name-derived pitches; counters → an amplitude-modulated drone.
3. Mix the master, encode each buffer to AAC-LC (ffmpeg), and emit
   `TracePacket.audio_frame` packets (one AAC access unit per frame, plus a
   per-frame peak for the waveform) — the `android.audio` format.
4. Optionally render the master's spectrogram.

## Notes / ideas

- The `android.audio` plugin (`dev.perfetto.Audio`) is what renders/plays the
  output. This tool reuses that infra purely as a player.
- Natural next steps: peak-pair audio fingerprints for automatic
  trace-similarity search; per-name instrument banks; stereo panning by
  CPU/thread.

---
Built with [Claude Code](https://claude.com/claude-code).
