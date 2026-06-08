# trace-sonify

Turn any [Perfetto](https://perfetto.dev) trace into **audio you can listen to**
and a **spectrogram you can read** — an *aural signature* for a trace.

Every track in the trace (a thread's slices, or a counter) becomes one audio
stream; a `MASTER` stream mixes them all. The output is itself a
`.perfetto-trace` in the **`android.audio`** wire format, so you open it in
`ui.perfetto.dev` (with the `dev.perfetto.Audio` plugin) and **each track is a
playable waveform**. With `--merge`, the audio is added *into the original
trace*, time-aligned, with every process/thread/track descriptor preserved.

The point: the mapping is **deterministic**, so *similar traces sound similar* —
two similar app-startups produce nearly the same sound and the same spectrogram.
A trained ear, or a Shazam-style audio fingerprint, can then label regions and
research trace similarity.

---

## Why sonification works (the idea)

The human auditory system is a *world-class time-series classifier*. It
resolves timing to a few milliseconds, separates many simultaneous streams
("cocktail-party" effect), and recognises rhythmic and melodic patterns
instantly and shift-invariantly — exactly the things you want when comparing
the temporal shape of two traces. A flame graph shows you *one* moment well; an
ear takes in *all the tracks at once, over time*, and notices "this startup has
an extra stutter here" without you staring at 4,000 rows.

So we don't try to make music. We make a **faithful, deterministic transform**
of the trace into sound, chosen so that the things engineers care about —
*latency, jank, phase changes, which subsystem is busy* — become things ears and
spectrograms are good at: *when a note happens, how long it lasts, its pitch,
and the rhythm of repetition*.

---

## What's in a Perfetto trace (the input)

- **Tracks**, organised as a tree of **descriptors**: process → thread → track.
- **Slices**: timed events on a track with a **name**, a start `ts`, a `dur`,
  and a **depth** (nesting level in the call stack). `dur = 0` is an instant.
- **Counters**: a numeric value sampled over time on a counter track
  (e.g. heap size, # binder proxies, CPU frequency).

`trace-sonify` reads these with `trace_processor_shell` (SQL over the trace).

---

## The mapping

| Trace feature | Sound | Why |
|---|---|---|
| slice **name** | **pitch** | names are the trace's vocabulary; a stable name→pitch map is what makes the signature |
| slice **depth** | **timbre** (added harmonics) | depth is orthogonal to identity, so it shouldn't move the pitch — deeper stacks just sound *brighter* |
| slice **duration** | **note length** | a long critical-section is a long note |
| **instant** / very short slice | a short blip (≥6 ms) | still audible, and 6 ms < a 16.6 ms frame so frame cadence resolves |
| counter **value** | a **drone** whose loudness tracks the value | a counter is a continuous signal; loudness is a natural readout |
| **time** | linear & **absolute** (`--speed`) | preserves *ratios*, so latencies and rhythm survive |

Output amplitude per stream is soft-limited; the per-track waveform you see in
the UI is the per-frame **peak** (0..1000) of that stream.

---

## The math (so it's reproducible, not magic)

**Name → pitch.** A name is hashed and the hash selects a note from a fixed
palette:

```
h        = md5(name)                      # deterministic, stable across traces
palette  = [12*octave + semitone ...]     # chromatic, octaves 3..8 -> 72 notes
midi     = palette[h mod len(palette)]
freq(Hz) = 440 * 2^((midi - 69) / 12)     # equal-tempered
```

Chromatic (all 12 semitones), not a pretty pentatonic scale, on purpose: the
goal is **distinguishability** — 72 log-spaced, perceptually-even pitches so
different operations clearly sound different. Same name ⇒ same `freq`, in every
trace (the basis of the signature). Counters use a lower palette (octaves 1..3)
so their drones sit beneath the slices.

> Collisions: with `N` distinct names and 72 pitches there are collisions by
> pigeonhole, but (a) the pitch is paired with the depth-derived **timbre**, and
> (b) you can't perceptually separate hundreds of pitches anyway. The pair
> (pitch, brightness) gives a usefully large alphabet.

**Depth → timbre.** A slice at depth `d` is an additive-synthesis tone — a
fundamental plus `d` harmonics, brighter as it nests deeper:

```
s(t) = sin(2πf t) + Σ_{k=2..2+d} (0.5/k) · sin(2π k f t)
```

shaped by a short **ADSR** envelope (8 ms attack, 40 ms release) so overlapping
notes layer cleanly instead of clicking.

**Time → sound (this is the latency-sensitive part).** Trace time is mapped
*linearly and absolutely*:

```
audio_t(seconds) = (ts - t0) · scale
  scale = D / (t1 - t0)        # --duration D  (length-normalised)
  scale = 1 / speed / 1e9      # --speed       (default; real time at speed=1)
  scale = 1e-9                 # --merge       (real time, aligned to original)
```

Because it's linear with **no per-trace normalisation** (unless you ask for
`--duration`), a 1.1 s startup is audibly *longer* than a 0.9 s one, and a
100 ms stall is a clearly-placed ~100 ms gap. `--speed N` fast-forwards long
traces while keeping every interval proportional.

**Counter → drone.** Values are min–max normalised and held between samples:

```
a(t) = 0.08 + 0.55 · (value(t) - vmin) / (vmax - vmin)
out  = a(t) · [sin(2πf t) + 0.4·sin(2·2πf t)]
```

**Encoding.** Each float buffer → 16-bit PCM → AAC-LC (ffmpeg) → one access unit
per ~21 ms frame, wrapped as `TracePacket.audio_frame` packets with a per-frame
`peak`. That's the `android.audio` format, so the existing Perfetto audio plugin
plays it and draws the waveform.

**Spectrogram = the fingerprint.** `--spectrogram` runs a Short-Time Fourier
Transform of the master, `S(t,f) = |STFT{x}|` on a log-frequency axis (ffmpeg
`showspectrumpic`). Horizontal bands are counter drones; vertical striations are
slice onsets / the frame beat; the texture is the trace's fingerprint. This is
the same representation Shazam fingerprints: pick spectral **peaks**, hash
**pairs** of peaks `(f1, f2, Δt)` into a constellation, and you can match a trace
(or a region of one) against a library — automatic trace-similarity search.

---

## Why the claims hold

- **Similar traces sound similar.** Pitch is a pure function of names and timing
  is linear, so identical workloads produce *byte-identical* audio (verified),
  and small differences produce small, localised differences in sound and in the
  spectrogram. There's no hidden per-trace normalisation to wash out a
  regression.
- **Latency is audible.** Absolute linear time means intervals map to
  proportional intervals — the ear discriminates well below 100 ms.
- **Jank is audible.** Frame work (`Choreographer#doFrame`, `DrawFrame`,
  `RenderThread`) recurs on a ~16.6 ms cadence → a steady pulse. A dropped/long
  frame is a long note plus a gap; the rhythm stutters, and the spectrogram's
  vertical comb breaks — visibly and audibly periodic jank.

---

## Modes

- **Standalone** (default): writes an audio-only trace. Open it and the **Audio**
  group has `MASTER (mix)` + one waveform per source track.
- **`--merge`**: appends the audio to the **original** trace and time-aligns it,
  so you get *the entire original trace* — every process/thread/track descriptor
  and all slices/counters, untouched — **plus** the playable waveforms lined up
  on the same timeline. (A Perfetto trace is a concatenation of `TracePacket`s,
  so this is a true superset of the input.) Each audio stream is named by its
  source's descriptor path (`process / thread`) so it maps back to its track.

## Usage

```bash
export TP_SHELL=/path/to/trace_processor_shell

# audio only, with a spectrogram
python3 sonify.py in.perfetto-trace out.perfetto-trace --spectrogram out.png

# add the sound INTO the original trace (keeps the full hierarchy, time-aligned)
python3 sonify.py in.perfetto-trace out.perfetto-trace --merge

# fast-forward a 2-minute trace to ~12 s, latencies kept proportional
python3 sonify.py in.perfetto-trace out.perfetto-trace --speed 10
```

Then drag the output into <https://ui.perfetto.dev>.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--speed` | `1.0` | `audio_len = trace_len / speed` (absolute, latency-preserving) |
| `--duration S` | – | force a fixed audio length (normalises trace length away) |
| `--merge` | off | append audio to the original trace, aligned, hierarchy preserved |
| `--spectrogram PNG` | – | also render a spectrogram of the master |
| `--colormap` | `intensity` | spectrogram colours (`intensity`, `magma`, `viridis`, `fire`, `rainbow`, `cool`, …) |
| `--rate HZ` | `32000` | sample rate |
| `--max-tracks N` | `24` | sonify the N busiest slice tracks and N busiest counters |
| `--no-master` | off | skip the mixed master stream |
| `--tp-shell PATH` | `$TP_SHELL` | `trace_processor_shell` binary |

## Requirements

- `python3` + `numpy`
- `ffmpeg` (AAC encode + `showspectrumpic`)
- `trace_processor_shell` ([build](https://perfetto.dev/docs/contributing/build-instructions)
  or grab a Perfetto release), via `--tp-shell` / `$TP_SHELL`

## Caveats

- **Clock alignment in `--merge`** assumes the trace's primary clock is
  `BOOTTIME` (true for Android system traces). Other sources may sit at an
  offset on the timeline; the standalone mode is unaffected.
- Only the **busiest `--max-tracks`** slice tracks and counters are sonified
  (the long tail is dropped); raise it to include more.
- Playback in the UI uses the `dev.perfetto.Audio` plugin (the `android.audio`
  feature). This tool only produces the format; that plugin is the player.

## Ideas / next steps

- Peak-pair (constellation) audio fingerprints for automatic trace-similarity
  search and "find this jank elsewhere" queries.
- Per-name instrument banks; stereo panning by CPU or thread.
- Nesting the audio track inline as a sibling row *under* each source thread
  (needs the audio plugin to honour a parent-track id) rather than in the Audio
  group.

---
Built with [Claude Code](https://claude.com/claude-code).
