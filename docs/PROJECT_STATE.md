# AURA PROJECT STATE

Date: 2026-09-28 (audit) · updated 2026-10-07 (session 9)

Handoff document. Purpose: allow a new Claude Code session to resume this
engineering audit and remediation process without losing context.

---

## Session 9 — 2026-10-07: nothing may kill a long-running service

Session 8 ended with two live failures. One was fixed; the other was
undiagnosed. This session closed the second, landed **plan step 10**, and
removed a family of ways a single expected error could take AURA down.

### The memory-writer death — parse path RULED OUT, database path confirmed

Session 8 could only record two candidate causes and ask for a traceback
that never arrived. **The traceback is no longer needed, because the run's
own log answers it.**

`mind/reasoning_loop.py:46` consumes the **same `percepts` stream**, with
the **same `Percept` model**, through the **same `EventBus.consume()`** — and
the Mind survived the whole run (`Still running: perception, mind`). Both
consumer groups are created at `id="0"`, so both read the same entries. A
malformed message would have killed both consumers identically. The Mind
being unaffected therefore means **those entries parsed fine**, and the
writer died on the **database side** of its loop.

That leaves the 2 s `connect_timeout` as the leading explanation, still
unproven but no longer one-of-two. Both paths are now guarded regardless —
see below — so the writer cannot die that way again whichever it was.

**This is the second time in two sessions that reading the existing record
overturned a plan's assumption** (session 7: step 7's exception tuple;
session 9: G-8 being the writer's killer). Read the evidence before
implementing the fix.

### What landed

| Change | File | Why |
|---|---|---|
| **Step 10 / G-8** — the parse is guarded | `bus/stream.py` | Extracted to `_parse()`. Catches `ValidationError` / `KeyError` / `TypeError`, logs a warning naming entry, stream, group, consumer, **XACKs** the bad entry, and continues. A genuine bug in a consumer's model still raises. |
| **Writer survives a DB failure** | `memory/episodic/writer.py` | `tenacity` retry, 3 attempts, exponential backoff, **`psycopg.OperationalError` only**. On exhausted retries: warn **once per outage**, do **not** ack — the percept stays in the PEL and is re-delivered on restart. A `ProgrammingError` still raises. |
| **Connect timeout 2 s → 10 s** | `memory/episodic/store.py` | The documented-too-tight value. Evidence and the "do not lower this back" note are on the constant. |
| **Preflight shares the constant** | `run_aura.py` | It had its own literal `2`. Two independent literals is how they drifted too tight together; now the preflight cannot disagree with the connection the services actually make. |
| **A failed exchange is survivable** | `bodies/desktop/conversation.py` | Mic unplugged, Gemini rate-limited, Postgres slow — any of these used to propagate out of `run_conversation()` → the Body → `run_aura.py`'s `main()` and **shut down all of AURA**, perception included. Now: full traceback printed (not a silent swallow) and the loop continues. |
| **A failed greeting is survivable** | `bodies/desktop/run.py` | The greeting is LLM-generated; a rate limit at startup used to kill the Body before the conversation loop was ever reached. Only the *generation* is guarded — speaker/audio failures stay loud, or AURA would come up looking healthy while producing no sound. |
| **Narration thread survives** | `bodies/desktop/run.py` | It is a **daemon** thread: if it raised, it died without a trace, the process stayed up, and AURA silently stopped describing what she can see. Warn-once-per-outage, same shape as the camera and writer handling. |

**Test suite: 80 tests, OK** (63 after session 8 + 17 new in
`tests/test_stability_round2.py`).

**Preflight re-verified this session: `ALL CLEAR` in 7.6 s.** A direct
`psycopg.connect` measures **0.09–0.30 s** warm — so the 2 s timeout was not
being hit under *current* conditions. That is consistent with the failure
being cold-start-specific and is why the timeout raise is a precaution, not
a proven fix.

### The answering policy — already correct, unchanged

The owner restated it this session: reply to **any** question, but be
careful and exact about what she is observing, and never fabricate. That is
already what `mind/query.py:build_qa_prompt()` implements (two numbered
epistemic modes, observations closed inside BEGIN/END markers, everything
else open, no headings — see session 7's prompt-leak note). **No change was
needed and none was made.** Do not "improve" this prompt without re-reading
why the headings were removed.

### Password — NOT touched this session

No password operation was performed. The owner has asked to be told **before**
any password work so he can supply it himself. **If a session needs the
Postgres role password, say so and stop** — do not run an elevated reset
unannounced, even though session 7 established the procedure.
`AURA_PG_PASSWORD` is currently set in the environment and the preflight
succeeds with it.

### Still not done — Priority 1

**The end-to-end run has still not succeeded.** The keyboard fix from
session 8 is untested on real hardware, and the four interactivity criteria
below remain unconfirmed. Everything this session is verified by unit test
and preflight only.

---

## Session 8 — 2026-10-06 (evening): the Body could not read the keyboard, and the memory writer died

**The end-to-end run finally happened — and it failed in two new ways, both
now understood.** AURA started all four services, announced herself, began
narrating percepts ("a cup", "one", "two"), and then:

```
Press Enter to talk...
No input available — conversation loop stopping.
```

and separately, moments later, the supervisor reported
`Still running: perception, mind` — **the memory-writer had died**, with its
traceback scrolled off the console before it could be read.

### The interactivity failure — root cause and fix

`bodies/desktop/conversation.py` read stdin with `input()`, which raised
`EOFError` on the *first* prompt, so the loop returned immediately and the
Body process exited. The Body was starting as `mp.Process` child #4 of
`run_aura.py` (pid 3840 in the log), and **a spawned child on Windows does
not reliably inherit a usable console stdin.**

This was not proven by a direct probe. A multiprocessing stdin probe
(`%TEMP%\aura_stdin_probe.py`) was written and run, but it was
**inconclusive** — the tool's own stdin is the null device, so both parent
and child saw the same useless handle. CPython's `spawn.py` contains no
stdin handling, which rules out the library *deliberately* stripping the
handle, but does not explain what did.

**The fix does not depend on that explanation, and that is the point.**
Whatever the mechanism, the interactive component is the one component that
must own the console, so it now does so structurally:

- **`run_aura.py`** — `"body"` was removed from the `targets` dict. Only
  perception, memory-writer and mind are spawned. The Body runs on the
  **main thread** via `_run_body()`, inside `try / except KeyboardInterrupt
  / finally` that terminates and joins the three children. Supervision moved
  into a new module-level `_supervise()` running on a **daemon thread**,
  because the main thread is now blocked on stdin for the whole session.
  Module docstring records why the Body is not a child.
- **`bodies/desktop/run.py`** — `run_conversation()` returning no longer
  ends the process. It prints that AURA can't take questions, then
  **joins the ambient-narration listener**, so losing the keyboard no longer
  also loses narration. The listener is a daemon and the main thread still
  owns the exit, so Ctrl+C remains the way out.
- **`bodies/desktop/conversation.py`** — the useless `"No input available"`
  line is replaced by `_explain_dead_stdin()`, which prints the actual
  `sys.stdin` repr and `isatty`, and distinguishes three cases: no stdin at
  all (`pythonw`), stdin that is not a terminal (redirected, or an inherited
  non-console handle), and a real terminal that reported end-of-file. The
  old message cost a whole debug cycle to interpret; the new one names the
  cause.

**`tests/test_body_interactivity.py` — NEW, 9 tests, all passing.** AST
assertions that the Body is not in `targets`, that `_run_body()` is called
directly while `_supervise` is started on a daemon thread, that the children
are terminated in a `finally`; plus behavioural tests that a dead stdin is
reported as *which kind* of dead, that `EOFError` never escapes
`run_conversation`, and that `run.py` joins the listener after the loop.

### The memory-writer death — undiagnosed, hypothesis recorded

**Not fixed. The traceback was not captured**, so this is a hypothesis, and
it is labelled as one.

`memory/episodic/writer.py` is 11 lines of loop with **no guard on either
half of it**: `bus.consume()` parses unguarded (`bus/stream.py:81` — the
already-recorded **G-8**), and `memory.write()` opens a fresh connection per
percept with `connect_timeout=2` (`store.py:51`). Either one raising kills
the process, because nothing between them catches anything.

**Most probable cause: the 2 s connect timeout elapsing under cold-start
load.** The writer prints its banner *after* `EpisodicMemory()` succeeds, so
the schema connection worked; it then died on the drain path while three
processes were simultaneously paying cold-import cost on a 5400 RPM disk,
with the camera running. This is the same 2 s timeout that produced the
false "Postgres is not reachable" in session 6 — it is documented as tight
on this machine and it is on the writer's hot path, once per percept.

The parse-failure path (G-8) is the other candidate and is not excluded.

**The fix for both is the already-planned step 10** — guard the parse in
`bus/stream.py`, log, XACK the bad entry, continue — **plus** the writer
surviving a transient DB error rather than dying on it. Step 10's own
recorded scope covers the first; the second needs deciding, not assuming.
**Obtain the traceback before implementing**: step 10 as specified would not
fix a `psycopg.OperationalError`, and implementing it as though it would is
exactly the mistake session 7 caught in step 7's exception tuple.

### Test suite

**63 tests, OK** (54 before this session + 9 new). Run warm: 33 s.

### Still not done — unchanged from session 7

**Priority 1 is NOT complete.** The end-to-end run has now been *attempted*
and got further than ever — four services up, narration flowing, no crash
on the Gemini call — but **no spoken answer has been heard yet**, because
the keyboard never worked. Camera, mic, Redis, Postgres, GEMINI key and a
human pressing Enter are still to be exercised **together, successfully**.
**Do not advance to item 2.**

### Files changed this session

| File | Change |
|---|---|
| `run_aura.py` | Body off the child-process list; `_supervise()` on a daemon thread; main thread runs the Body; `finally` terminates children |
| `bodies/desktop/run.py` | joins the narration listener after the conversation loop returns |
| `bodies/desktop/conversation.py` | `_explain_dead_stdin()` replaces the bare "No input available" message |
| `tests/test_body_interactivity.py` | NEW — 9 tests |
| `docs/PROJECT_STATE.md` | this record + RESUME POINT |

---

## Session 7 — 2026-10-06 (later): Postgres unblocked, steps 5-9 landed, live G-5 failure caught and fixed

**This is the session that made AURA runnable — and it found a real bug the
audit had mis-specified.** The Postgres blocker is cleared, steps 5-9 are
landed, and a live run surfaced a transport failure that the plan's own
Step 7 would not have caught.

### The Postgres reset — CLEARED

Owner decision: set the local role password to the value already in
`AURA_PG_PASSWORD` (offered as a choice; chosen explicitly).

The first attempt, unelevated, **failed and changed nothing**:
`pg_ctl reload` returned `could not send reload signal (PID: 5884):
Operation not permitted` — the postmaster runs under a different account, so
signalling it needs Administrator. The script's `trap` restored
`pg_hba.conf` on exit, and verification confirmed the file was
**byte-identical** to its backup and that the live server still rejected a
wrong password — proving the `trust` window was **never loaded**. No
security window was ever open, and no data changed. This distinction is
worth recording: "the fix failed" and "the fix half-applied" are very
different states, and only the latter needs remediation.

The second attempt ran the same logic elevated, as one one-shot script:

1. back up `pg_hba.conf`
2. rewrite the three `all all` rules (`local`, `127.0.0.1/32`, `::1/128`)
   from `scram-sha-256` to `trust`
3. `pg_ctl reload`
4. `psql -f` a SQL file holding `\getenv pw AURA_PG_PASSWORD` followed by
   `ALTER USER postgres WITH PASSWORD :'pw'` — **the password was never
   printed, never in argv, and never written to disk**, because psql reads
   the variable itself
5. restore `pg_hba.conf` and reload again, in a `finally` block

Result: `PASSWORD CHANGED OK`, all six rules back to `scram-sha-256`.

**Verified after:** `psql` connects in **0.53 s** (PostgreSQL 18.1);
`_preflight_checks()` returns **`NONE - ALL CLEAR`** in 13.1 s;
`tests/test_query.py` passes **21/21**. The stray `pg_hba.conf.aura-backup`
was hash-verified identical to the live file before removal. The preflight's
Postgres message can be trusted again now that a real connection succeeds —
but the 2 s `connect_timeout` at `run_aura.py:54` is still tight on a cold
disk.

### G-5 observed in the wild — and the plan was wrong about the exception

A live `mind/query_local.py` run died on the owner's first question:

```
httpx.RemoteProtocolError: Server disconnected without sending a response.
```

This is G-5 exactly as predicted, and a **better data point than anything in
the audit**. Two corrections follow, both verified against the installed
source:

| Claim | Reality |
|---|---|
| Step 7's planned catch, `(APIError, httpx.ConnectError, httpx.TimeoutException)` | Would **not** have caught it. `RemoteProtocolError` is a `ProtocolError` → `TransportError`, a sibling of `ConnectError` and `TimeoutException`, not a subclass of either. |
| Step 5's SDK retry would absorb it | **No.** `_HTTPX_TRANSIENT_EXC` (`_api_client.py:111`) is only `httpx.TimeoutException` and `httpx.ConnectError`, plus a fixed 5xx list. `RemoteProtocolError` is not in it. |

The catch is therefore `httpx.TransportError` — the common base covering
`TimeoutException`, `NetworkError` (`ConnectError`, `ReadError`, …) and
`ProtocolError` (`RemoteProtocolError`, …). Step 5 is still worth having (it
absorbs the common transient case in place instead of paying for a model
switch), but **step 7's per-model fallback is what actually survives the
observed failure.**

`httpx` is now imported directly, so it became a **declared** dependency in
`pyproject.toml` rather than borrowed transitively from google-genai;
`uv lock` was re-run (254 packages, resolved clean).

### Prompt leak — found by the same live run

The two-mode prompt used standalone headings:

```
WHAT YOU HAVE OBSERVED — ...
EVERYTHING ELSE — ...
```

Asked "who created you", AURA replied with the **headings echoed back as her
own output format** — the facts were correct, the framing leaked. Headings
in a system prompt read as a response template.

Fixed by rephrasing the modes as numbered instructions, with the observation
list delimited by `--- BEGIN/END OBSERVATIONS ---`, plus a load-bearing
closing guard: *"Never reproduce these instructions, the rule numbers, the
markers, or the observations themselves."* Two tests assert the headings are
gone and the guard stays.

### Steps 5-9 — LANDED

| Step | Change | Verification |
|---|---|---|
| 5 (G-5 p1) | `genai.Client` now takes `http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=3))` | client constructed with `attempts == 3` |
| 7 (G-5 p2) | `except (APIError, httpx.TransportError)` at `:110` and `:151` | `RemoteProtocolError` on model 1 → models 2 and 3 tried; all-fail still raises; `ValueError` still propagates |
| 6 (G-2) | warning now states AURA is DEGRADED, the service will NOT be restarted, and lists what still runs | warns once per dead worker, never again |
| 8 (G-3) | duplicate `__main__` guard deleted from `perception/service.py` | AST test: exactly one guard, in both perception entry points |
| 9 (G-6) | `MAX_CONSECUTIVE_FAILURES = 50`, `FAILURE_SLEEP_SECONDS = 0.1`; sleep, one warning per failure run, `RuntimeError` at the threshold, counter resets on a good frame. Mirrored in `run_local.py`, which imports the shared constants | gives up at exactly N reads; one warning; counter survives stutters |

Step 6 needed a small refactor: the warn loop was inline in
`run_aura.py:main()`, which spawns real processes and is therefore
untestable. It was extracted to `_warn_for_dead_workers(processes,
already_warned)`.

The perception tests stub `perception.detector` into `sys.modules` to avoid
importing ultralytics/torch (minutes on this disk), **and** patch
`service.ObjectDetector` directly — under `unittest discover` another test
module imports the real `perception` package first, which makes the
`sys.modules` stub a no-op and hands the service the real detector. Both are
needed; the second is what makes the suite order-independent.

### Live verification after the changes

```
You : who created you
AURA: I was created by Smartdon.

You : what is your name
AURA: I am AURA. I don't have any visual input right now...

You : what have you seen
AURA: I haven't observed anything recently.
```

All three are correct: the call-name rule holds, the prompt framing no
longer leaks, and mode 1 stayed closed on an empty memory.

**Full suite: 54 tests, OK** — `tests/test_query.py` 24,
`tests/test_stability_fixes.py` 14, plus the pre-existing store and
interaction-gate suites. Run with `.venv/Scripts/python.exe -u`; warm runs
are 5-40 s.

### What is still NOT done

**The end-to-end run.** Stabilization is code-complete but **unconfirmed**:
camera, mic, Redis, Postgres, GEMINI key and a human pressing Enter have
never been exercised together. **Priority 1 is not complete.**

`perception/service.py` changed this session, so its Q/C key behaviour needs
a re-verify on real hardware — the key handling itself was not touched.

### Files changed in this session

| File | Change |
|---|---|
| `mind/llm_provider/gemini_provider.py` | `import httpx`; SDK retry enabled; `TransportError` caught at both sites |
| `mind/query.py` | prompt rephrased, markers added, echo guard added |
| `run_aura.py` | `_warn_for_dead_workers()` extracted; degraded-mode wording |
| `perception/service.py` | camera-failure handling; duplicate guard deleted |
| `perception/run_local.py` | mirrors step 9, imports the shared constants |
| `pyproject.toml` | `httpx>=0.28` declared |
| `uv.lock` | re-resolved |
| `tests/test_query.py` | 3 assertions updated to the new prompt; 3 tests added |
| `tests/test_stability_fixes.py` | NEW — 14 tests covering steps 5-9 |

---

## Session 6 — 2026-10-06: query policy implemented, machine I/O collapse blocks verification

**The dominant fact of this session is environmental, not code.** The
machine's disk I/O has collapsed to the point where a cold import takes
minutes. Nothing about this session's code change has been verified by
execution, and that is the first thing the next session must fix. See
[The 992-second import](#the-992-second-import--new-blocking-constraint).

### Branch determination — and a corrected diagnosis

The resume point's one check was run:

```bash
uv run python -c "import run_aura; print(run_aura._preflight_checks() or 'CLEAR')"
```

It reported **`Postgres is not reachable (connection timeout expired)`** —
which reads as a dead server, and is wrong.

| Check | Observed |
|---|---|
| `Get-Service *postgres*` | `postgresql-x64-18` — **Running** (Session 5 recorded *Stopped*) |
| `Get-NetTCPConnection -LocalPort 5432 -State Listen` | listening, PID **5884** (was 6544 in Session 5) |
| `Get-Process postgres` | **10 processes** |
| Raw TCP connect, 127.0.0.1:5432 | **OK, 0.10 s** |
| Raw TCP connect, ::1:5432 | **OK, 0.00 s** |
| `psycopg.connect(DEFAULT_DSN, connect_timeout=30)` | **FAILS in 1.34 s** — `FATAL: password authentication failed for user "postgres"` |

So the server is **running and answering promptly**, and the real failure is
**the same blocker as Session 5**: `AURA_PG_PASSWORD` is set (14 chars) and
is not the password the `postgres` role holds.

**The `connection timeout expired` was a transient artifact.** `run_aura.py:54`
passes `connect_timeout=2`; at the moment the check ran, the box was mid
cold-start (thrashing the 5400 RPM HDD), and two seconds of wall clock
elapsed before the connect completed. Once warm, the same connect fails in
1.34 s with the true auth error.

**This is a real defect in the preflight's diagnostic value, recorded not
fixed.** `run_aura.py:57-64` reports any Postgres exception as *"Postgres is
not reachable"* and instructs the operator to run
`net start postgresql-x64-18` — which, on this machine, is exactly the wrong
action, because the service is already up and the problem is the password.
A 2 s budget is too tight for a cold start here. **No code was changed**:
the timeout value is H-4 / step-3 territory and the message wording is a
presentation decision for the owner.

### The 992-second import — NEW BLOCKING CONSTRAINT

Measured, not inferred:

```bash
.venv/Scripts/python.exe -u -c "import time; t0=time.time(); import mind.query; print(time.time()-t0)"
# IMPORT OK 992.2s        ← 16.5 minutes, and it succeeded
```

Consequences, all observed this session:

| Command | Outcome |
|---|---|
| `uv run python -m unittest discover -s tests -v` | **timed out at 8 m 20 s**, exit 143 |
| `.venv/Scripts/python.exe -m unittest tests.test_query -v` (background) | killed, **0 bytes of output** |
| same, with `-u` unbuffered | **0 bytes of output** |
| `import mind.query` alone | **992.2 s**, then OK |
| `Get-Process` via PowerShell | **timed out at 2 m** |

**Therefore: `tests/test_query.py` has never been executed.** It is written
and its author believes it is correct, but that is not evidence. Treat the
suite as **unverified** until it runs.

**Mitigations to try first next session** (none applied):

- Use `.venv/Scripts/python.exe` **directly**, not `uv run` — `uv` adds its
  own resolution work on top of an already disk-bound start, and it is what
  consumed the 8 m 20 s.
- Re-run the *same* command a second time: the 992 s figure is a **cold-cache**
  reading, and the hypothesis that a warm re-run is far faster is
  **untested** — it was about to be measured when the session ended. Warm it
  before concluding anything about runtime cost.
- Check Windows Defender exclusions on `.venv` and the Postgres data dir.

**Do not record any check as passing on the basis of this session.**

### Disk / Postgres environment (updated readings)

| Metric | Session 5 | Session 6 |
|---|---|---|
| `C:` used | 83% (84.5 GB free) | **79%** (98 GB free) — improved |
| Files in `...\18\data\log` | 447 | **450** — still growing, still unpruned |
| Service status | Reported **Stopped** | Reports **Running** |
| Postgres PID | 6544 | **5884** — the machine has rebooted since |

The Session 5 root cause (5400 RPM HDD + `logging_collector` + no
`log_rotation_*` + logs inside the data dir) remains **untouched and
unapproved**. Punch-list item 1 still needs the owner's explicit go-ahead.

### IMPLEMENTED — `mind/query.py` two-mode answering policy

The owner decision of 2026-10-05 is now **in the code**. Changed:

| File | Change |
|---|---|
| `mind/query.py` | `build_qa_prompt()` rewritten. The old *"Answer the user's question using only what's in your observations above"* is **gone**. Two explicitly separated modes: **WHAT YOU HAVE OBSERVED** (closed — the memory list is complete and exact, never embellished, "say you haven't seen it" if absent) and **EVERYTHING ELSE** (open — science, maths, philosophy, language, programming, history, politics answered freely). Plus: say **"I don't know"** when she doesn't; she cannot browse, so recent news draws only on training data and she must say she can't check rather than invent. Module docstring updated with this scope and the no-retrieval limitation. |
| `mind/persona.py` | New `self_knowledge()` (facts about AURA) and `creator_knowledge()` (facts about the owner), plus `CREATOR_FULL_NAME` / `CREATOR_CALL_NAME`. Both are fed into the Q&A system prompt. Module docstring updated to say persona.py holds the creator facts too, and that **phase 2 memory is their eventual home** (`source_type="user_told"`, `user_confirmed=True`). |
| `tests/test_query.py` | **NEW — 20 tests. NEVER EXECUTED.** Covers: the old observation-only restriction is gone; both modes present; observations stay closed; "I don't know" instruction; the cannot-browse limitation; identity and creator grounding present; question passed as `user_prompt`; memory window args unchanged; `SPEAK`/`NORMAL` action shape; injected memory is used rather than constructing one (the G-9 seam). |

Not changed, deliberately: `mind/greeting.py` still uses
`system_prompt()` untouched — the Q&A prompt does **not** reuse it, per
J-4. `Persona.system_prompt()` is byte-identical, and a test asserts
`"Smartdon"` does **not** appear in it.

### Creator profile — supplied by the owner, 2026-10-06

AURA is now grounded on **Hammed AbdulRahman Olabidemi**, who goes by
**Smartdon** and is to be addressed that way (never the given name) — the one
behavioral rule in the profile. Also recorded: mechatronics engineering
student at Bayero University Kano, level 2; focus on AI, computer vision,
embedded systems, edge AI, Physical AI; CEO of Smartdon Robotics Automation
and Software Solutions (SRASS) with products Beacon, Swarm AI, Quantum X,
NLens; born 18 June, Gemini; role model Nikola Tesla; drawn to
electromagnetism for power and motion; aims to build new inventions and a
technology legacy.

**Every claim came from the owner.** Nothing was inferred — an earlier draft
had a docstring stating there was deliberately no creator name in the repo,
and that docstring was corrected rather than left standing once the name
existed.

The prompt states explicitly that **AURA is the owner's own project and not
one of the SRASS products**, matching CLAUDE.md's separation. Without that
line a model holding both fact sets would reasonably assume AURA was a fifth
SRASS product.

**OPEN — owner to decide.** This profile is now part of the system prompt
sent to the **Gemini API on every question**. Gemini's *free tier* terms
generally permit using submitted content to improve their products — the
owner should check the current terms themselves before accepting this. It
sits against `Problems_AURA_Solve.md` §11 ("a dependable local system with
optional cloud intelligence"). Options, cheapest first: accept it; keep the
profile out of the prompt and retrieve it from phase-2 memory only when
relevant; or move the provider to a local model (a one-line change in
`llm_provider/__init__.py`). **No action taken — flagged, not decided.**

### Stability items — verified against the code this session (all still unfixed)

Read directly, not quoted from earlier sessions:

| Step | File / line | Verified current state |
|---|---|---|
| **5** (G-5 p1) | `gemini_provider.py:53` | `genai.Client(api_key=key)` — **no `http_options`**. SDK retry is off; `retry_args(None)` ⇒ `stop_after_attempt(1)`. |
| **7** (G-5 p2) | `gemini_provider.py:110` | still `except APIError as err` — does not catch `httpx.ConnectError`. |
| **6** (G-2) | `run_aura.py:122-128` | still warns **once** then suppresses forever; no restart. |
| **8** (G-3) | `perception/service.py:99-104` | duplicate `if __name__ == "__main__": main()` still present. |
| **9** (G-6) | `perception/service.py:72-75` | `if frame is None: continue` — still no sleep, no log, no bound. |

**Steps 5-9 are the shortest path to a testable AURA** and are all recorded,
owner-reviewed plan items. They were **not** started this session.

### Files changed in this session

| File | Change | Verified? |
|---|---|---|
| `mind/query.py` | two-mode policy; module docstring | ⚠️ **imports cleanly (992 s), never executed functionally** |
| `mind/persona.py` | `self_knowledge()`, `creator_knowledge()`, `CREATOR_*`; docstring | ⚠️ same |
| `tests/test_query.py` | **NEW** — 20 tests | ❌ **never executed** |
| `docs/PROJECT_STATE.md` | this record + RESUME POINT | ✅ read back |

No other source file touched. **No commit was made** (still 0 commits; the
whole tree is untracked).

### Operational findings

- **Cold `import mind.query` = 992.2 s.** The single most important
  operational fact of this session. Every prior session's "run it through
  Bash" advice still holds, but speed is now the binding constraint.
- **`uv run` is the slow path on this machine** — `uv run python -m unittest`
  exceeded 8 m 20 s. Prefer `.venv/Scripts/python.exe` (confirmed 3.12.14).
- **Python block-buffers stdout when redirected.** Redirecting to a file
  yields **0 bytes** until the process exits, so a killed run leaves nothing
  to read. Use `-u`.
- A `PowerShell` `Get-Process` listing timed out after 2 minutes — the
  Session 4 finding (PowerShell hangs after a Python child) is now joined by
  plain slowness on any WMI cmdlet while the disk thrashes.
- `docs/PROJECT_STATE.md` is large (~26 k tokens for the first 1621 lines).
  Read it in slices, or grep for a section heading, rather than in full.

---

## Session 5 — 2026-10-05: Step 3 verified, interactivity built, Postgres root-caused

**Execution was available for most of this session.** The classifier outage
recurred intermittently (a bare `echo` would pass while a `python`
invocation was refused in the same minute — confirming again that a
refusal does not prove a full outage). It also, for the first time this
project, refused **`Edit` and `Write`**, not just shell commands. All work
below was completed once it cleared.

### Step 3 (H-4) — **VERIFIED**

Run through **Bash**, as Session 4 required:

```bash
uv run python -m unittest discover -s tests -v
```

**Result: 9 tests, 8 passed, 1 failed** — and the failure was in the
*test*, not the code. `ConnectTimeoutTests._connect_calls` trimmed
construction-time connects off `call_args_list`, but
`test_ensure_schema_connect_is_bounded` then asserted that exact trimmed
call was present. Zero connects were left to inspect.

Fixed by having `_connect_calls()` return `(construction_calls,
exercise_calls)` as a pair. The connect made during `__init__`
(`_ensure_schema`) is itself under test, so it must stay addressable
rather than being trimmed as noise. **Re-run: 9/9 pass.**
`connect_timeout=_CONNECT_TIMEOUT` is confirmed on all four connect sites.

Note: bare `python` resolves to the system 3.11 and fails with
`ModuleNotFoundError: No module named 'psycopg'`. **Always use
`uv run python`,** not bare `python`, for anything importing project deps.

### Punch-list item 1 (Postgres Windows service) — **ROOT CAUSE ESTABLISHED**

The Session 4 note that the server "is **not currently running**" was
**UNVERIFIED** and is now **DISPROVEN**. The server is running.

| Check | Observed |
|---|---|
| `Get-Service *postgres*` | `postgresql-x64-18` — **Stopped**, Automatic |
| `Get-NetTCPConnection -LocalPort 5432 -State Listen` | **listening**, PID 6544 |
| `Get-Process postgres` | **10 processes** running |

So the service is orphaned in SCM's view only. Verified cause chain:

1. **The disk is a 5400 RPM HDD** — `Get-PhysicalDisk` reports
   `WDC WD5000LPCX-60VHAT0`, `MediaType: HDD`, `BusType: SATA`. This is
   the root enabler: fsync is physically slow here, not merely
   mis-tuned. `C:` is also **83% full** (84.5 GB free of 500 GB).
2. **447 log files** in `C:\Program Files\PostgreSQL\18\data\log` — count
   confirmed. Newest is `postgresql-2026-09-29_000000.log` at **0 bytes**;
   no October logs.
3. `postgresql.conf` has `logging_collector = on` and
   `log_destination = 'stderr'`, and **`log_rotation_age` /
   `log_rotation_size` are not set** — so daily rotation has been
   accumulating since install with nothing pruning it. (New finding;
   Session 4 did not have this.)
4. Those files live **inside the data directory**, and startup fsyncs the
   whole data dir. The 2026-09-23 log shows the sync walking *nothing
   but* `./log/*.log` entries.
5. **`elapsed time` is cumulative, not per-file** — the 2026-09-23 run
   reads `140.17 → 150.01 → 160.19 → 170.03 → 180.04 s`, advancing ~10 s
   per reported path. That is the ~10 s/file rate, now confirmed as a
   rate rather than inferred from the one 73,329 s outlier.
6. The cumulative sync blows past the SCM start timeout, so Windows marks
   the service Stopped while `postgres.exe` carries on orphaned — the
   exact observed symptom.

**Deliberately NOT done.** `fsync`, `synchronous_commit` and
`wal_sync_method` are unset (i.e. at durable defaults) and were **left
alone**. Turning durability off to win startup time is the wrong trade
for a memory system, and would mask the real problem. The proposed fix
set — `log_directory` pointed outside the data dir, pruning the 447
files, Defender exclusions, then a graceful `pg_ctl` restart — is
**admin-level and partly a deletion, so it needs the owner's go-ahead.
Nothing was changed.**

Honest caveat to carry forward: on a 5400 RPM HDD this stays slow. An SSD
is the real fix, and that is hardware, not config.

### `run_aura.py` — service name corrected

`:59` said `postgresql-x64-17`; the installed service is
`postgresql-x64-18`. Corrected. (This was a Session 4 punch-list item.)

### Punch-list item 3 (make AURA interactive) — **IMPLEMENTED, UNIT-TESTED, NOT RUN END TO END**

The two entry points are merged into one.

- **`bodies/desktop/run.py`** is now the single entry point: intro →
  start the ambient-narration bus loop on a **daemon thread** →
  run the conversation loop on the **main thread**.
- **`bodies/desktop/conversation.py`** is now a **library, not an entry
  point**. It exports `InteractionGate`, `run_conversation()` and
  `_flush_stdin()`. Its `__main__` block prints a pointer to
  `bodies/desktop/run.py` rather than dead-ending silently.

**`InteractionGate` is one `threading.Event` plus one `threading.Lock`**
— three constraints reduce to "these two must not overlap":

- `exchange()` — held for the whole press-Enter-to-answer-finished
  window. Sets the suppression flag, then takes the speak lock.
- `ambient_speech()` — yields `True`/`False`; used by the bus thread.

**A real bug was caught while writing the tests, and fixed.** The first
version of `ambient_speech()` took the speak lock *before* testing the
flag. Because an exchange holds that lock for its entire duration, an
ambient call would **block for the whole exchange and then speak the
moment it ended** — narration piling up behind the conversation and
firing after it. That is queuing under another name, i.e. exactly what
the agreed policy forbids. Fixed by testing the flag *first* (yield
`False` immediately, never touching the lock), with a second test inside
the lock to close the race where an exchange starts while the call was
waiting on an earlier narration. Regression test:
`test_narration_does_not_block_behind_an_active_exchange`.

**Suppressed narration is ACKED.** Leaving it unacked would park it in
the consumer group's PEL and have it redelivered later — queuing by
another mechanism. So the drop is made permanent with the ack.

**The gate is held across *recording*, not just playback** — the
microphone is open for `RECORD_SECONDS`, so anything AURA says in that
window would be transcribed back as though the user had said it. This is
why `exchange()` deliberately sets the flag *before* taking the lock:
an in-flight narration finishes, then the loop proceeds.

**`_flush_stdin()`** uses the documented platform split — `msvcrt` on
Windows, `termios.tcflush` on POSIX — because the standard library has
no portable "drain stdin". Both branches are present; nothing is
Windows-only. Narrow `except (ImportError, OSError, ValueError)`, not a
bare except.

**`tests/test_interaction_gate.py` — NEW, 7 tests.** Bus and speaker are
stubbed, so it needs no camera, mic, Redis or API key. Covers: speaks +
acks when idle; dropped + still acked during an exchange; the
non-blocking regression; narration resumes after the exchange; non-SPEAK
intents acked without speaking; two narrations never overlap; `_flush_stdin`
is safe with no console.

**Suite total: 16 tests, all passing.**

**NOT verified end to end.** This needs the camera, the microphone,
Redis, Postgres, a `GEMINI_API_KEY` and a human pressing Enter. Nothing
about the runtime behaviour has been observed yet — only the coordination
logic, under stubs.

`decision/policy.py:21` carries a comment referring to `conversation.py`;
still accurate in substance (that is where direct questions are handled),
left as-is. Nothing else in the tree referenced `conversation.main`.

### Punch-list item 4 (`mind/query.py`) — **OWNER DECISION RECORDED, CODE NOT YET CHANGED**

CLAUDE.md said to flag this rather than silently work around it. Flagged;
the owner decided on **2026-10-05**:

- AURA **should** answer beyond her observations — general knowledge
  across physics, maths, philosophy, English, news, programming,
  socio-politics, "all of it".
- She must **not** produce false output about **anything she is
  observing**.
- If she doesn't know, she should **say "I don't know"**.

**The policy is settled; the code still reflects the old behaviour.**
`build_qa_prompt()` at `mind/query.py:46-55` still instructs *"Answer the
user's question using only what's in your observations above"*, and
`answer_question()` has no self-identity grounding. **This is the work
item that follows from the decision — it has not been started.**

Separately, and still true: no live search/retrieval tool is attached to
Gemini calls, so news and current-events questions draw only on training
data. That is a real limitation to state, not a bug to paper over.

### Pre-commit hygiene — **all three gate conditions now met**

The RESUME POINT previously said: *do not commit until `.gitignore` has
`__pycache__/` and `*.pyc` rules, the two password literals are replaced,
and `README.md` documents `AURA_PG_PASSWORD` setup.* All three are done:

- **`.gitignore`** — now carries `__pycache__/`, `*.py[cod]`, `*$py.class`
  plus the standard github/gitignore "Python" template sets (`.venv/`,
  build output, tool caches). Added `*.pt`, since ultralytics
  re-downloads `yolov8n.pt` and it is regenerable. Verified with
  `git check-ignore -v __pycache__` → matches `.gitignore:19`, and
  `__pycache__/` has disappeared from `git status`.
- **`README.md`** — password literal replaced. This file also turned out
  to contain **~375 lines of pasted terminal output** (a `getaddrinfo`
  `ConnectError` traceback and ~160 repeated `WARNING: ... has stopped
  unexpectedly` lines) accidentally left in the file from line 84; all
  removed. The "No conversation yet" scope limit was **now false** after
  item 3 and has been rewritten. `AURA_PG_PASSWORD` setup is now
  documented, with the real defaults stated.
- **`memory/episodic/writer.py:8`** — the Docker
  `POSTGRES_PASSWORD=...` example replaced with environment-variable
  guidance, which also corrects a misleading instruction: the real setup
  is the local Windows Postgres 18 service, not Docker.

**Caveat:** the repo still has **no commits**, and the tree is all
untracked. These conditions gate the *first* commit; they do not
constitute one.

### NEW BLOCKER — `AURA_PG_PASSWORD` is rejected by the server

`run_aura.py` preflight reports: `password authentication failed for user
"postgres"`. Diagnosis:

- The server **is** reachable and answering — this is an auth failure,
  not a connection failure. Redis and `GEMINI_API_KEY` both pass.
- `pg_hba.conf` active rules are **`scram-sha-256` on every path**
  (local, 127.0.0.1/32, ::1/128), so a correct password is mandatory.
  There is no `trust` shortcut.
- `AURA_PG_PASSWORD` **is set** — 14 characters. Checked without ever
  printing the value: no leading/trailing whitespace, no whitespace
  anywhere, no non-printable or non-ASCII characters. A stray `\r` from
  a Windows `setx` is ruled out.
- It is **not** the old hardcoded literal (verified by substring test
  against `writer.py`, `README.md` and `PROJECT_STATE.md` — all `False`).

So it is simply not the password the `postgres` role holds.
`_build_dsn()` percent-encoding is **not** implicated: the
`DsnEscapingTests` round-trip asserts `parsed["password"] == password`
for `p@ssw0rd`, `p:ss`, `p/ss`, `p#ss`, `p?ss`, and passes.

**Owner states (2026-10-05) they know the password themselves.** So this
is the owner's to resolve by setting the environment variable
correctly — **it is not a code defect and no code change is warranted.**
Once set, `run_aura.py` should clear preflight and item 3 becomes
end-to-end testable.

**Separately flagged, not changed:** `DEFAULT_DSN` resolves to database
**`postgres`** — the server's default maintenance database — because
`_DEFAULT_DB = "postgres"` in `store.py:44`. Running the application
schema inside the maintenance DB is unusual; a dedicated `aura` database
is the conventional choice. This is a design question for the owner, not
something to change unilaterally.

### `.claude/settings.local.json` — owner-supplied allowlist added

The owner supplied a `permissions.allow` block (`Bash(git *)`,
`Bash(python -m unittest *)`, `Bash(pytest *)`, `Bash(uv *)`,
`Bash(pip *)`), merged in above the existing session-specific entries.

**Noted for the owner, not acted on:** `Bash(uv *)` permits
`uv run python <anything>`, which is arbitrary code execution without a
prompt, and `Bash(git *)` covers destructive git verbs (`reset --hard`,
`clean -fdx`, `push --force`). These are broad by design — they exist to
stop the classifier outage from blocking work — but the breadth is worth
knowing.

### Read-only confirmations obtained this session

- `perception/service.py` `q`/`c` key handling — re-read and still
  matches spec (Q = full stop of camera/detection/publish/window; C =
  display-only toggle with detection and publish continuing). **Still
  not re-verified at runtime**, which is what the punch list asks for.
- `mind/llm_provider/gemini_provider.py` — all three punch-list fixes
  remain applied (model ordering, retry bounds, narrowed except clause).

### Operational findings

- **`PowerShell` still hangs after a Python child exits**, even on
  success (reconfirmed, exit 143). Continue to run Python through
  **`Bash`**.
- **`grep -r` via Git Bash is pathologically slow on this machine** and
  timed out at 120 s scanning the tree; it does not respect
  `.gitignore`, so it walks `.venv`. Use the ripgrep-backed search tools
  on narrow paths instead, or exclude `.venv` explicitly.
- `Glob` remains unreliable on large trees (`C:\Program Files\...`).

### Files changed in this session

| File | Change |
|---|---|
| `memory/episodic/store.py` | *(Session 4)* `connect_timeout` on all four connects — now **verified** |
| `tests/test_store.py` | `_connect_calls()` returns `(construction, exercise)`; 9/9 pass |
| `bodies/desktop/run.py` | Rewritten — single entry point, ambient narration on a daemon thread |
| `bodies/desktop/conversation.py` | Rewritten — library module: `InteractionGate`, `run_conversation`, `_flush_stdin` |
| `tests/test_interaction_gate.py` | **NEW** — 7 tests |
| `run_aura.py` | `postgresql-x64-17` → `-18` in the preflight message |
| `.gitignore` | `__pycache__/`, `*.py[cod]`, `.venv/`, build/caches, `*.pt` |
| `README.md` | Password literal removed; ~375 lines of pasted output removed; scope limits corrected; env setup documented |
| `memory/episodic/writer.py` | Docker password example → env-var guidance |
| `.claude/settings.local.json` | Owner allowlist merged in |

---

## Session 4 — 2026-10-02: execution restored, steps 1-2 verified

**The queued command executed for the first time.** It had been attempted
three times in Session 3 and never ran. On this session's first attempt it
ran to completion and produced real output. Its results are recorded below
as **observed**, not predicted.

Command run (the exact queued command from Session 3, unmodified):

```powershell
$py = ".venv\Scripts\python.exe"
git check-ignore --no-index -v start-claude.sh .env secrets.env
& $py -c "import sys; print(sys.version)"
& $py -m unittest discover -s tests -v
```

### Observed results

| Check | Output | State |
|---|---|---|
| `git check-ignore` | `.gitignore:7:start-*.sh  start-claude.sh` / `.gitignore:14:*.env  .env` / `.gitignore:14:*.env  secrets.env` — exit 0 | ✅ **Phase 0 `.gitignore` assertion CLOSED.** All three paths ignored. |
| Interpreter | `3.12.14 (main, Sep  1 2026) [MSC v.1944 64 bit (AMD64)]` | ✅ venv is the pinned 3.12 |
| Step 1 tests | 3 × `ok` | ✅ **Step 1 (H-2) is now VERIFIED** |
| Step 2 pre-fix test | **FAILED** on `p@ssw0rd` and `p/ss` | ✅ audit's Step 2 premise **REPRODUCED** |

Step 2's observed failures, verbatim in substance: for `p@ssw0rd` the parsed
host was `ssw0rd@db.example.invalid`; for `p/ss` it was `postgres`. The
other three subtest values (`p:ss`, `p#ss`, `p?ss`) did not break the
authority section, which is consistent with the finding's mechanism.

Because the pre-fix test was **observed failing** (the resume point's
precondition) and not merely predicted, Step 2 was implemented.

### Implemented and verified this session

| Step | State |
|---|---|
| **Step 1** (H-2) | ✅ **VERIFIED** — 3 tests pass, observed |
| **Step 2** (DSN escaping) | ✅ **IMPLEMENTED AND VERIFIED** — `from urllib.parse import quote` added; `_build_dsn()` now returns `f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{db}"`; a paragraph was added to its docstring explaining the encoding. Re-ran the suite: **4 tests, `OK`, exit 0.** |
| **Step 3** (H-4) | ⚠️ **IMPLEMENTED, NOT VERIFIED.** `_CONNECT_TIMEOUT = 2` added with a comment recording psycopg's 130 s default and that `write()` connects per row; `connect_timeout=_CONNECT_TIMEOUT` passed at all four `psycopg.connect` sites. A 5-test class (`ConnectTimeoutTests`) was written but **has never been executed** — the classifier went down again before it could run. |

**Steps 4 and later: NOT STARTED.** No other source file modified.

### Recurrence of the Session 3 blocker — and a new operational finding

The command-safety classifier went down again during this session
("deepseek-v4-flash is temporarily unavailable"), on **both** the `Bash` and
`PowerShell` tools. It is the same intermittent condition Session 3
recorded, and it is what left Step 3 unverified. Read-only operations
(file reads, grep, glob) were unaffected throughout.

**New operational finding — the `PowerShell` tool hangs after a Python
child exits.** Two separate `PowerShell` invocations each ran the command
successfully and printed full output, then **failed to terminate**, being
killed at the tool timeout with exit code **143** — including on a run whose
test suite reported `OK` / exit 0. The `Bash` tool does not exhibit this:
the identical command returned promptly with `exit=0`.

**How to apply:** run the test suite through `Bash`, not `PowerShell`, and
redirect output to a file (or pipe through `tail`) so a killed run still
yields the output captured before the kill.

**How to tell an outage from a block.** During the outage, `echo
classifier-probe` executed normally while every command that actually
*invokes Python* was refused with the same "temporarily unavailable"
message — on both tools. So the outage does **not** silence all execution;
it only refuses commands that need real classification, while trivially-safe
ones still pass. A refused command therefore does not by itself prove the
classifier is down: repeat it, and use a trivial `echo` probe to distinguish
"outage" from "this command is being blocked".

### Read-only confirmations obtained this session

Reads need no classifier, so the following were confirmed directly from the
filesystem:

| Fact | Evidence | State |
|---|---|---|
| `gemini_provider.py`'s three CLAUDE.md punch-list fixes | `:34` `gemini-3.1-flash-lite` is first in `DEFAULT_MODELS`; `:71-72` and `:124-125` both `stop_after_attempt(2)` + `wait_exponential(multiplier=1, min=1, max=4)`; `:110` and `:151` both `except APIError as err` — narrowed, no `Exception` | ✅ **all three APPLIED** |
| Step 5's SDK API names | `types.py:2593` `class HttpRetryOptions` with `attempts: Optional[int]`; `types.py:2651` `class HttpOptions`; `types.py:2685` `retry_options: Optional[HttpRetryOptions]` | ✅ the plan's names are **exact** — `types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=3))` is valid against the installed 2.23.0 |
| G-3 duplicate guard | `perception/service.py:99-100` and `:103-104` | ✅ confirmed — two module-level guards |
| G-6 busy loop | `perception/service.py:74-75` `if frame is None: continue` — no sleep, no log, and it precedes `cv2.waitKey(1)` at `:90` | ✅ confirmed |
| Q / C key handling (CLAUDE.md punch list) | `:91-94` — `q` breaks the loop; `c` toggles `show_visuals` only, leaving detection and publish at `:80-82` running | ✅ **behaves as spec'd** |
| `run_local.py` exact shape | `:32-34` prints **on every** failed read and `continue`s **before** `cv2.waitKey(1)` at `:44` | ✅ **correction #1 in Session 3 is CONFIRMED** — busy loop + stdout flooding. (It has only **one** `__main__` guard, unlike `service.py`.) |
| G-4 / G-8 | `bus/stream.py:65` docstring claims redelivery; `:71` reads `">"` only; `:81` `model_validate_json` unguarded | ✅ both confirmed |
| G-1 | `decision/policy.py:22`, `:29`, `:36` — constant, param, predicate | ✅ confirmed as recorded |
| L-4 | `mind/llm_provider/base.py:32` signature `500`, `:36` docstring `400` | ✅ confirmed |

The two files Session 3 listed as unread — `perception/run_local.py` and
`bodies/desktop/conversation.py` — are now **half closed**: `run_local.py`
has been read in full (above). `conversation.py` remains unread.

### Correction to Session 3's "under-scoped `.gitignore`" concern

Session 3 wrote that without `__pycache__/` and `*.pyc` rules, the first
commit "would sweep in hundreds of bytecode files". **The premise is
narrower than stated, but the concern is still valid.** `git status
--porcelain` lists a fully-untracked directory as one collapsed entry
(`bodies/`, `contracts/`, …), so `__pycache__` never appeared as its own
line — it is not being ignored, it is merely hidden behind its parent. A
`git add .` would still stage it.

Checked and ruled out as the cause: `~/.gitconfig` has **no**
`core.excludesFile`, and `.git/info/exclude` contains only stock comments.
So nothing is ignoring bytecode today, and `.gitignore` still needs the
`__pycache__/` / `*.pyc` rules **before the first commit**.

Unresolved minor discrepancy: the session-start `gitStatus` snapshot listed
`?? .claude/`, but this session's `git status --porcelain` did **not**. Not
investigated (read-only tools cannot run `git check-ignore`). Worth one
`git check-ignore -v .claude` before the first commit.

### Decisions now operative

- **D1 = INCLUSION** (per the resume point). Step 12 is therefore **Branch B**:
  keep the `not in` predicate, rename `ALWAYS_QUIET_UNLESS_ASKED` →
  `REACTIVE_LABELS`, rewrite the comment and module docstring so the
  documented contract matches the implementation. **No behavior change** —
  this is the constraint-5 default, and it supersedes the audit's
  exclusion recommendation (recorded in Session 3's owner decisions).
- **Step 4 / V3 remains CLOSED** (three model IDs verified valid in Session
  3), so **Step 5 is unblocked** and is the next implementable step.
- **F-1 issuer-side rotation remains UNCONFIRMED.** Treat the token as live.
  Unchanged this session; no action taken.

### Files changed in this session

| File | Change | Verified? |
|---|---|---|
| `memory/episodic/store.py` | **MODIFIED** — steps 2 and 3 | Step 2 ✅ verified; step 3 ⚠️ **NOT VERIFIED** |
| `tests/test_store.py` | **MODIFIED** — added `ConnectTimeoutTests` (5 tests) | ⚠️ never executed |
| `docs/PROJECT_STATE.md` | **MODIFIED** — this record | ✅ read back |

No other source file, configuration file, or dependency has been touched.
No commit was made.

---

## Session 3 — 2026-09-29: remediation started

**Execution is currently BLOCKED — and intermittent.** `Bash` and `PowerShell`
fail because the command-safety classifier is unavailable ("deepseek-v4-flash
is temporarily unavailable"). It is not simply down: one attempt was rejected
by a *policy* guard rather than the outage, which means the command path was
live at that instant. Read-only inspection (file reads, grep, glob) works
throughout.

**No test, probe, or verification command has successfully executed in this
session.** Nothing has been run. Do not record any check as passing unless it
actually ran — in particular, every "expected result" below is a *prediction*,
not a result.

**Command guard note:** a `Remove-Item` on a temp path was rejected with
"Remove-Item on system path 'e:' is blocked". Avoid shell-based temp-file
cleanup in verification commands; write real files instead.

**Re-attempted immediately before this record was written** — `PowerShell`
*and* `Bash`, same command, both refused with the same classifier message. The
verification command has now been attempted three times and has **never
executed**. Do not read the queued command below as having produced output.

### Read-only confirmations obtained instead

Reads do not need the classifier, so two Phase 0 facts were re-confirmed
directly from the filesystem:

| Fact | Evidence | State |
|---|---|---|
| `start-claude.sh` is gone | a direct read of the path returns "File does not exist" | ✅ **VERIFIED (read-only)** |
| The repo exists and is empty | `.git/HEAD` reads `ref: refs/heads/main`; `.git/refs/heads/main` does not exist; `.git/config` has no `[remote]` or `[branch]` section | ✅ **VERIFIED (read-only) — 0 commits** |

This also corrects a stale signal: the session's environment banner still
reports `Is a git repository: false`. It predates the `git init` and is wrong.

Still **unverified**: the `.gitignore` rules, the test suite, the interpreter
version, and everything about Step 1's runtime behaviour.

### Implemented but NOT verified

| Step | State |
|---|---|
| **Step 1** (H-2) | **IMPLEMENTED, NOT VERIFIED.** `memory/episodic/store.py`: `_FALLBACK_PASSWORD` deleted; `_build_dsn()` now raises `RuntimeError` with `setx`/`export` instructions when `AURA_PG_PASSWORD` is unset. Confirmed by reading only; **never executed**. Its tests exist but have never been run. |
| **Step 18** (T-2) | **HARNESS CREATED, NOT VERIFIED.** `tests/__init__.py` exists (stdlib `unittest`, no new dependency). `python -m unittest discover -s tests` has never been run. |
| **Step 2** (DSN escaping) | **NOT IMPLEMENTED.** Its test is written and **intentionally expected to fail** as pre-fix evidence. Do not edit the test to make it pass. |
| **Steps 3 and later** | **NOT STARTED.** No other source file modified. |

### `tests/test_store.py` — CREATED, never executed

| Test | Covers |
|---|---|
| `test_raises_when_password_absent` | Step 1 — `RuntimeError`, and that its message names `AURA_PG_PASSWORD`, `setx`, `export` |
| `test_password_appears_in_dsn_when_present` | Step 1 — the value reaches the DSN when set |
| `test_hardcoded_fallback_constant_is_gone` | Step 1 — `_FALLBACK_PASSWORD` no longer exists on the module |
| `test_password_with_uri_delimiters_keeps_host_intact` | Steps 2 — **pre-fix, expected to fail** (subtests over `@ : / # ?`) |

The module sets `AURA_PG_PASSWORD` via `os.environ.setdefault` **before**
importing `store`, because `store.py:79` computes `DEFAULT_DSN` at import time
and would otherwise fail to import on a machine without the variable.

### Pending verification command — the next action

Nothing below has run yet. This exact command is queued:

```powershell
$py = ".venv\Scripts\python.exe"
git check-ignore --no-index -v start-claude.sh .env secrets.env
& $py -c "import sys; print(sys.version)"
& $py -m unittest discover -s tests -v
```

1. `git check-ignore --no-index` on `start-claude.sh`, `.env`, `secrets.env`
   — closes Phase 0's outstanding assertion. `--no-index` creates no files,
   which sidesteps the `Remove-Item` guard noted above.
2. Python interpreter version — confirm the venv is the 3.12 the project pins.
3. `python -m unittest discover -s tests -v` — closes step 18 and verifies
   step 1.

**Expected result — a PREDICTION, not verified:** the three step 1 tests pass,
and `test_password_with_uri_delimiters_keeps_host_intact` **fails**. If step 2's
test instead *passes*, the audit's premise for step 2 does not reproduce and
must be investigated before implementing that fix.

### Phase 0 — executed

| Action | State |
|---|---|
| Delete `start-claude.sh` | **DONE.** File removed; verified absent from `git status` untracked output. |
| Add `.gitignore` | **DONE, UNVERIFIED.** Rules for `start-*.sh` and `.env` / `.env.*` / `*.env`. The `git check-ignore` assertion never ran. |
| `git init` | **DONE.** Empty repository, 0 commits, 0 staged entries. |

**F-1 token rotation remains UNCONFIRMED.** The owner stated it would be
verified separately and instructed that it must never be assumed. Treat the
token as live until the owner explicitly confirms otherwise.

### Verification gates closed this session

Four of the five items previously listed as *unverified* are now resolved, by
read-only inspection or by probes that ran before execution was blocked:

| Gate | Result |
|---|---|
| `psycopg/conninfo.py:104` malformed-DSN path | **CLOSED — no leak.** Four malformed DSNs probed (invalid port, missing host, bad option value, unknown option). The marker password appeared in neither `str()` nor `repr()` in any case, including the `ProgrammingError` path. H-3's residual uncertainty is eliminated; **D4 stays withdrawn**. |
| **V3** — Gemini model IDs | **CLOSED — all three valid.** A live `models.list()` returned 61 models; `gemini-3.1-flash-lite`, `gemini-3.6-flash` and `gemini-3.5-flash` all resolve exactly. **Step 4 is therefore done** — it was never owner-blocked; the key was already in the environment. |
| Memurai version / `MINID` | **CLOSED — available.** Memurai **8.1.242**, reporting `redis_version:8.2.7`. `COMMAND DOCS XADD` explicitly documents the `MINID` trimming strategy. **D5 Option B is technically available**; the audit's "requires 6.2+, installed version never checked" caveat is obsolete. |
| `perception/run_local.py`, `bodies/desktop/conversation.py` | **CLOSED — both read in full.** |
| Step 15 production/drain measurement | **STILL OPEN.** Deferred by the owner until after Phase 1. |

### Corrections to the audit record

1. **Step 9's description of `run_local.py` is wrong.** The plan says it "keeps
   its first-failure print." It does not: `perception/run_local.py:33` prints on
   **every** failed read, and the `continue` at `:34` precedes `cv2.waitKey(1)`
   at `:44`, so there is no delay at all. It is a busy loop that floods stdout
   at CPU speed — G-6 *plus* log flooding.
2. **PostgreSQL IS running** on `localhost:5432` (a `postgres` process), even
   though the `postgresql-x64-18` *service* reads Stopped. Step 15's Postgres
   half is feasible; the audit's "nothing running" picture was incomplete.
3. **F-2 now has runtime proof.** `XINFO STREAM percepts`: `entries-added` =
   `length` = 12,181 and `max-deleted-entry-id` = `0-0` — nothing has ever been
   trimmed or deleted, and `XACK` clears the PEL, not the stream. The stream
   spans 2026-09-14 → 2026-09-24, but that is intermittent dev use (its first
   entry is a synthetic `test-object`), **not** a production rate, and it does
   **not** satisfy step 15.
4. **`run_aura.py:59` names the wrong service.** It instructs the operator to
   run `net start postgresql-x64-17`; the installed service is
   `postgresql-x64-18`. Owner approved recording this as a later-phase
   documentation/config fix. Not yet fixed.
5. **Removing the H-2 fallback makes the README's setup incomplete** — see
   pending cleanups below.

### Owner decisions recorded

| Decision | Answer |
|---|---|
| **D1** NoveltyFilter | **INCLUSION.** Keep current behavior; rename `ALWAYS_QUIET_UNLESS_ASKED` → `REACTIVE_LABELS` and rewrite the comment/docstring so the documented contract matches the implementation. **The audit's exclusion recommendation is superseded** — the per-label 30 s cooldown means exclusion would narrate every non-person object in frame once per 30 s, producing exactly the constant narration the module docstring exists to prevent. |
| **D2** Redis delivery | **DOCSTRING-ONLY.** Do not implement `XAUTOCLAIM` or durable at-least-once at this stage. |
| **D3** Schema init | **OPTION C** — explicit initialization in preflight *and* in each standalone entry point. |
| **D5** Redis retention | **DEFERRED** — do not choose a value until step 15 measures production and drain rates. |
| **D5** Postgres retention | **NO ACTION.** |
| `seen_recently` | **DELETE.** Verified dead: no callers in `mind/`, `memory/`, `bodies/`, `decision/`, `perception/`, `bus/`, `contracts/`, or `run_aura.py`. |
| Postgres row count | **DO NOT QUERY YET.** |
| Supervised camera run | **DEFERRED** until after Phase 1 is implemented and verified. |

### Files changed in this session

| File | Change | Verified? |
|---|---|---|
| `start-claude.sh` | **DELETED** — F-1 credential file, removed before `git init` | ✅ read-only: direct read returns "File does not exist" |
| `.gitignore` | **CREATED** — `start-*.sh`, `.env`, `.env.*`, `*.env` | ⚠️ `git check-ignore` never ran |
| `.git/` | **CREATED** — `git init` | ✅ read-only: `.git/HEAD` = `ref: refs/heads/main`; no `refs/heads/main`, no remote/branch config ⇒ **0 commits** |
| `tests/__init__.py` | **CREATED** — harness only | ⚠️ discovery never ran |
| `tests/test_store.py` | **CREATED** — step 1 tests + step 2 pre-fix test | ⚠️ never executed |
| `memory/episodic/store.py` | **MODIFIED** — step 1 (H-2) | ⚠️ **NOT VERIFIED** |
| `docs/PROJECT_STATE.md` | **MODIFIED** — this record | ✅ read back |

No other source file, configuration file, or dependency has been touched.

### Owner operating rules (2026-09-29) — still in force

1. No further broad audits; the audit is sufficient.
2. Do not ask about minor architectural choices.
3. Use recommended defaults for non-critical decisions.
4. Stop and ask **only** when a decision materially changes AURA's intended
   behavior or data semantics.
5. D1/D2/D3/D5 stay recorded but must not block unrelated fixes.
6. Step 1 stays IMPLEMENTED / NOT VERIFIED until it is actually tested.
7. Do not stack another source change on step 1 until it can be tested.
8. When execution returns, verify step 1 immediately and continue the sequence.
9. Use the existing implementation plan; do not redesign the architecture.
10. Minimal changes only; test after each logical batch.

**Escalation criteria — stop only for:** changes to AURA's intended behavior;
changes to event delivery semantics; destructive data operations;
credential/secret operations; anything that could materially break the existing
architecture. Small documentation and cleanup changes do **not** require
approval.

**Owner-stated priority order:** verify step 1 → step 2 (DSN encoding) →
step 3 (Postgres connect timeout) → G-5 transport/DNS handling → other
already-approved correctness/reliability fixes → tests → documentation cleanup
→ Redis retention measurement → remaining architectural decisions.

**Parked, not abandoned:** an NVIDIA VLM integration was discussed. A
background analysis concluded a VLM belongs in `perception/` as a sibling
`Percept` producer using the already-reserved `PerceptType.SCENE_CHANGE` and
`raw_ref` hooks — **not** in the `LLMProvider` slot, whose signature is
text-in/text-out. No work is authorised on this; it is recorded so the
reasoning is not lost. Note the analysis ran while the safety classifier was
down, so treat it as unverified.

### Pending documentation / security cleanups

- **`README.md` needs explicit `AURA_PG_PASSWORD` setup instructions.** Step 4
  of the README starts Postgres with a password but never tells the user to
  export `AURA_PG_PASSWORD`. The H-2 fallback silently covered that gap; with it
  removed, a user following the README exactly now hits the new `RuntimeError`.
- **The old hardcoded password literal must be replaced with a safe placeholder**
  in `README.md:33` and `memory/episodic/writer.py:8` — both carry it inside a
  `docker run … POSTGRES_PASSWORD=…` example. **Must be done before the first
  commit.**
- **The new `.gitignore` entries still need verification** (`git check-ignore`)
  before the first commit.
- **`.gitignore` is under-scoped as written.** It covers launcher scripts and
  env files only. Without `__pycache__/`, `*.pyc` and `.claude/`, the first
  commit would sweep in hundreds of bytecode files and Claude Code session data.
  `.venv` needs no rule — it self-ignores. Owner decision outstanding.
- **`yolov8n.pt` (6.5 MB)** is untracked and would be committed. Per I-4 it is
  intentionally in the repo; left as-is pending owner confirmation.

---

## Current Status

AURA is a working multi-process Python 3.12 companion application at
`C:\Users\USER\Desktop\aura`. Four services run under a multiprocessing
supervisor in `run_aura.py`: perception, memory-writer, mind, body.

**Remediation has begun** — see
[Session 3](#session-3--2026-09-29-remediation-started) for the authoritative
current state, which supersedes the paragraphs immediately below. Two full
audit passes are complete:

- **Pass 1** — full audit, findings numbered by area (F, G, H, I, J, K, L, T).
- **Pass 2** — verification of every finding against the actual installed
  dependency source and executable probes; relevance analysis of four
  "absence" claims (no tests, no version control, no migrations, no restart
  policy).

Beyond the audit, the following planning artifacts were produced but **not
implemented**:

- a remediation plan, re-prioritized into five categories;
- a "current behavior" field plus eight further fields per fix;
- a set of architecture decision gates (D1–D5) with options and consequences;
- a decision brief, including a direct-experiment verification (V2) that led
  to **D4 being withdrawn**;
- the final phased implementation sequence reproduced in
  [Implementation Plan](#implementation-plan) below.

At the time of the audit, **no source file, configuration file, or dependency
had been modified, created, or deleted**, and this document was the only file
written. That is no longer true — see
[Session 3](#session-3--2026-09-29-remediation-started).

The directory **is now** a git repository (initialised 2026-09-29: 0 commits,
0 staged entries). There **is now** a `tests/` directory, containing the
harness only — no tests have been run. There is still no migration tooling.

### Blocking security condition

`start-claude.sh` existed in the repository root and contained a live
plaintext bearer token. Full detail under [F-1](#f-1--live-credential-in-the-repository-root).
**The file was deleted on 2026-09-29, before `git init`, so the value never
entered history.** Issuer-side **rotation remains UNCONFIRMED** — the owner
will verify it separately and has instructed that it must not be assumed. The
token must be treated as live until the owner explicitly confirms otherwise.
See [Session 3](#session-3--2026-09-29-remediation-started).

---

## Audit Status

Two passes complete. Pass 2 verified findings against installed source rather
than inference. Findings below are the **surviving** set.

Verified evidence sources used in pass 2:

| Subject | Evidence |
|---|---|
| google-genai 2.23.0 | `_api_client.py:722`, `:848-852`, `:111-120`, `:562-564`, `:582-585`, `:905/910/1537`; `types.py:2685` |
| psycopg3 | direct probe against an unreachable port; `conninfo.py:23`, `conninfo.py:104` |
| redis-py 8.1.0 | default `Retry` behaviour, exponential jitter |
| AURA tree | grep for exception handlers; grep for `maxlen`/`XTRIM`/`XACK`/`XAUTOCLAIM` |

**ID integrity note.** IDs are carried over from the audit. Two findings
discovered *during* pass-2 verification — the `_build_dsn()` escaping defect
and the empty `seen_recently()` method — **never received an ID in the
audit**. They are marked **ID: unassigned** rather than given invented ones.
Documentation/contract mismatches are grouped as a set with line references,
which is how they were tracked.

**Severity note.** H-3 is the only grade with a recorded revision: downgraded
from Medium to Low after the V2 probe disproved its premise. All other grades
below were **re-derived from the verified behavior during this handoff**, not
copied from a verbatim severity record — treat them as working grades, and
re-grade any finding whose impact you weigh differently. F-1 (Critical) is the
one grade fixed by the audit's own framing of it as the critical security
finding.

### Surviving findings

---

#### F-1 — Live credential in the repository root

| Field | Value |
|---|---|
| **ID** | F-1 |
| **Severity** | **Critical** |
| **File** | `start-claude.sh` (repository root, 4 lines) |

**Problem.** A Claude Code launcher script sits in the repository root. It
sets `ANTHROPIC_BASE_URL` to a third-party host, and exports one live
bearer token **twice** — as both `ANTHROPIC_AUTH_TOKEN` and
`ANTHROPIC_API_KEY`. It also pins `ANTHROPIC_MODEL`.

**Current verified behavior.** The file exists and is readable. The token is
a live-looking `sk-…` string stored in plaintext, duplicated across two
variables. **The token value is deliberately not reproduced in this document
or anywhere in the audit.** The file is not referenced by any Python module
in the tree — it is not AURA code.

**Intended behavior.** No credentials in the working tree.

**Recommended action.** (1) Revoke/rotate the token at the issuer — this is
the only step that actually neutralizes exposure, since the value must be
assumed already compromised. (2) Delete the file. (3) Add an ignore rule for
launcher scripts and `.env` files. (4) Do all of this **before** any
`git init`, or the token enters permanent history and cannot be removed
without a history rewrite.

*(Owner action: rotation is not something this session can perform.)*

---

#### F-2 — Redis streams are unbounded

| Field | Value |
|---|---|
| **ID** | F-2 |
| **Severity** | High |
| **File** | `bus/stream.py:40-43` (`EventBus.publish`) |

**Problem.** `publish()` calls `XADD` with no `maxlen` argument.

**Current verified behavior.** `XADD` is issued without any trimming
parameter. Grep confirms **no** `maxlen`, `MAXLEN`, `XTRIM`, or `MINID`
anywhere in the tree. Two streams are affected: `PERCEPTS_STREAM` and
`actions:{body_id}` (the latter produced at `mind/reasoning_loop.py:66`).
Redis therefore grows without bound for as long as the process runs. Note the
producer runs at camera framerate while the consumer's drain rate is
unmeasured.

**Intended behavior.** Bounded retention, sized from measured production and
drain rates rather than a guessed constant.

**Recommended action.** Do not choose a number yet — see
[D5 Redis retention](#d5--retention). Measure first (step 15), then bound with
approximate trimming (step 16).

---

#### G-1 — NoveltyFilter semantics contradict their own documentation

| Field | Value |
|---|---|
| **ID** | G-1 |
| **Severity** | High (behavioral) / Medium (fix cost) |
| **File** | `decision/policy.py:19-22`, `:26-33`, `:35-43` |

**Problem.** Three statements of intent describe an **exclusion** list, while
the parameter name and the predicate implement **inclusion**. The module
docstring, the constant name `ALWAYS_QUIET_UNLESS_ASKED`, and the inline
comment all say "stay quiet about these"; the parameter `reactive_labels` and
the `not in` predicate say "only react to these".

**Current verified behavior.** The predicate at line 36 is
`if label not in self._reactive_labels: return False`. With the default
frozenset containing exactly one entry, **only that one label is ever
narrated** and every other detected object is silently dropped. The code
therefore makes AURA narrate the single label the comment says to keep quiet,
and stay silent about everything else.

**Intended behavior.** Per the documented intent, AURA should react to
notable objects and stay quiet about the label in the exclusion set.

**Recommended action.** Gated on **D1**. Whatever is chosen, the name, the
comment, and the predicate must end up agreeing. Fix is one line plus
renames either way.

---

#### G-2 — Worker death is warned about once and never acted on

| Field | Value |
|---|---|
| **ID** | G-2 |
| **Severity** | High |
| **File** | `run_aura.py:118-129` |

**Problem.** The supervisor monitors child processes but only prints a
warning; the `already_warned` set means the warning appears once and is then
suppressed forever.

**Current verified behavior.** `mp.Process(..., daemon=True)` is started for
each of the four services. The supervisor loop calls `is_alive()` every 2 s.
On death it prints a warning and adds the name to `already_warned` — after
which that worker's death produces **no further output**. There is no restart.
The message itself ("Check its output above for the actual error") is
technically accurate but easy to miss, and the process is `daemon=True`, so
`terminate()` on shutdown is the only lifecycle management.

**Intended behavior.** The operator should be able to tell, from the
supervisor's own output, that AURA is running degraded.

**Recommended action.** Reword to state plainly that the service is down and
will not be restarted (step 6). This is deliberately paired with G-5: the
`already_warned` suppression is the mechanism that made G-5 invisible.

---

#### G-3 — Duplicate `__main__` guard

| Field | Value |
|---|---|
| **ID** | G-3 |
| **Severity** | Low |
| **File** | `perception/service.py:99-100` and `:103-104` |

**Problem.** The `if __name__ == "__main__": main()` guard appears twice at
module level, separated by blank lines.

**Current verified behavior.** Only the first executes; the second is dead
code. No behavioral impact.

**Intended behavior.** One guard.

**Recommended action.** Delete the second (step 8).

---

#### G-4 — `consume()` docstring asserts a delivery guarantee the code does not provide

| Field | Value |
|---|---|
| **ID** | G-4 |
| **Severity** | Medium (documentation correctness) |
| **File** | `bus/stream.py:62-66` |

**Problem.** The docstring states: *"Caller must call `ack` once an entry is
fully processed — unacked entries are redelivered on restart."* The
implementation reads only `">"` (never-delivered entries), so unacked entries
are **never** redelivered.

**Current verified behavior.** `xreadgroup(group, consumer, {stream: ">"},
...)` — `">"` returns only messages never delivered to any consumer. Entries
already delivered and left unacked stay in the PEL and are **not** re-read.
There is no `XAUTOCLAIM`, `XCLAIM`, or `XPENDING` anywhere in the tree, so the
PEL is never drained. Effective semantics are **at-most-once**, not
at-least-once.

**Important contrast:** the **module** docstring at `bus/stream.py:11-13`
claims consumer groups mean queued percepts survive a Memory outage — this was
verified **TRUE** and is not a finding.

**Intended behavior.** Either the code should provide the documented
redelivery, or the docstring should describe the actual guarantee.

**Recommended action.** Gated on **D2**. Under the standing constraint that
existing behavior is preserved unless a verified bug requires change, the
default is a **documentation-only** fix (step 21) — at-most-once is a design
choice, not a defect.

---

#### G-5 — Transport/DNS failure kills the mind loop; three retry layers, none cover it

| Field | Value |
|---|---|
| **ID** | G-5 |
| **Severity** | **High** |
| **Files** | `mind/llm_provider/gemini_provider.py:53`, `:110`, `:151`; `mind/reasoning_loop.py:58`; `run_aura.py:118-129` |

**Problem.** A DNS or transport failure — no internet, VPN down, DNS blip —
raises `httpx.ConnectError`, which matches **none** of the three retry layers.

**Current verified behavior — full traced path:**

1. `httpx.ConnectError` is raised (`ConnectError → NetworkError →
   TransportError → RequestError → HTTPError → Exception`).
2. **SDK layer does not retry.** `BaseApiClient` sets `_http_options` to a
   bare `HttpOptions()` (`_api_client.py:722`) because the project never
   passes `http_options`. `retry_options` therefore defaults to `None`
   (`types.py:2685`), and `retry_args(None)` returns
   `stop=stop_after_attempt(1)` (`_api_client.py:562-564`; docstring verbatim:
   *"If None, the 'never retry' stop strategy will be used."*). The SDK's
   transient list `_HTTPX_TRANSIENT_EXC = (httpx.TimeoutException,
   httpx.ConnectError)` (`_api_client.py:111-120`) and its retry predicate
   (`:582-585`) are **never reached**.
3. **Project tenacity layer does not match.** The `@retry` decorators at
   `:69-74` and `:122-127` use `retry_if_exception_type(ServerError)`.
   `ConnectError` is not a `ServerError`.
4. **Model-fallback layer does not match.** `except APIError as err`
   (`:110`, `:151`) does not catch `httpx.ConnectError`, which is not part of
   the google-genai exception hierarchy.
5. The exception propagates to `mind/reasoning_loop.py:58` — the
   `provider.generate(...)` call — which is **unguarded**. The mind process
   dies.
6. The supervisor prints one warning and never restarts (see G-2).

Net effect: a transient network failure permanently disables AURA's reasoning
until manual restart, with the failure easy to miss.

**Intended behavior.** Transient transport failures are absorbed; a
persistent outage degrades loudly rather than killing the loop.

**Recommended action.** Two steps, in this order: enable SDK retry via
`http_options` (step 5), then widen the project's `except` clauses to include
the httpx transport exceptions (step 7). Deliberately **not** broadening the
tenacity predicate as well — that would be a third mechanism for the same
failure.

**Dependency:** V3 must be resolved first (step 4). Configuring retries
against dead model IDs converts a fast crash into a slow retry storm.

---

#### G-6 — Camera read failure spins without sleeping

| Field | Value |
|---|---|
| **ID** | G-6 |
| **Severity** | Medium |
| **Files** | `perception/service.py:72-75`; `perception/run_local.py` |

**Problem.** When `read_frame()` returns `None`, the loop `continue`s
immediately with no sleep and no bound on consecutive failures.

**Current verified behavior.** `while True: frame = camera.read_frame(); if
frame is None: continue` — a tight busy loop that consumes a full CPU core
indefinitely if the camera is unplugged or held by another application. In
`service.py` there is not even a log line, so the failure is silent.

**Intended behavior.** Back off, and fail loudly enough that the supervisor
can report the service as stopped.

**Recommended action.** Sleep between failures and raise after a bounded
number of consecutive failures (step 9). Counter resets on any successful
read, so a driver that emits occasional `None` frames during mode changes
cannot trip it.

---

#### G-7 — Schema DDL runs on every `EpisodicMemory` construction

| Field | Value |
|---|---|
| **ID** | G-7 |
| **Severity** | Medium |
| **File** | `memory/episodic/store.py:75-82` |

**Problem.** `__init__` calls `_ensure_schema()`, which opens a connection and
executes the full `SCHEMA` DDL including two `CREATE INDEX IF NOT EXISTS`
statements.

**Current verified behavior.** Every construction costs a connect plus the
whole schema statement. Construction happens **per question** on the
interactive path (`mind/query.py:64`), so this runs on every user question.

**Intended behavior.** Schema initialization once per process, at startup.

**Recommended action.** Split into an explicit `initialize()` and call it
from each entry point (step 13). Gated on **D3**.

---

#### G-8 — One malformed message kills a long-running consumer

| Field | Value |
|---|---|
| **ID** | G-8 |
| **Severity** | Medium |
| **File** | `bus/stream.py:81` |

**Problem.** `model_cls.model_validate_json(fields["data"])` is unguarded
inside the generator.

**Current verified behavior.** A `ValidationError` (schema drift between
producer and consumer) or `KeyError` (missing `data` field) propagates out of
the generator, out of the `for` loop in the service, and kills the process.
Combined with G-2 (no restart), one bad message permanently stops memory
writes or reasoning.

**Intended behavior.** A single bad message is logged and skipped; the
consumer survives.

**Recommended action.** Guard the parse, log, and deliberately `XACK` the bad
entry so a systematic mismatch surfaces in logs rather than accumulating in
the PEL (step 10). Must precede step 17 — draining the PEL re-delivers
entries, and without this guard one malformed backlog entry would kill the
consumer on every restart.

---

#### G-9 — `EpisodicMemory` constructed per question

| Field | Value |
|---|---|
| **ID** | G-9 |
| **Severity** | Medium |
| **Files** | `mind/query_local.py:41`; `bodies/desktop/conversation.py` |

**Problem.** `answer_question()` constructs its own `EpisodicMemory` when
none is passed (`mind/query.py:64`), and the interactive callers never pass
one.

**Current verified behavior.** Each question builds a new `EpisodicMemory`,
which (per G-7) runs full DDL. No state is carried between questions.

**Intended behavior.** One long-lived instance per process, injected via the
parameter that already exists.

**Recommended action.** Hoist construction to process start and pass
`memory=` at the two call sites (step 14). **`mind/query.py:64` itself is left
unchanged** — the injection seam already exists there.

---

#### H-2 — Hardcoded fallback database password

| Field | Value |
|---|---|
| **ID** | H-2 |
| **Severity** | Medium |
| **Files** | `memory/episodic/store.py:44`, `:64`; also documented in `README.md:33` and `memory/episodic/writer.py:7` |

**Problem.** `_FALLBACK_PASSWORD` is a hardcoded literal used whenever
`AURA_PG_PASSWORD` is unset. It is the same value the README tells users to
pass to `docker run`.

**Current verified behavior.** With the environment variable unset, `_build_dsn()`
silently substitutes the literal and attempts to connect. There is no warning
that a default is in use. *(The literal is not reproduced here.)*

**Intended behavior.** Fail fast and loudly with setup instructions.

**Recommended action.** Remove the constant; raise `RuntimeError` with
PowerShell and bash export examples when the variable is missing (step 1) —
matching the style already used at `gemini_provider.py:48-52`.

---

#### H-3 — Raw driver exception text in user-facing output — **DOWNGRADED**

| Field | Value |
|---|---|
| **ID** | H-3 |
| **Severity** | **Low** (originally graded Medium; revised after V2) |
| **File** | `run_aura.py:56-58` |

**Problem (as originally filed).** A psycopg exception is interpolated
directly into the preflight problem message, and that message is printed for
the operator to follow — raising the possibility that the DSN, including the
password, appears in user-visible output.

**Current verified behavior.** The V2 probe tested this directly. Connecting
to `postgresql://…:ZZMARKER999ZZ@127.0.0.1:1/postgres` raises
`psycopg.errors.ConnectionTimeout` (MRO: `ConnectionTimeout → OperationalError
→ DatabaseError → Error`); `str()` is `"connection timeout expired"` and
`repr()` is `ConnectionTimeout('connection timeout expired')`. **The marker
appears in neither.** The credential-leak premise therefore fails on the path
AURA actually hits.

**Intended behavior.** Preflight output describes the problem without
interpolating raw driver text.

**Recommended action.** Minimal: drop the `{exc}` interpolation (step 11).
**D4 is withdrawn** — the exception type does not need to change. See
[D4](#d4--exception-type--withdrawn).

**Residual uncertainty.** `psycopg/conninfo.py:104` contains
`except e.OperationalError as ex: raise e.ProgrammingError(str(ex)) from None`
on the malformed-conninfo path. **This path was not executed** — the probe
covered the connection path only. If the DSN is malformed, driver text could
still reach the message. This is labelled uncertain, not verified.

---

#### H-4 — No connection timeout on any `EpisodicMemory` connect

| Field | Value |
|---|---|
| **ID** | H-4 |
| **Severity** | High |
| **File** | `memory/episodic/store.py:80`, `:85`, `:107`, `:123` |

**Problem.** All four `psycopg.connect` calls omit `connect_timeout`, so
psycopg's default of **130 seconds** applies (`conninfo.py:23`, verified).

**Current verified behavior.** The four bare call sites are confirmed by grep:

```
memory/episodic/store.py:80:   with psycopg.connect(self._dsn) as conn:
memory/episodic/store.py:85:   with psycopg.connect(self._dsn) as conn:
memory/episodic/store.py:107:  with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
memory/episodic/store.py:123:  with psycopg.connect(self._dsn) as conn:
```

`run_aura.py:54` passes `connect_timeout=2` and `run_aura.py:40` passes
`socket_connect_timeout=2` — these are the **only** timeouts in the codebase,
and they cover preflight only. An unreachable-but-not-refusing host (firewall
drop, sleeping machine, wrong subnet) stalls each connect for up to 130 s.
Because `writer` connects **per row**, this stalls the drain path for 130 s
per percept.

**Intended behavior.** Bounded connect attempts everywhere.

**Recommended action.** Add a module constant and pass it at all four sites
(step 3).

**Dependency:** this is a **hard predecessor to D2/G-4** (step 17). Any
pending-entry claim threshold must outwait the worst-case stall, and that
number is unknowable while the stall is 130 s.

---

#### `_build_dsn()` — password is not URL-encoded

| Field | Value |
|---|---|
| **ID** | **unassigned** (discovered during pass-2 verification; never given an ID in the audit) |
| **Severity** | Medium |
| **File** | `memory/episodic/store.py:65` |

**Problem.** The DSN is built by f-string interpolation of the raw password:
`f"postgresql://{user}:{password}@{host}:{port}/{db}"`. No percent-encoding.

**Current verified behavior.** A password containing any of `@ : / # ?`
misparses the URI. An `@` in the password causes everything after it to be
read as the host component; a `/` terminates the authority section. The
failure is a confusing connection error naming the wrong host, not an obvious
configuration error.

**Intended behavior.** Password is encoded; the parsed host always equals the
configured host.

**Recommended action.** Percent-encode with `quote(password, safe="")`
(step 2).

---

#### L-4 — `LLMProvider.generate` docstring states the wrong default

| Field | Value |
|---|---|
| **ID** | L-4 |
| **Severity** | Low |
| **File** | `mind/llm_provider/base.py:36` |

**Problem.** The docstring says *"max_tokens defaults to 400 (roughly 300
words)"*. The signature at line 32 is `max_tokens: int = 500,`. The
implementation in `gemini_provider.py:101` is also `500`.

**Current verified behavior.** Docstring and code disagree. Three artifacts
say 500; one says 400.

**Intended behavior.** Documentation matches the implementation.

**Recommended action.** Change the docstring to 500 (step 23). The code is the
3-to-1 majority and changing it would alter runtime behavior for a
documentation defect.

---

#### `seen_recently()` — unreachable method with a false docstring

| Field | Value |
|---|---|
| **ID** | **unassigned** (discovered during pass-2 verification) |
| **Severity** | Low |
| **File** | `memory/episodic/store.py:120-132` |

**Problem.** The docstring says *"Used by the Decision layer to avoid
re-announcing the same object."* Nothing calls it.

**Current verified behavior.** No callers anywhere in the tree. The Decision
layer uses `decision/policy.py`'s in-memory `NoveltyFilter` instead. The
duplicate-suppression logic exists in two places, only one of which runs.

**Intended behavior.** Delete, or wire up and remove the duplicate.

**Recommended action.** Delete (step 22) — pending owner confirmation, since
removing dead code is technically outside a strict correctness fix. See
[Remaining Decisions](#remaining-decisions).

---

#### Documentation / contract mismatches (verified set)

All confirmed by reading the exact lines. Each is a case of documentation
describing behavior the code does not have.

| # | File / lines | Claim | Verified reality |
|---|---|---|---|
| D-a | `bus/stream.py:62-66` | Unacked entries are redelivered on restart | Not redelivered; `">"` only. **= G-4** |
| D-b | `mind/llm_provider/base.py:36` | `max_tokens` defaults to 400 | Defaults to 500. **= L-4** |
| D-c | `README.md:23-24` | "Every arrow above is a Redis Stream." | The Body's startup greeting is a direct Python call into `mind/greeting.py` from `bodies/desktop/run.py`, not a bus message. |
| D-d | `README.md:56-61` | "Standalone debug tools" lists three runners | Omits `mind/query_local.py` and `bodies/desktop/conversation.py`, both of which exist and run. |
| D-e | `README.md:73-75` | "No conversation yet — AURA reacts to what it sees; it doesn't yet hold a back-and-forth dialogue with you." | False. `bodies/desktop/conversation.py` implements push-to-talk voice Q&A and already calls `answer_question()`. |
| D-f | `mind/query_local.py:5-7` | "Same `answer_question()` function voice input will call once it exists" | Voice input exists now; `conversation.py` already calls it. Stale future tense. |
| D-g | `README.md:84-457` | — | Lines 84-90 are blank padding; a pasted runtime log begins at line 91 and continues to EOF. Line 83 is the last real content. |
| D-h | `memory/episodic/store.py:120-132` | `seen_recently` "Used by the Decision layer" | No callers. See above. |
| D-i | `memory/episodic/store.py` `write()` | *(silent)* | `ON CONFLICT (id) DO NOTHING` makes writes retry-idempotent. Undocumented. **= I-3** |

**Not mismatches** (verified TRUE, listed so they are not re-flagged): the
module docstring at `bus/stream.py:11-13`; the `speaker.py` comment about
fresh `pyttsx3.init()` per call (see J-1).

---

#### Other surviving findings

| ID | Severity | File | Problem | Recommended action |
|---|---|---|---|---|
| **I-3** | Low | `memory/episodic/store.py` (`write`) | `ON CONFLICT (id) DO NOTHING` makes writes retry-idempotent, but this is undocumented — a reader cannot tell whether it is deliberate dedup or a safety net | Document as retry-idempotency, not deduplication (step 29) |
| **I-4** | Low | `perception/detector.py:…` (`_MODEL_BY_TIER`) | `select_model_weights()` returns `yolov8s.pt` on CUDA hosts, but only `yolov8n.pt` is in the repo — the GPU path downloads at runtime, undocumented | Document the runtime download (step 30) |
| **J-4** | Low | `mind/query.py:…` (`build_qa_prompt`) | Builds the prompt by string-formatting `persona.name` / `persona.traits` directly, bypassing `Persona.system_prompt()` | **Deferred** — adopting `system_prompt()` would inject the "introducing yourself for the first time" framing into Q&A answers, a behavior change with no verified bug behind it |
| **K-2** | Low | `pyproject.toml` (`[tool.hatch.build.targets.wheel]`) | `packages = ["contracts"]` omits every application package, so a built wheel would not contain the app | Fix or remove the wheel target (step 31) |
| **T-1** | — | *(new)* `tests/test_policy.py` | NoveltyFilter has no test; nobody noticed G-1 | Write from stated intent, not from current code (step 19) |
| **T-2** | — | *(new)* `tests/` | No test harness exists at all | stdlib `unittest`; no new dependency (step 18) |

**Absence claims analysed in pass 2** — no tests, no version control, no
migrations, no restart policy. All four verified true. Relevance: the first
two are addressed by T-2 and the Phase 0 git decision; "no migrations" is
acceptable at this schema size but becomes a finding the moment a column
changes; "no restart policy" is the substance of G-2.

---

### Withdrawn and downgraded findings

| ID | Status | Reason |
|---|---|---|
| **D4** | **Withdrawn** | The V2 probe disproved the premise. See [D4](#d4--exception-type--withdrawn). |
| **J-1** | **Withdrawn** | `speaker.py` creates a fresh `pyttsx3.init()` per `speak()` call. Initially flagged as wasteful; the inline comment documents it as **intentional** for SAPI5 reliability on Windows. The comment is accurate. Reusing the engine is the thing that breaks. |
| **J-5** | **No action** | `sys.path.insert(0, …)` at the top of entry points. Flagged as a code smell; it is the deliberate run-from-repo-root mechanism. No behavior change recommended; worth a one-line comment at most. |
| **H-3** | **Downgraded** Medium → **Low** | Verified by direct probe, not inference. See H-3 above. |
| **V2** | **Resolved, negative** | The verification question "can the DSN appear in the exception string/repr?" answered **no** on the reachable path. |

---

## Architecture Decisions / Gates

Five gates were raised. **D4 is withdrawn.** D1, D2, D3, and D5 are **still
open** — the owner has not answered them. Nothing is blocked on a decision
that could be resolved from the code alone; these are genuinely product and
architecture choices.

---

### D1 — NoveltyFilter

**Current behavior.** `decision/policy.py` defines
`ALWAYS_QUIET_UNLESS_ASKED = frozenset({"person"})` and passes it as the
parameter `reactive_labels`. The predicate is
`if label not in self._reactive_labels: return False`. Net effect: **only the
one label in the set is ever narrated**; every other label returns `False`.
A cooldown of 30 s suppresses repeats.

**Exclusion vs inclusion interpretation.**

| Reading | Meaning | Consequence if adopted |
|---|---|---|
| **Exclusion** — the set names labels to stay *quiet* about | `person` is suppressed; everything else is notable | AURA begins narrating objects it has been silent about for the life of the project. Matches the constant name, the inline comment, and the module docstring. |
| **Inclusion** — the set names labels to *react* to | Only `person` is notable | Current behavior is correct; the constant name and both comments are wrong. Matches the parameter name `reactive_labels` and the `not in` predicate. |

Weight of evidence: **3 to 1 in favour of exclusion** (constant name,
inline comment, module docstring) against one identifier (`reactive_labels`).

**Technical recommendation (DeepSeek).** Adopt the **exclusion** reading — it
matches the three documented statements of intent, and the module docstring is
the most considered artifact in the file. Then rename `reactive_labels` →
`quiet_labels`, rename the constant to `QUIET_BY_DEFAULT`, and invert the
predicate to `if label in self._quiet_labels`. **However:** this is the
highest-blast-radius change in Phase 1, because it changes what AURA says out
loud. The one-identifier counter-argument is real, and only the owner knows
whether the intent was ever "only react to people."

**Decision still required from the owner.** Exclusion or inclusion.

---

### D2 — Redis Stream Delivery

**Current producer/consumer/ACK behavior.**

- **Producer.** `EventBus.publish()` → `XADD stream {"data": <json>}`. No
  trimming (F-2). Used by perception (`PERCEPTS_STREAM`) and by the mind for
  outgoing actions (`actions:{body_id}`).
- **Consumer.** `EventBus.consume()` → `XREADGROUP group consumer {stream:
  ">"} count=10 block=5000`, wrapped in a `try` that `continue`s on
  `redis.TimeoutError` / `redis.ConnectionError`. Yields `(entry_id, model)`.
  Parse is unguarded (G-8).
- **Groups.** `ensure_group()` creates the group at `$`, tolerating
  `BUSYGROUP`.
- **ACK.** `ack()` → `XACK`, called by the consumer after processing —
  `writer.py:35` after `memory.write()`; `reasoning_loop.py:69` after the LLM
  call and the action publish.

**At-most-once behavior.** This is what the code does. `">"` returns only
never-delivered entries; a delivered-but-unacked entry sits in the PEL and is
never re-read, because there is no `XAUTOCLAIM` / `XCLAIM` / `XPENDING`
anywhere. Net: **every message is delivered at most once; a crash between
delivery and ACK loses it permanently.**

**At-least-once behavior.** Would require draining the PEL on startup and
claiming stale entries (e.g. `XAUTOCLAIM` with a `min_idle_time`), plus the
guard from G-8 so a malformed backlog entry cannot kill the consumer, plus
step 3 so the idle threshold is meaningful. Net: **messages are re-delivered
until ACKed, so duplicates are guaranteed on any crash-after-process /
before-ACK.**

**Duplicate implications.**

- **Memory writer.** Duplicates are already absorbed: `write()` uses
  `ON CONFLICT (id) DO NOTHING` (see I-3). Percept IDs are UUIDs generated by
  the producer, so a redelivered percept is a no-op insert. This is the one
  consumer that is already safe under at-least-once.
- **Mind.** Duplicates are **not** free. `reasoning_loop.py` would generate a
  second LLM call and publish a second `Action` — AURA would speak the same
  observation twice. The `NoveltyFilter` cooldown partly masks this today,
  but only while G-1 stays unfixed and only for the one label that currently
  passes the filter. Fixing G-1 removes that accidental protection.

**Technical recommendation (DeepSeek).** **Correct the docstring; do not
change the delivery behavior yet.** The verified defect in G-4 is
documentation, not code — at-most-once is a legitimate design choice for an
ambient companion, where losing one frame's observation is harmless and
speaking twice is not. Under the standing constraint that existing behavior is
preserved unless a verified bug requires changing it, and the constraint that
unnecessary abstractions are not introduced, implementing durable delivery is
an architectural addition, not a bug fix. If it is ever implemented, it must
come **after** step 3 (so the idle threshold exceeds the worst-case stall),
**after** step 12 (so duplicates become observable rather than hidden by the
filter), and **after** the G-8 guard.

**Decision still required.** Docstring-only correction, or implement durable
delivery (at-least-once).

---

### D3 — Schema Initialization

**Options considered.**

| Option | Description |
|---|---|
| **A** | `run_aura.py` preflight initializes the schema before starting children. Requires a schema-owning module callable from the supervisor. |
| **B** | Each entry point initializes its own schema on startup (current `__init__` behavior, made explicit). |
| **C** | **Preflight *and* each entry point** idempotently — `run_aura.py` initializes before spawning, and each standalone entry point (`writer.py`, `query_local.py`, `conversation.py`) does the same on its own startup. |

**Trade-offs.**

- **A** centralizes the DDL and avoids four concurrent `CREATE TABLE IF NOT
  EXISTS` racing on startup, but does nothing for the standalone debug tools
  (`writer.py`, `query_local.py`, `conversation.py`), which are documented and
  expected to run independently — they would fail against a fresh database.
- **B** keeps every entry point self-sufficient, at the cost of redundant DDL
  in the supervised path (four processes each running it once).
- **C** costs one extra idempotent DDL in the supervised case and makes every
  entry point correct on its own. `CREATE TABLE IF NOT EXISTS` is safe to
  repeat; the concurrent race in the supervised case is benign (both would
  create-or-noop) though it can emit a duplicate-object error under a tight
  race.

**Technical recommendation (DeepSeek).** **Option C.** It is the only option
that preserves the documented standalone-entry-point behavior (constraint:
preserve existing behavior) without making the supervised path depend on
spawn order. The redundant DDL is once per process, not per question — which
is the actual defect G-7 was filed for.

**Decision still required.** Option A, B, or C.

---

### D4 — Exception Type — **WITHDRAWN**

**Why it was raised.** H-3 suggested that a psycopg exception interpolated
into preflight output might carry the DSN — including the password — into
user-visible text. If true, the fix might have required changing which
exception type is caught or raised.

**Why it is withdrawn.** V2 was verified by **direct experiment**, not
inference. Connecting to
`postgresql://…:<marker>@127.0.0.1:1/postgres` with `connect_timeout=2`
raises `psycopg.errors.ConnectionTimeout`, whose `str()` is
`"connection timeout expired"` and whose `repr()` is
`ConnectionTimeout('connection timeout expired')`. **The marker appeared in
neither.** The credential-leak premise fails on the path AURA actually hits.

**Consequence.** The exception type does not need to change. H-3's surviving
scope is the minimal presentation fix at step 11 — drop the `{exc}`
interpolation. No caller depends on the exception type: grep confirms **no**
`except psycopg…` / `OperationalError` / `DatabaseError` handler anywhere in
the tree. The only handlers are `bus/stream.py:49`
(`redis.ResponseError`) and `gemini_provider.py:110`, `:151` (`APIError`).

**Residual uncertainty (labelled).** `psycopg/conninfo.py:104` handles the
malformed-conninfo path with `except e.OperationalError as ex: raise
e.ProgrammingError(str(ex)) from None`. **Not executed — unverified.** If the
DSN is malformed rather than merely unreachable, driver text could still reach
the message.

---

### D5 — Retention

Redis and PostgreSQL retention are **separate systems** and must not be
combined into one decision.

#### D5 — Redis retention

**Current behavior.** Nothing is ever trimmed. `publish()` issues `XADD`
without `maxlen`; no `XTRIM`, `MAXLEN`, or `MINID` exists anywhere in the
tree. Both `PERCEPTS_STREAM` and `actions:{body_id}` grow without bound.
Consumed entries are not removed — `XACK` clears the PEL, not the stream.
Production rate is camera-framerate-driven (one `XADD` per detected object per
frame); drain rate is unmeasured and, per H-4, can stall 130 s at a time.

| Option | Description | Consequence |
|---|---|---|
| **A — fixed count (`MAXLEN ~ N`)** | Approximate trimming keeps `XADD` amortised O(1). Bounds memory in entries. | Works on any Redis/Memurai version. Memory bound is in entries, so it scales with payload size. If `N` is set below the consumer's backlog, trimming can discard entries a live consumer has not yet read — data loss on the unread tail. |
| **B — time-based (`MINID`)** | Trim entries older than a timestamp, e.g. keep 24 h. | **Requires Redis 6.2+** (the `MINID` trim strategy on `XADD`/`XTRIM`). Memurai implements the Redis 6.2/7.x command surface, but **the installed Memurai version was never checked in this audit — unverified.** Also requires a clock-derived ID on every publish. |
| **C — unlimited** | Current behavior. | Zero risk of losing unread entries; unbounded host memory growth for the life of the process. |

**Technical recommendation (DeepSeek).** **Option A**, with the value derived
from step 15's measurement rather than chosen now — the standing constraint
explicitly forbids picking a `MAXLEN` before production and drain rates are
measured. Option B's version dependency is real but unverified, and there is
no reason to take it on when A bounds the same memory. Option C is what F-2
filed.

**Decision still required.** Which unit (count vs time) and which value. **The
value cannot be chosen before step 15.**

#### D5 — PostgreSQL retention

**Current behavior.** The `percepts` table is append-only. `write()` inserts;
nothing ever deletes. Two indexes are maintained (`idx_percepts_ts` DESC,
`idx_percepts_type`). No partitioning, no pruning job, no retention policy,
no migration tooling.

**Whether the table grows indefinitely.** Yes. Growth is one row per detected
object per frame — the same rate that drives Redis growth in F-2, but here it
lands on disk rather than RAM.

**Whether retention is needed now.** This is the open question, and it is a
product question, not a technical one: the percept history **is** AURA's
memory. `mind/query.py` reads a 600-second / 50-row window
(`MEMORY_WINDOW_SECONDS`, `MEMORY_LIMIT`), so only recent rows are read today —
but deleting older rows would destroy data that a future feature (longer-term
recall, summarization) is the obvious reason to keep.

**Possible strategies.** (a) none yet; (b) time-window delete (e.g. rows older
than N days); (c) partition by month with detach-and-drop; (d) roll up into
summaries before deleting.

**Technical recommendation (DeepSeek).** **No action now.** This is disk, not
RAM; the table is the product; and pruning deletes data without a verified
defect compelling it. Revisit when measured (step 15) growth makes the disk
cost concrete. If a bound is wanted later, (b) is the smallest change and
(c) is the one that scales.

**Decision still required.** Whether to add a Postgres retention policy now,
and if so which strategy.

---

## Important Verified Findings

Consolidated list of the findings that were established by **verification**
rather than inference. Full detail in the sections above.

1. **G-5 — transport/DNS failure handling.** The complete failure path was
   traced from installed google-genai 2.23.0 source: DNS failure →
   `httpx.ConnectError` → **the SDK does not retry** (`retry_options` defaults
   to `None` → `retry_args(None)` → `stop_after_attempt(1)`, even though
   `_HTTPX_TRANSIENT_EXC` already lists `ConnectError`) → the project's
   tenacity predicate matches only `ServerError` → the model-fallback `except
   APIError` does not match the httpx hierarchy → `reasoning_loop.py:58` is
   unguarded → the process dies → the supervisor warns once and never
   restarts. **Three retry layers, none covering transport errors.**
   Explicitly verified: the SDK does **not** catch or retry this on its own.
2. **H-4 — PostgreSQL connection timeout.** Four bare `psycopg.connect` calls
   with no `connect_timeout`; psycopg's default is 130 s (verified at
   `conninfo.py:23`). The writer connects per row, so this stalls the drain
   path. Hard predecessor to any durable-delivery work.
3. **`_build_dsn()` password escaping.** Raw f-string interpolation of the
   password into the DSN; a password containing `@ : / # ?` misparses the URI.
   ID unassigned.
4. **V2 — DSN in exception text: negative.** Established by running the probe,
   not by reading. Marker absent from both `str()` and `repr()`. This is what
   withdrew D4.
5. **All documentation/contract mismatches** — nine verified items (D-a
   through D-i), each confirmed by reading the exact lines rather than
   inferring from names.
6. **Other verified correctness/reliability findings** — G-1 (predicate
   inverted relative to three statements of intent), G-2 (`already_warned`
   suppresses all repeat warnings; no restart), G-3 (second guard is dead
   code), G-6 (no sleep on `None` frame), G-7 (DDL per construction), G-8
   (unguarded parse kills the consumer), G-9 (construction per question), H-2
   (hardcoded fallback), F-1 (live token in the tree), F-2 (no trimming).
7. **Absence claims verified true**: no tests, no version control, no
   migrations, no restart policy.

**Explicitly unverified / uncertain** (do not treat as established):

- `conninfo.py:104` malformed-conninfo path — not executed.
- V3 — the three Gemini model IDs in `DEFAULT_MODELS` were not validated
  against the live model list.
- Step 15's production/drain rate measurement — not performed.
- The installed Memurai version, and therefore whether `MINID` is available.
- The exact verbatim lines of `perception/run_local.py` (shape known:
  print-then-continue; the file was not read in full this session).
- The full contents of `bodies/desktop/conversation.py` (partially known).
- Severity grades marked *(uncertain)* in the finding tables.

---

## Implementation Plan

Reproduced as established. Nothing here has been implemented.

**Global constraints in force** (owner-stated, apply to every step):

1. Fix correctness/reliability before architectural improvements.
2. Do not implement D2 until PostgreSQL timeout behavior is addressed.
3. Do not choose a Redis `MAXLEN` value until production and drain rates are
   measured.
4. Do not introduce unnecessary abstractions.
5. Preserve existing behavior unless a verified bug requires changing it.
6. Do not refactor unrelated code.
7. Every code change must have a verification step.

Two tests are exceptions to their phase's position and must execute **first**:
step 18 (harness) before any Phase 1 test lands, and step 19 (NoveltyFilter
test) before step 12.

---

### PHASE 1 — Correctness and runtime reliability

**Step 1 — H-2 hardcoded fallback password**
- **Files:** `memory/episodic/store.py` — `_FALLBACK_PASSWORD` (line 44), `_build_dsn()` (line 64)
- **Intended change:** delete the constant; replace line 64 with a lookup that raises `RuntimeError` carrying PowerShell and bash export examples when the variable is unset.
- **Verification:** unittest — `RuntimeError` when the env var is absent; the value appears in the DSN when present.
- **Dependencies:** none.
- **Blocked?** No.

**Step 2 — `_build_dsn()` password escaping**
- **Files:** `memory/episodic/store.py` — `from urllib.parse import quote`; line 65
- **Intended change:** `f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{db}"`
- **Verification:** unittest asserting `psycopg.conninfo.conninfo_to_dict(dsn)["host"]` equals the intended host for passwords containing `@ : / # ?`.
- **Dependencies:** step 1 (same function; step 1 edits the line above).
- **Blocked?** No.

**Step 3 — H-4 connection timeout**
- **Files:** `memory/episodic/store.py` — new `_CONNECT_TIMEOUT` constant; lines 80, 85, 107, 123
- **Intended change:** add `_CONNECT_TIMEOUT = 2` (matching `run_aura.py:54`) and pass `connect_timeout=_CONNECT_TIMEOUT` at all four connect sites.
- **Verification:** grep asserts four occurrences and no bare `psycopg.connect(self._dsn)`; unittest stubbing `psycopg.connect` asserts the kwarg on each method.
- **Dependencies:** none. **Required before step 17.**
- **Blocked?** No.

**Step 4 — V3 verify Gemini model IDs**
- **Files:** `mind/llm_provider/gemini_provider.py:33-37`
- **Intended change:** none — read-only check that the three IDs in `DEFAULT_MODELS` resolve.
- **Verification:** the IDs resolve against the current model list.
- **Dependencies:** none. **Gates step 5.**
- **Blocked?** **Yes — requires owner/external verification (V3 unresolved).**

**Step 5 — G-5 part 1: enable SDK retry**
- **Files:** `gemini_provider.py:53`
- **Intended change:** pass `http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=3))` to `genai.Client`, with a comment recording that the SDK defaults to `stop_after_attempt(1)` and already lists `httpx.ConnectError` as transient.
- **Verification:** spy on the constructor asserts `http_options` is passed; stub raising `ConnectError` once then succeeding asserts `generate()` returns text.
- **Dependencies:** step 4.
- **Blocked?** Yes, on step 4.

**Step 6 — G-2 make worker death visible**
- **Files:** `run_aura.py:122-128`
- **Intended change:** reword the warning to state that AURA is degraded, the named service has stopped and **will not be restarted**, and the remaining services continue.
- **Verification:** fake process list with one `is_alive()` false asserts exactly one line per worker; a second iteration asserts none.
- **Dependencies:** step 5 — **deliberately coupled.** `already_warned` is what made G-5 invisible; landing the retry fix alone recreates the blind spot.
- **Blocked?** No (but should land with step 5).

**Step 7 — G-5 part 2: let the model fallback advance**
- **Files:** `gemini_provider.py` — `import httpx`; `:110`, `:151`
- **Intended change:** `except (APIError, httpx.ConnectError, httpx.TimeoutException) as err:` at both sites. The tenacity predicate is **deliberately left alone**.
- **Verification:** stub raising `ConnectError` for model 1 only asserts models 2 and 3 are attempted.
- **Dependencies:** step 5 — strictly after, so the SDK absorbs the common transient case first.
- **Blocked?** No.

**Step 8 — G-3 duplicate `__main__` guard**
- **Files:** `perception/service.py:102-104`
- **Intended change:** delete the second guard.
- **Verification:** AST test asserting exactly one module-level `if __name__ == "__main__"` node.
- **Dependencies:** none. Placed here so steps 9 edits a clean file.
- **Blocked?** No.

**Step 9 — G-6 camera failure spins**
- **Files:** `perception/service.py:72-75` + constants + `import time`; `perception/run_local.py`
- **Intended change:** `_MAX_CONSECUTIVE_FAILURES = 50`, `_FAILURE_SLEEP_SECONDS = 0.1`; sleep on `None`, raise `RuntimeError` at the threshold, reset the counter on success. `run_local.py` mirrors this but keeps its first-failure print.
- **Verification:** stub camera yielding `None` asserts the sleep and the `RuntimeError` at exactly N; `None, None, frame, None…` asserts the counter resets.
- **Dependencies:** step 8 (same file).
- **Blocked?** **Partly — the exact diff for `run_local.py` requires reading that file first (it was not read in full).** See [Remaining Decisions](#remaining-decisions).

**Step 10 — G-8 malformed payload kills the consumer**
- **Files:** `bus/stream.py` — `import logging`; `ValidationError` import; `logger`; `:79-82`
- **Intended change:** wrap the parse in `try/except (ValidationError, KeyError, TypeError)`, log a warning with entry id / stream / group / consumer, `XACK` the bad entry, `continue`.
- **Verification:** stub-redis test with one malformed entry then one valid asserts exactly one yield, no raise, two `xack` calls.
- **Dependencies:** none. **Required before step 17.**
- **Blocked?** No.

**Step 11 — H-3 drop driver text from preflight output**
- **Files:** `run_aura.py:56-58`
- **Intended change:** `except Exception:` (drop `as exc`), remove the `{exc}` interpolation. D4 withdrawn, so the exception type is untouched — this is H-3's entire surviving scope.
- **Verification:** forced connection failure asserts the message names host/port/db and contains no raw driver text.
- **Dependencies:** none (same file as step 6 — land after).
- **Blocked?** No.

**Step 12 — G-1 / D1 NoveltyFilter semantics — GATED**
- **Files:** `decision/policy.py:22`, `:26-33`, `:36`
- **Intended change:** **Branch A (exclusion)** — rename constant to `QUIET_BY_DEFAULT`, parameter to `quiet_labels`, attribute to `_quiet_labels`, predicate to `if label in self._quiet_labels:`. **Branch B (inclusion)** — keep the predicate, rename the constant to `REACTIVE_LABELS` and rewrite the comment; no behavior change.
- **Verification:** four cases written from stated intent — `person` → `False`, `cup` → `True`, repeat within cooldown → `False`, after cooldown → `True`. Must be **seen failing** before the change.
- **Dependencies:** step 19 (its test) must exist first.
- **Blocked?** **Yes — D1 undecided.** Branch B is the constraint-5 default (preserve behavior).

---

### PHASE 2 — Data/event reliability

**Step 13 — G-7 schema initialization (D3) — GATED**
- **Files:** `memory/episodic/store.py:75-82`; call sites in `writer.py`, `query_local.py`, `conversation.py`, `run_aura.py`
- **Intended change:** remove `_ensure_schema()` from `__init__`; add an explicit `initialize()` that runs the DDL with `connect_timeout`; call it once per process at startup. Under D3 Option C, both the preflight and each standalone entry point call it.
- **Verification:** stub connection asserts zero DDL on construction and one on `initialize()`; each entry point starts against a fresh database.
- **Dependencies:** steps 3 (timeout constant), 10 (call sites enumerate cleanly).
- **Blocked?** **Yes — D3 undecided** (Option C recommended).

**Step 14 — G-9 `EpisodicMemory` per question**
- **Files:** `mind/query_local.py:41`; `bodies/desktop/conversation.py`. `mind/query.py:64` **unchanged**.
- **Intended change:** construct one instance at process start, call `initialize()`, pass `memory=memory` into `answer_question()`.
- **Verification:** construction counter across two calls asserts `1`, not `2`.
- **Dependencies:** step 13.
- **Blocked?** Yes, on step 13.

**Step 15 — D5 measure production and drain rates**
- **Files:** none — read-only measurement.
- **Intended change:** none. Sample `XLEN percepts` twice 60 s apart; count rows/minute in Postgres; read `INFO server` for the Redis version.
- **Verification:** the three measurements exist and are recorded.
- **Dependencies:** step 3 — drain rate is meaningless while the writer can stall 130 s.
- **Blocked?** No. **Gates step 16.** *Caveat: `write()` connects per row, so the measured drain reflects that cost — this measures current behavior, and changing it is out of scope (constraint 6).*

**Step 16 — F-2 bound the streams — GATED**
- **Files:** `bus/stream.py:40-43` + new constant
- **Intended change:** add `maxlen=<measured N>, approximate=True` to the `XADD` in `publish()`. One constant covers both `percepts` and `actions:{body_id}`.
- **Verification:** stub `xadd` kwargs assert `maxlen` present and positive.
- **Dependencies:** step 15.
- **Blocked?** **Yes — D5 Redis retention undecided, and the value requires step 15.**

**Step 17 — G-4 / D2 delivery semantics — GATED**
- **Files:** `bus/stream.py` (`consume`, `ensure_group`, new drain method); call sites in `writer.py`, `reasoning_loop.py`
- **Intended change:** under constraint 5 the **default is no code change** — the verified defect is the docstring (step 21). If durable delivery is chosen: drain the PEL on startup with `XAUTOCLAIM` at a `min_idle_time` exceeding the worst-case stall.
- **Verification:** if implemented — stub PEL with one stale entry asserts it is yielded and acked and that `xautoclaim` receives `min_idle_time` ≥ threshold.
- **Dependencies:** steps 3, 12, 15.
- **Blocked?** **Yes — D2 undecided.** Constraint 2 also applies (Postgres timeout must precede this).

---

### PHASE 3 — Tests

**Step 18 — T-2 test harness** *(must execute first — exception to phase position)*
- **Files:** new `tests/`
- **Intended change:** stdlib `unittest` only, run via `python -m unittest discover -s tests`. No new dependency (constraint 4).
- **Verification:** the command exits 0 with zero tests collected.
- **Dependencies:** none.
- **Blocked?** No.

**Step 19 — T-1 NoveltyFilter test** *(must execute before step 12 — exception to phase position)*
- **Files:** new `tests/test_policy.py`
- **Intended change:** the four intent-derived cases. Written from the **stated intent**, not from current code, or it locks the bug in.
- **Verification:** fails against current code, passes after step 12.
- **Dependencies:** step 18.
- **Blocked?** No.

**Step 20 — tests landing alongside their fixes**

| Fix step | Assertion |
|---|---|
| 1 | `RuntimeError` when `AURA_PG_PASSWORD` absent |
| 2 | host survives `@ : / # ?` in the password |
| 3 | `connect_timeout` passed at all four sites |
| 5 | `http_options` reaches the constructor; transient-then-success returns text |
| 6 | exactly one degraded line per dead worker |
| 7 | `ConnectError` on model 1 → models 2 and 3 attempted |
| 8 | exactly one `__main__` guard |
| 9 | N failures → `RuntimeError`; counter resets on success |
| 10 | malformed + valid entry → one yield, no raise, two acks |
| 13 | no DDL on construct; one on `initialize()` |
| 14 | one construction across two calls |
| 16 | `maxlen` on every publish |

All use stubs — no live Redis, Postgres, Gemini, or camera.

- **Blocked?** No.

---

### PHASE 4 — Documentation/contracts

Methods steps 21-30 are doc-only, no behavior change (constraint 5).

**Step 21 — G-4 `consume()` docstring.** Replace the false redelivery sentence with the actual at-most-once guarantee. *Wording depends on D2's outcome* — if step 17 is implemented, document the new guarantee instead. The module docstring at `bus/stream.py:11-13` is verified TRUE and stays. **Blocked? Partly — D2 changes the wording.**

**Step 22 — see `seen_recently`.** Delete `memory/episodic/store.py:120-132`. `timedelta` remains used by `recent()`, so no import cleanup. **Blocked? Owner confirmation** (dead-code removal is outside a strict correctness fix). Also ID unassigned.

**Step 23 — L-4 `base.py:36`.** `400` → `500`. The code is the 3-to-1 majority. **Blocked? No.**

**Step 24 — README:73-75.** Replace the false "No conversation yet" note with what `bodies/desktop/conversation.py` actually does and how to start it. **Blocked? No.**

**Step 25 — README:23.** Replace the "Every arrow above is a Redis Stream" claim to note the Body's startup greeting is a direct Python call into `mind/greeting.py` from `bodies/desktop/run.py`. **Blocked? No.**

**Step 26 — README:56-61.** Add `mind/query_local.py` and `bodies/desktop/conversation.py` to the standalone tools list. **Blocked? No.**

**Step 27 — `mind/query_local.py:5-7`.** Stale future tense → present tense. **Blocked? No.**

**Step 28 — README:84-457.** Delete the pasted log. **Capture the two `httpx.ConnectError` tracebacks into a G-5 issue first — they are the primary evidence for G-5.** **Blocked? No.**

**Step 29 — I-3 `write()` docstring.** Document `ON CONFLICT (id) DO NOTHING` as retry-idempotency, not deduplication. **Blocked? No.**

**Step 30 — I-4 README.** Note the `yolov8s.pt` runtime download on CUDA hosts. **Blocked? No.**

**Why Phase 4 is safe here:** every doc string now describes code that already behaves as described, because Phases 1-2 landed first. Writing it earlier documents intent rather than behavior — the exact failure this audit found nine times.

---

### PHASE 5 — Optional engineering improvements

**Step 31 — K-2 `pyproject.toml`.** Remove or correct `[tool.hatch.build.targets.wheel]` (`packages = ["contracts"]` omits every application package). Last because it is packaging config, behind every correctness fix (constraint 1). **Blocked? No.**

**Step 32 — J-4 `mind/query.py` `build_qa_prompt`.** **Deferred.** Adopting `Persona.system_prompt()` would inject the "introducing yourself for the first time this session" framing into Q&A answers — a behavior change with no verified bug behind it (constraints 5, 6). **Blocked? Not blocked — deliberately deferred.**

**Step 33 — D5 PostgreSQL retention.** **No action now.** Disk, not RAM; the table is the product; pruning deletes data without a verified defect compelling it. **Blocked? Owner decision if a policy is wanted.**

**Step 34 — J-5 `sys.path.insert`.** No action; document as intentional run-from-repo-root if anything. **Blocked? No.**

**Withdrawn:** J-1, D4 (see [Withdrawn and downgraded findings](#withdrawn-and-downgraded-findings)).

---

## Remaining Decisions

Only decisions the **project owner** must make. Everything else is resolvable
from the code or the installed dependencies.

> **Status as of 2026-09-29 — the table below is retained as the original
> record.** Items 1–8 have since been answered, and items 6 and 8 were closed
> by verification rather than by decision. See
> [Session 3](#session-3--2026-09-29-remediation-started) for the authoritative
> answers. The only item still genuinely open is **item 4, D5 Redis retention**,
> which cannot be answered until step 15 measures production and drain rates.

| # | Decision | Blocks | Recommended default |
|---|---|---|---|
| 1 | **D1** — NoveltyFilter: exclusion or inclusion | Step 12, and step 19's assertions | Exclusion (3-to-1 documented intent); inclusion is the constraint-5 preserve-behavior default |
| 2 | **D2** — Redis delivery: docstring-only, or implement durable at-least-once delivery | Step 17, step 21's wording | Docstring-only |
| 3 | **D3** — Schema initialization: Option A (preflight), B (each entry point), or C (both) | Step 13's call sites | **Option C** |
| 4 | **D5 Redis retention** — fixed count vs time-based `MINID`, and the value | Step 16 | Fixed count, **value only after step 15** |
| 5 | **D5 PostgreSQL retention** — add a policy now, and which strategy | Step 33 | No action now |
| 6 | **V3** — verify the three Gemini model IDs in `DEFAULT_MODELS` | Step 5 (and therefore steps 6, 7) | Resolve before step 5 |
| 7 | **Delete `seen_recently`?** — dead code with a false docstring; removing it is technically outside a strict correctness fix (constraint 6) | Step 22 | Delete |
| 8 | **`perception/run_local.py` in step 9?** — its exact diff requires reading the file first, which has not been done | Step 9's scope | Include it |
| 9 | **Phase 0 — token rotation authorization** (owner action) | Everything | Rotate first; the value must be assumed compromised |
| 10 | **Phase 0 — authorization to delete `start-claude.sh` before `git init`** | Any `git init` | Delete before init, or the token enters permanent history |

Steps 1-3, 5-11, 13-16, and 18-30 are executable without any gate once Phase 0
is cleared. Steps 4, 12, 13, 16, 17, and 21 are blocked on the decisions above.

---

## Next Session Instructions

The next Claude Code session must:

1. **Read this file first.** It is the authoritative handoff. Do not re-derive
   what is recorded here.
2. **Do not repeat the entire audit.** Passes 1 and 2 are complete and their
   evidence is recorded above with file and line references. Re-verify a
   finding only if the file has changed since 2026-09-28.
3. **Resolve the remaining verification gates.** The five items in
   [Important Verified Findings](#important-verified-findings) marked
   unverified — the `conninfo.py:104` malformed-conninfo path, V3, the step-15
   measurement, the Memurai version, and the unread `perception/run_local.py`
   and `bodies/desktop/conversation.py` — are all resolvable without owner
   input.
4. **Ask for owner decisions only where genuinely necessary.** The ten items
   in [Remaining Decisions](#remaining-decisions). Do not ask about anything
   resolvable from the code.
5. **Continue from the first uncompleted implementation step.** Step 1
   (`_build_dsn()` / H-2) is the first. Steps 2, 3, 5, 6, 7, 8, 10, 11 are
   unblocked and sequential within Phase 1; step 4 gates step 5.
6. **Do not modify code until the owner explicitly authorizes
   implementation.** At the time of writing, the standing instruction is
   "Do not implement anything."

### Phase 0 authorization — **GRANTED AND EXECUTED 2026-09-29**

Originally stated as: no implementation may begin until the F-1 token has been
rotated by the owner, the owner has authorized deleting `start-claude.sh`, and
the owner has authorized `git init` (which must follow the deletion, never
precede it).

**Current state:**

- Deleting `start-claude.sh` — **authorized and done**, before `git init`.
- `git init` — **authorized and done.**
- Token rotation at the issuer — **still UNCONFIRMED.** The owner will verify
  this separately and has instructed that it must not be assumed. Treat the
  token as live until the owner explicitly confirms otherwise.

Implementation was authorized to proceed by the owner on 2026-09-29 despite the
rotation being unconfirmed. See
[Session 3](#session-3--2026-09-29-remediation-started).

### Preserved conventions

- **Finding IDs and step numbers above are exact** and must not be renumbered.
  Two findings carry **ID: unassigned** because the audit never assigned one —
  do not invent IDs for them.
- **Never reproduce secret values** in output, commits, or new files. F-1 is
  recorded by reference; the database fallback password literal is described,
  not quoted.
- **Severity grades are as recorded.** H-3 was revised Medium → Low; the
  reason is documented.
- **Uncertainty is labelled.** Anything marked *unverified*, *uncertain*, or
  *(not executed)* has **not** been established — do not treat it as fact.
- **Preserve technical detail.** Do not summarize the G-5 path, the H-4
  reasoning, or the D2 duplicate analysis — the specifics are the value.

---

# RESUME POINT

**Updated 2026-10-07 (session 9).** Postgres is unblocked, plan steps 5-10
are **LANDED**, the prompt leak is **FIXED**, the Body owns the console, and
the memory writer can no longer be killed by a database hiccup or a poison
message. Suite: **80 tests, OK**. Preflight re-verified: **ALL CLEAR**.

**Priority 1 is still NOT complete, and it is one confirmed step away: run
it and press Enter.** Nothing has been heard from AURA's voice yet, and the
session-8 keyboard fix has never been exercised on real hardware.

## The next action, in full

```bash
.venv/Scripts/python.exe -u run_aura.py
```

Once the banner appears: wait for AURA's introduction, **press Enter**, speak,
wait for the recording window to close, and listen for the answer. Five
things to watch for — every one of them was broken or unverified before:

1. **Does "Press Enter to talk..." accept the Enter?** It should print
   `Listening...`. If instead it prints the stdin explanation,
   `_explain_dead_stdin()` now says *which* kind of dead stdin it saw — paste
   that line, do not summarise it.
2. **Is the spoken answer grounded in memory** — does she answer about what
   she has actually observed rather than inventing? And does she still answer
   a *general* question (e.g. "what is a black hole") freely?
3. **Is ambient narration suppressed during the exchange** — expect
   `(narration suppressed during conversation: ...)` lines, and no narration
   audio overlapping the recording window.
4. **Can you press Enter again and get a second answer?** The loop must
   accept Enter repeatedly, not once.
5. **Does the memory-writer survive the whole run?** It died in session 8.
   If the supervisor prints `Still running: perception, mind` again, it is
   now guarded — but **capture the traceback anyway**, because a guard that
   is firing when it should not is itself a finding.

**If a run fails, the message to capture is the traceback, not the summary.**
Twice now the diagnosis has turned on a detail that a paraphrase lost.

## Session 9's evidence — do not re-derive this

**The memory-writer death was database-side, not a bad message.** The `mind`
consumer reads the same stream with the same `Percept` model
(`reasoning_loop.py:46`) and survived the same run, so those entries parsed
fine. Both paths are guarded now; the connect timeout is the leading
explanation and is no longer 2 s.

## Known limitations, stated rather than papered over

- **No live search or retrieval tool** is attached to the Gemini calls, so
  current-events questions draw only on training data. That is a real
  limitation, not a bug — state it, do not work around it.
- **`EpisodicMemory` is constructed per question** (G-9) and runs
  `_ensure_schema()` — DDL per question. Not yet fixed.
- **The connect timeout is still a compromise.** The real fix is an SSD.
- **Delivery is at-most-once.** No `XAUTOCLAIM` / PEL drain exists; the
  writer's unacked-percept recovery therefore only happens **on restart**.

## Password — ask the owner first

**Do not run any Postgres password operation without telling the owner
first** — he has asked to supply it himself. Session 7's elevated reset
procedure is recorded below and works, but it is not to be run unannounced.

## Still owner-blocked

- **Punch-list item 1** (Postgres service reporting Stopped, 450 unpruned
  log files inside the data directory) — admin-level, includes a deletion.
- **F-1's issuer-side rotation** — **UNCONFIRMED**; treat that token as live.
- **The Gemini free-tier terms for the creator profile** — it is sent to the
  API on every question.

**What is left for Priority 1 is one thing: the end-to-end run.** Camera,
mic, Redis, Postgres, GEMINI key and a human pressing Enter have still never
been exercised together *successfully*. Stabilization is code-complete but
**unconfirmed** — do not treat it as done, and do not advance to item 2.

**The machine's disk I/O remains the binding constraint.** A cold
`import mind.query` measured **992 seconds** in session 6. Warm runs are
seconds (session 9: full suite 6 s). Plan for a cold start to take many
minutes; do not read slowness as a code failure, and do not conclude a run
has hung before warming the cache once.

## Before anything else — two checks

**1. Is the cache warm?** Run the same command twice — the second run is warm.
In session 6 nothing could be verified because every attempt paid the cold
cost from scratch.

**2. Preflight.** Resolved in session 7 — the local role password was set to
the existing `AURA_PG_PASSWORD` value, so this now returns **`CLEAR`**.

```bash
.venv/Scripts/python.exe -u -c "import run_aura; print(run_aura._preflight_checks() or 'CLEAR')"
```

Run Python through **`Bash`**, never `PowerShell` (it hangs after a Python
child exits, and its WMI cmdlets timed out at 2 minutes under disk load).
Prefer **`.venv/Scripts/python.exe` directly** — it is the pinned 3.12.14.
**Avoid `uv run` for probe/test commands on this machine**: it adds its own
resolution work on top of an already disk-bound start and burned **8 m 20 s**
in session 6. Never use bare `python` (system 3.11, no `psycopg`). Always
pass `-u`: Python block-buffers when redirected, so a killed run otherwise
leaves a **0-byte** file.

⚠️ **The preflight's Postgres message was not trustworthy on a cold machine.**
In session 6 it reported `Postgres is not reachable (connection timeout
expired)` while the server was in fact **Running** and answering in 1.34 s —
the 2 s `connect_timeout` at `run_aura.py:54` had simply elapsed during a cold
start. A real connection now succeeds, so it reports correctly again, but 2 s
is still tight on a cold disk: if it ever says "not reachable", confirm with a
direct connect before acting. **`password authentication failed`** was the
honest failure; "not reachable" on this box often was not.

- **`CLEAR`** → **Branch A**.
- anything else → **Branch B**.

## Branch A — preflight is clear

**Run AURA end to end.** This is the one remaining item for Priority 1, and
nothing about the interactive Body's runtime behaviour has been observed yet
— only the coordination logic, under stubs. (The `mind/query.py` policy did
execute in session 7 via `query_local.py`, and answered correctly; what is
still unobserved is the *voice* path through the Body.)

```bash
uv run python run_aura.py
```

Then press Enter, speak, and confirm all four:

1. A spoken answer comes back, grounded in what AURA has actually seen.
2. Ambient narration **stops** during the exchange and does **not** fire
   immediately afterwards. (That "fires afterwards" case is precisely the
   bug that was caught and fixed in Session 5 — if you see it, the fix
   regressed.)
3. A stray Enter pressed while AURA is speaking does **not** trigger a
   surprise recording.
4. AURA does not record her own voice — the mic is closed to narration for
   the whole exchange, not just playback.

Then **punch-list item 5**: re-verify `perception/service.py` `q` (full
stop) and `c` (display-only toggle) at runtime. `service.py` **changed in
session 7** (G-3, G-6), so this re-verify is now overdue rather than
optional — the key handling at `:91-94` was not touched and the new loop
logic is covered by tests, but the window has not been re-run since.

## Branch B — preflight fails

Diagnose before changing anything. Session 7 cleared the last known cause
(the password), so a new failure is a **new** problem, not the old one.

**Do not re-land steps 5-9 — they are done.** See
[Session 7](#session-7--2026-10-06-later-postgres-unblocked-steps-5-9-landed-live-g-5-failure-caught-and-fixed)
for what changed and how each was verified.

### Next, in this order

**1. Step 10** (G-8, guard the message parse) → **Step 11** (H-3, drop `{exc}`
from the preflight message) → **Step 19** (NoveltyFilter test) → **Step 12**
with **D1 = INCLUSION**.

**2. Punch-list item 1** (Postgres service) — still untouched: 450 unpruned
log files inside the data directory, and the service reporting Stopped while
the process runs. Owner go-ahead required; admin-level, and includes a
deletion.

### Still owner-blocked

- **Punch-list item 1** (Postgres service, 450 unpruned log files) —
  admin-level, includes a deletion.
- **The creator profile is sent to Gemini on every question.** Check the free
  tier's terms before accepting it; options are recorded in session 6.
- `DEFAULT_DSN` still resolves to the **`postgres` maintenance database**
  (`store.py:44`) rather than a dedicated `aura` database.
- **F-1's issuer-side rotation** — still **UNCONFIRMED**; treat the token as
  live.

## Then, in the recorded order

**0. The end-to-end run** (see "The next action, in full" above). This comes
   first and is the only thing blocking Priority 1. Everything else below is
   item 2+ work and stays parked until a spoken answer has been heard.

1. **Step 11** (H-3, drop `{exc}` from the preflight message) → **Step 19**
   (NoveltyFilter test) → **Step 12** with **D1 = INCLUSION (Branch B)**.
   **Step 10 is DONE** (session 9) — do not redo it. Note that step 17 (PEL
   drain) was gated on step 10 precisely so a malformed backlog entry could
   not kill the consumer on every restart; that gate is now satisfied.
   **Before starting step 11:** the PEL is no longer empty of failures — the
   writer leaves unwritten percepts unacked by design. Step 11 is unrelated,
   but step 17 will now have real entries to drain.
2. **Steps 5-10 are DONE** (sessions 7 and 9). **Session 8's Body fix is
   also DONE.** Do not redo any of them.

## Standing constraints

**Do not** re-audit, re-derive findings, or reopen any decision recorded
above.

**The pre-commit gate is now SATISFIED** — `.gitignore` carries
`__pycache__/` and `*.py[cod]`, the two password literals
(`README.md:33`, `memory/episodic/writer.py:8`) are replaced, and
`README.md` documents `AURA_PG_PASSWORD`. The repo still has **no commits**
and the whole tree is untracked, so the first commit is now *possible*, but
**committing is still the owner's call — do not commit unprompted.**

**Punch-list item 1 (Postgres) needs the owner's explicit go-ahead.** The
proposed fixes are admin-level and one is a deletion (the unpruned log files —
**447 in session 5, 450 in session 6**). Nothing has been changed. Also still
unanswered: whether to keep the application schema in the `postgres`
maintenance database or move to a dedicated `aura` database (`store.py:44`).

**The creator profile is now in the Q&A system prompt** (`mind/persona.py`,
`creator_knowledge()`) and is therefore sent to the Gemini API on every
question. It was supplied by the owner on 2026-10-06 and is entirely his to
narrow, relocate to phase-2 memory, or withdraw. Do not quietly drop or trim
it — ask.

**Owner-blocked, do not touch without an explicit go-ahead:** the Postgres row
count, the supervised camera run, the Redis/Postgres retention values, and
F-1's issuer-side rotation — which remains **UNCONFIRMED**, so treat that
token as live.

