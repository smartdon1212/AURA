# AURA — v1

A vision-first cognitive AI companion, built as a body-agnostic core: the
Mind (perception → memory → reasoning → action) never knows which Body
it's talking to. There are two Bodies today — `bodies/desktop` (console
and speakers) and `bodies/web` (a browser interface) — and a robot Body
is a future addition, not a rewrite. The web Body is the first real test
of that claim: it added no code to Mind, Memory, the bus or the
contracts.

## What v1 actually does

Looks through your webcam, notices objects, remembers what it's seen,
reacts out loud (speech, not text) when something new shows up, and
introduces itself with freshly generated, never-identical phrasing each
time it starts.

It is also interactive. Press Enter, speak, and it answers out loud from
what it has actually observed. Ambient narration pauses for the whole
exchange — from your keypress until it finishes answering — so it never
talks over you, and never records its own voice back as though you had
said it.

## Architecture (see contracts/ for the source of truth)

```
perception/  →  bus/  →  memory/episodic  (persists everything seen)
                      →  mind/             (reasons, decides, reacts)
                      →  bodies/desktop    (speaks)
                      →  bodies/web        (browser: face, camera, voice)
```

Every arrow above is a Redis Stream. Every payload is a validated
Pydantic model (`Percept` or `Action`) defined once in `contracts/`.
Services never call each other directly.

The routing is what makes a new Body cheap: a Body publishes its Percepts
stamped with its own `source_body_id`, and Mind addresses its reply to
`actions:<that id>`. A Body that calls itself `web-01` therefore gets its
own reply stream automatically. Neither Mind nor Memory has a branch for
"which Body is this".

## One-time setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
2. `uv sync` — installs everything from `pyproject.toml`
3. Start Redis:
   - Windows: `net start Memurai` (Administrator PowerShell)
   - Linux: `sudo systemctl start redis`
4. Start Postgres:
   - Windows: `net start postgresql-x64-18` (Administrator PowerShell;
     confirm the exact name with `Get-Service *postgres*`)
   - Linux: `sudo systemctl start postgresql`
5. Set the Postgres password. It is read from the environment and never
   written into source, so this is the only place it lives:
   - Windows (PowerShell): `$env:AURA_PG_PASSWORD="your-password"`
   - Linux/macOS: `export AURA_PG_PASSWORD="your-password"`

   `setx AURA_PG_PASSWORD "..."` persists it across sessions instead,
   but only affects terminals opened afterwards.
6. Set your free Gemini API key (get one at
   https://aistudio.google.com/apikey — no card required):
   - Windows (PowerShell): `$env:GEMINI_API_KEY="your-key"`
   - Linux/macOS: `export GEMINI_API_KEY="your-key"`

`AURA_PG_HOST` (localhost), `AURA_PG_PORT` (5432), `AURA_PG_USER`
(postgres) and `AURA_PG_DB` (postgres) override the connection defaults;
you only need to set them if your server differs.

## Running it

**Everything at once (recommended):**
```
uv run python run_aura.py
```
Runs preflight checks (Redis, Postgres, API key) and fails fast with a
clear message if something isn't ready, instead of four separate
services crashing with unrelated stack traces.

**In the browser:**
```
uv run python run_aura.py --web
```
Then open **http://127.0.0.1:8000**.

There is no separate front-end build step — the page is plain HTML, CSS
and JavaScript served by the web Body itself, with no bundler and no
framework, so `uv sync` is still the only install.

`--web` swaps the Body *and* drops the `perception` service, because the
web Body owns the camera. Only one process can hold a webcam on Windows:
OpenCV takes exclusive access, so running both would mean one of them
failing to open the device. Memory and Mind are byte-for-byte the same
services in both modes — that is the entire point of the split.

In the browser interface:

| Input | Effect |
| --- | --- |
| Click the orb, or press `Enter` / `Space` | Start talking. Press again to stop and send. |
| `c` | Open the camera panel; press again to close it. |
| `Escape` | Close the camera panel. |

Recording stops by itself at 30 seconds and sends what it has, so a
forgotten recording cannot hold the microphone forever — press again to
send earlier.

The orb is the face: cyan and slow while observing, a bright fast ring
while listening (driven by live microphone amplitude), amber while
thinking, violet while speaking. The camera panel is a live MJPEG feed
with the same detection boxes the desktop window draws, plus corner
brackets drawn on top so it reads as an instrument rather than a video.
Ambient narration appears as a dimmed `AMBIENT` line in the caption; your
question and AURA's answer take over the same space in brighter rows.
While an exchange is open, ambient narration is suppressed — and still
acknowledged, so it is dropped rather than delivered late.

`Escape` while recording cancels: the audio is discarded without being
sent, and the exchange window is closed cleanly.

**Individually, for debugging** (each in its own terminal):
```
uv run python perception/service.py
uv run python memory/episodic/writer.py
uv run python mind/reasoning_loop.py
uv run python bodies/desktop/run.py
uv run python bodies/web/app.py       # browser Body alone, 127.0.0.1:8000
```

**Standalone debug tools** (no bus/memory/mind involved):
```
uv run python perception/run_local.py   # camera + detection window only
uv run python bus/run_local.py          # bus round-trip test, no camera
uv run python mind/run_local.py         # generates 3 introductions, no camera/bus
uv run python mind/query_local.py       # typed questions, memory-grounded answers
```

## Tests

```
uv run python -m unittest discover -s tests -v
```
Covers the DSN construction and connect-timeout behaviour in
`memory/episodic/store.py`, the interaction policy shared by both Bodies
(that ambient narration is dropped rather than queued during a
conversation, and that two sources of speech never overlap), the
question-answering prompt, and the web Body's HTTP surface — the
`/ask` endpoint, the exchange window, narration fan-out, and the vision
loop's camera-failure handling. No camera, microphone, Redis or API key
needed — the bus, camera, detector and speaker are all stubbed.

## Known v1 scope limits (deliberate, not oversights)

- **Memory is recency-based only** — "what did you see recently", not
  semantic search ("show me something like this"). That needs an
  embeddings pipeline (pgvector), planned as an addition alongside
  `memory/episodic`, not a replacement for it.
- **Decision layer is a novelty filter only** — cooldown-based, so AURA
  doesn't narrate every video frame. Full reflex-vs-deliberative
  arbitration is deferred until there's a robot Body with actual
  reflexes to arbitrate.
- **Conversation is push-to-talk and fixed-length** — press Enter, then
  it records a fixed window. There is no voice-activity detection, so
  long answers get clipped and short ones waste the tail of the window.
  A naive energy-threshold VAD misfires on real room noise, so this
  waits until the fixed-window baseline is proven end to end.
- **Answers draw on a recent window, not all of memory** — the last 10
  minutes of observations, capped at 50 rows. Recalling something from
  last week needs the semantic layer above.
- **The web interface is local-only and unauthenticated** — bound to
  `127.0.0.1` with no login, which is the right trade for a personal
  system on one machine and the wrong one for a LAN. A LAN address would
  not work anyway: browsers refuse `getUserMedia` on a non-secure origin,
  so the microphone is unavailable from another device without TLS.
- **The browser's voice is not AURA's configured voice** — the desktop
  Body picks a voice through `pyttsx3`; the page uses whichever voice the
  host browser offers, chosen by a preference list in `app.js`. Quality
  therefore varies by machine and by installed voices.

## Platform notes

Developed on Windows, designed to move to Linux later — `perception/camera.py`
and `bodies/desktop/speaker.py` both auto-select the right backend per
OS (DirectShow/SAPI5 on Windows, V4L2/eSpeak on Linux) with no config
changes needed when you migrate.

The one place that cannot be portable is draining buffered keystrokes
(`bodies/desktop/conversation.py`), because the standard library has no
portable "clear stdin". It uses the documented split — `msvcrt` on
Windows, `termios` on POSIX — with both branches present, so neither
platform is special-cased away.

The web Body has no such split: the browser owns the microphone, the
speakers and the camera, so `bodies/web` contains no OS-specific code at
all. It records 16 kHz mono PCM through an `AudioWorklet` — not the
deprecated `ScriptProcessorNode`, which drops samples under load, and not
`MediaRecorder`, whose WebM/Opus output Gemini does not accept — and
encodes a WAV header in the page so the bytes reaching the transcriber
are the same format `perception/voice.py` produces on the desktop path.
It speaks with the browser's own `speechSynthesis`, so `pyttsx3` is not
involved in web mode.
