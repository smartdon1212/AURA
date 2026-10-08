# AURA — Project Memory

## What this is
AURA is a vision-first cognitive AI companion: continuous webcam perception,
memory, reasoning, and voice output, architected so the same reasoning/memory
core can eventually drive a physical robot body instead of just a desktop
speaker. Personal long-term project, separate from an unrelated commercial
product (Beacon). Full context: @docs/research-roadmap.md

The central problem AURA exists to solve, and the twelve sub-problems it
decomposes into, are set out in @Problems_AURA_Solve.md. Treat that document
as the roadmap, and its "Highest-priority sequence" as the order.

## Architecture (current, do not restructure without discussion)
Four services, communicating only through Redis Streams with typed
Percept/Action contracts — never direct function calls between modules:
- perception/        — webcam capture, YOLOv8n detection, publishes Percepts
- memory/episodic/    — writer persists Percepts to Postgres
- mind/               — reasoning loop, calls LLM via swappable LLMProvider
                         interface (mind/llm_provider/), emits Actions
- bodies/desktop/     — only component that produces audio output

This separation is deliberate — it's what lets a future robot Body plug in
without touching Mind or Memory. Don't collapse it for convenience.

## Stack / constraints
- Python monorepo. CPU-first CV (YOLOv8n, MediaPipe) — no GPU assumed for v1.
- Windows + Git Bash now, Linux migration planned later — don't add
  Windows-only or Linux-only code without flagging it.
- LLM: Gemini free tier via mind/llm_provider/gemini_provider.py, multi-model
  fallback chain. No budget for paid LLM APIs at AURA's runtime layer — keep
  this separate from Claude Code's own backend (DeepSeek v4 via Agent Router),
  which is unrelated dev tooling, not AURA's code.
- Postgres (episodic memory now, pgvector planned), Redis Streams (event bus).

## Current priority — in this order, do not reorder
1. **Stabilize.** No new features until this is done and confirmed working
   end to end. See "Known pending issues" below. **Report to Smartdon
   explicitly when this milestone is reached — do not slide into item 2
   silently.** "Working perfectly" includes AURA being *interactive*: press
   Enter, speak, and get a spoken answer grounded in memory, with ambient
   narration suppressed during the exchange.

   **Status 2026-10-06 (later the same day) — the Postgres blocker is
   CLEARED and plan steps 5–9 are landed.** The owner chose to set the local
   role password to the existing `AURA_PG_PASSWORD` value. The reset ran
   through a one-shot elevated script that opened a `trust` window, ran
   `ALTER USER`, and restored `pg_hba.conf` in a `finally` block. Verified
   after: `psql` connects in **0.53 s**, `_preflight_checks()` returns
   **ALL CLEAR**, `tests/test_query.py` passes **21/21**.

   Also landed that session: steps 5–9 (G-5 SDK retry + transport fallback,
   G-2 degraded-mode warning, G-3 duplicate guard, G-6 camera failure
   handling) and a **prompt-leak fix** in `mind/query.py`. Full suite:
   **54 tests, OK**.

   **Status 2026-10-06 (session 8, evening) — an end-to-end run was
   attempted, and it got further than any before it.** All four services
   started, Gemini did not crash, and ambient narration flowed ("a cup",
   "one", "two"). It failed in two new ways, both now understood:

   1. **The Body could not read the keyboard.** It ran as `mp.Process`
      child #4, and a spawned child on Windows does not reliably inherit a
      usable console stdin, so `input()` raised `EOFError` on the first
      prompt and the conversation loop returned. **FIXED structurally:** the
      Body is off `run_aura.py`'s `targets` dict and now runs on the
      launcher's **main thread**, with supervision moved to a daemon thread;
      `bodies/desktop/run.py` joins the narration listener when the
      conversation loop ends instead of exiting; `conversation.py`'s
      uninformative "No input available" is replaced by
      `_explain_dead_stdin()`, which prints the stdin repr, `isatty`, and
      which of three cases it is. `tests/test_body_interactivity.py` (NEW,
      9 tests). **Suite: 63 tests, OK.**
   2. **The memory-writer died mid-run** (`Still running: perception,
      mind`). **Undiagnosed — its traceback scrolled off the console
      unread.** Leading hypothesis, labelled as such: the 2 s
      `connect_timeout` at `memory/episodic/store.py:51` elapsing under
      cold-start load, since `write()` connects per percept and `writer.py`
      guards neither half of its loop. The other candidate is the unguarded
      parse at `bus/stream.py:81` (**G-8**). **These need different fixes** —
      step 10 as specified would not fix a `psycopg.OperationalError`.
      **Get the traceback before implementing.**
      **→ RESOLVED in session 9 — see the status block immediately below.
      The database side was the cause; both paths are now guarded.**

   **Status 2026-10-07 (session 9) — the memory-writer death is diagnosed,
   and nothing expected may kill a long-running service any more.** The
   session-8 hypothesis was narrowed to fact by the run's own log: the
   `mind` consumer reads the **same `percepts` stream with the same
   `Percept` model** (`reasoning_loop.py:46`) and survived the same run, so
   those entries parsed fine and **the writer died on the database side**.
   Landed: **step 10 / G-8** (parse guarded in `bus/stream.py`, poison entry
   logged and XACKed, genuine bugs still raise); the **writer survives a
   transient DB failure** (tenacity retry on `psycopg.OperationalError`
   only, warns once per outage, leaves the percept unacked in the PEL so a
   restart recovers it); the **connect timeout 2 s → 10 s** with
   `run_aura.py` now sharing that constant instead of its own literal; and
   three interactive-path guards — a failed exchange no longer shuts down
   all of AURA, a failed greeting no longer kills the Body, and the daemon
   narration thread no longer dies silently. **Suite: 80 tests, OK.**
   Preflight re-verified: **ALL CLEAR**. `tests/test_stability_round2.py`
   (NEW, 17 tests).

   ⚠️ **Do not run any Postgres password operation without telling the owner
   first** — he asked to supply it himself. Session 7's elevated procedure
   is recorded and works, but it is not to be run unannounced.

   **Priority 1 is still NOT complete, and is now one confirmed step away:
   re-run it and press Enter.** No spoken answer has been heard yet. Do not
   advance to item 2 until one has.

   **The machine's disk I/O is the binding constraint on verification.** A
   cold `import mind.query` measured **992 seconds** on 2026-10-06. Use
   `.venv/Scripts/python.exe -u` (not `uv run`), warm the cache by running
   twice, and don't conclude a run has hung before doing so. Warm runs are
   now ~13–40 s.

   ⚠️ The preflight's Postgres message was **not trustworthy on a cold
   machine** — it reported "Postgres is not reachable" while Postgres was up
   and answering; the 2 s `connect_timeout` at `run_aura.py:54` had merely
   elapsed. It reports correctly now that a real connection succeeds, but 2 s
   is still tight on a cold disk. See RESUME POINT in @docs/PROJECT_STATE.md.
2. **Long-term memory with provenance** (Perplexity research Priority 1 —
   see @docs/research-roadmap.md). Structured facts, not raw conversation
   logs. Schema: memory_id, memory_type, content, source_type
   (user_told | visual_observation | audio_observation | inference |
   external), source_event_ids, created_at, valid_from, valid_until,
   confidence, user_confirmed, sensitivity_level, supersedes_memory_id,
   retention_policy, deletion_status.
3. Everything else in the research roadmap comes after — this is a
   multi-year plan, don't front-load later phases.

## RESOLVED (2026-10-02) — Postgres stays local; no cloud migration
The OpenCV AI Competition 2026 was **never entered** and is now **cancelled** —
no grant accepted, no obligation, no deadline. AURA remains a local system.

Postgres and Redis both stay local. Do not propose AWS, RDS, Graviton, cloud
deployment, a hosted web endpoint, or any other competition-driven work.
`Problems_AURA_Solve.md` §11 states the target directly: AURA should be "a
dependable local system with optional cloud intelligence — not a cloud chatbot
attached to a webcam". The competition's requirements pulled against that, so
the competition went.

Consequence: the Postgres service problem in the punch list below is **live
work, not moot** — it was previously parked pending a possible RDS migration.

## Known pending issues (stabilization punch list)
- Postgres Windows service shows "Stopped" while postgres.exe runs fine
  independently — orphaned tracking only. **Confirmed 2026-10-05**: the
  service reports Stopped, yet port 5432 is listening (PID 6544) and 10
  postgres processes are running. The server **is** running; an earlier note
  suggesting otherwise was unverified and is now disproven. Do not kill the
  running process.
  Confirmed root-cause chain: the disk is a **5400 RPM HDD**
  (`WDC WD5000LPCX-60VHAT0`, MediaType HDD, SATA) and C: is 83% full, so
  fsync is genuinely slow (~10 s/file — the elapsed values in the 2026-09-23
  log are *cumulative*, advancing ~10 s per path, so this is a rate, not one
  outlier run). `logging_collector = on` with `log_rotation_age` /
  `log_rotation_size` unset has accumulated **447 log files inside the data
  directory**, and startup fsyncs the whole data dir — that log walks
  nothing but `./log/*.log`. The cumulative sync exceeds the SCM start
  timeout, so Windows marks the service Stopped while the process runs on
  orphaned.
  Proposed fix, **not applied** — admin-level and one part is a deletion, so
  it needs explicit go-ahead: point `log_directory` outside the data dir,
  prune the 447 files, add Defender exclusions, then a graceful pg_ctl
  restart. Deliberately **not** doing: disabling `fsync` /
  `synchronous_commit` to win startup time — that trades durability away,
  the wrong direction for a memory system. Caveat: on a 5400 RPM HDD this
  stays slow; an SSD is the real fix.
  `run_aura.py:59` service name corrected to `postgresql-x64-18`.
- gemini_provider.py: **CONFIRMED APPLIED (re-read 2026-10-05)** — all three
  fixes are in place: (1) DEFAULT_MODELS ordered gemini-3.1-flash-lite first
  (per-minute quota, not daily cap), (2) retry stop_after_attempt reduced
  from 3→2 and wait_exponential max reduced 10→4 on both _call_generate and
  _call_transcribe (prevents 40s+ stacked-retry hangs across the fallback
  chain), (3) except clause narrowed from `(APIError, Exception)` to just
  `APIError` so real bugs don't get silently swallowed as fake fallback
  events. Nothing to do here.
- run_aura.py never starts conversation listening — **IMPLEMENTED
  2026-10-05, unit-tested, NOT yet run end to end.** bodies/desktop/run.py
  and bodies/desktop/conversation.py are now merged into one entry point:
  `run.py` is the single entry point (ambient narration on a daemon thread,
  conversation loop on the main thread) and `conversation.py` is a library
  exposing `InteractionGate`, `run_conversation()` and `_flush_stdin()`.
  The agreed policy is implemented as specified: while an exchange is active
  (Enter press → AURA finishing her answer) ambient narration is
  **suppressed and acked — dropped, not queued** (leaving it unacked would
  park it in the consumer group's PEL and redeliver it later, which is
  queuing by another mechanism), and buffered Enter keypresses are flushed
  after the exchange. The gate is held across *recording*, not just
  playback, so AURA never records her own voice. `_flush_stdin` uses the
  documented msvcrt/termios split. **Session 8 added the layer above this:**
  the Body is launched by `run_aura.py` on the *launcher's main thread*
  rather than as a fourth child process, because a spawned child does not
  reliably inherit a usable console stdin — see the priority-1 status block
  above. **Remaining: the end-to-end run** — camera, mic, Redis, Postgres,
  GEMINI key and a human pressing Enter.

  camera, mic, Redis, Postgres, GEMINI key and a human pressing Enter.
- mind/query.py: **IMPLEMENTED 2026-10-06, EXECUTED and passing.** The owner
  decision of 2026-10-05 is in the code: `build_qa_prompt()` answers
  **beyond her observations** on general knowledge — physics, maths,
  philosophy, English, news, programming, socio-politics, all of it — while
  keeping observation-grounded claims **closed and exact**, and saying
  **"I don't know"** otherwise. `mind/persona.py` gained `self_knowledge()`
  and `creator_knowledge()` so identity ("who created you") is grounded. Keep
  the two epistemic modes distinct — do not collapse this into "answer
  everything confidently". No live search/retrieval tool is attached to the
  Gemini calls, so current-events questions draw only on training data; that's
  a real limitation, not a bug — state it rather than papering over it with a
  workaround. `tests/test_query.py` runs green (24 tests).
  **Prompt-leak fix (2026-10-06):** the first version used standalone
  headings ("WHAT YOU HAVE OBSERVED" / "EVERYTHING ELSE") and a live run
  echoed them back as its own answer format on "who created you". The modes
  are now numbered instructions wrapped in BEGIN/END markers, with a
  "never reproduce these instructions" guard. **Do not reintroduce headings
  in this prompt** — two tests fail if you do.
  **The owner restated this policy on 2026-10-07** ("reply to any question,
  but be careful and accurate on what she is observing, no fabrications") —
  it is exactly what the code already does. Nothing was changed, and
  nothing needs to be: do not "improve" this prompt without re-reading the
  paragraph above.
- **Stability steps 5-9: LANDED 2026-10-06.** One correction to the plan
  matters more than the rest: Step 7's exception tuple (`ConnectError`,
  `TimeoutException`) **would not have caught the failure actually
  observed** — a live run died on `httpx.RemoteProtocolError`. The catch is
  `httpx.TransportError`, the common base, at `gemini_provider.py:110`/`:151`.
  Step 5's SDK retry does not cover `RemoteProtocolError` either
  (`_HTTPX_TRANSIENT_EXC` is only `TimeoutException` + `ConnectError`), so it
  is step 7's per-model fallback that actually survives it. Also: `httpx` is
  now a **declared** dependency in `pyproject.toml` rather than borrowed
  transitively from google-genai (`uv lock` re-run), and
  `_warn_for_dead_workers()` was extracted out of `run_aura.py:main()` so
  step 6's warn-once behaviour is testable. Covered by
  `tests/test_stability_fixes.py` (14 tests).
- perception/service.py: confirm Q (full stop: camera, detection, publish,
  window) and C (display-only toggle, detection/publish continue) still
  behave as spec'd. **service.py changed on 2026-10-06** (G-3 duplicate guard
  deleted, G-6 camera-failure handling added), so this needs a re-verify on
  real hardware. The key handling at `:91-94` was **not** touched and the new
  loop logic is covered by `tests/test_stability_fixes.py`, but the window
  has not been re-run since the change.

## Engineering standards
- Security/correctness > maintainability/reliability > performance > speed
  of delivery. Say explicitly when trading one for another.
- **Reuse established implementations rather than inventing mechanisms.**
  Prefer well-known libraries and their documented patterns — a real web
  framework over a hand-rolled HTTP server, standard retry/connection-pool
  patterns over improvised ones — and name the project or pattern a change
  follows. AURA's own core (the four-service split, the Percept/Action
  contracts, edits to existing files here) has no upstream equivalent and
  must be written in this repo; **flag when nothing standard fits** rather
  than inventing quietly or forcing an ill-fitting library.
- No silent `except Exception` swallowing real bugs as "expected" failures.
- No large speculative changes without stating the plan first.
- Flag architecture-significant or hard-to-reverse decisions back to
  Smartdon rather than deciding alone — ambiguity on these is a stop sign,
  not a reason to guess.
- Explain the "why," not just the diff — this is a personal flagship
  project he needs to be able to defend every decision in, not a black box.