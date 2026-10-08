"""
The web Body's voice-out: consume the Actions the Mind routes to
`actions:web-01` and hand them to the browser over SSE.

The browser owns the speaker in this Body — it speaks with
speechSynthesis — so unlike bodies/desktop/run.py this module never
produces sound itself. It decides *whether* a line should be spoken and
publishes it; the page does the rest.

What is identical to the desktop Body, deliberately:

  - the gate policy. Ambient narration is dropped, not queued, while an
    exchange is active, and the dropped action is still acked. Leaving it
    unacked would park it in the consumer group's PEL and have it
    redelivered later, which is queuing under a different name.
  - the guard. This runs on a daemon thread, so an unguarded exception
    kills it silently and AURA simply stops commenting on what she sees.
    One warning per failure run, not one per action — the stream carries
    several entries a second.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bodies.gate import EXCHANGE_MAX_SECONDS, InteractionGate
from bodies.web.state import AuraState
from bus import EventBus, actions_stream
from contracts import Action, ActionIntent

BODY_ID = "web-01"
GROUP = "body-web"
CONSUMER = "web-1"


class NarrationService:
    def __init__(
        self,
        state: AuraState,
        gate: InteractionGate,
        bus=None,
        body_id: str = BODY_ID,
    ) -> None:
        self._state = state
        self._gate = gate
        self._bus = bus
        self.body_id = body_id

    def run(self) -> None:
        bus = self._bus or EventBus()
        stream = actions_stream(self.body_id)
        consecutive_failures = 0

        print(f"Web body narration listening on {stream}. Ctrl+C to stop.")

        for entry_id, action in bus.consume(stream, GROUP, CONSUMER, Action):
            try:
                if action.intent != ActionIntent.SPEAK:
                    bus.ack(stream, GROUP, entry_id)
                    continue

                # Checked here, every time narration arrives, because this
                # is the only moment a stuck exchange does any harm — and
                # because an exchange that was begun and never ended (a
                # tab closed mid-answer) would otherwise suppress
                # narration for the rest of the session with no symptom
                # other than AURA going quiet.
                if self._gate.expire_stale_exchange():
                    print(
                        "Warning: an exchange was left open for over "
                        f"{EXCHANGE_MAX_SECONDS:.0f} seconds without finishing "
                        "— releasing it so narration can resume. A browser tab "
                        "was probably closed mid-answer."
                    )

                text = action.params.get("text", "")
                with self._gate.ambient_speech() as should_speak:
                    # Both cases go to the browser. Suppressed narration is
                    # still shown, marked as suppressed, so the policy is
                    # visible rather than looking like AURA had nothing to
                    # say — the two are indistinguishable otherwise.
                    self._state.broadcast(
                        "narration", {"text": text, "suppressed": not should_speak}
                    )

                bus.ack(stream, GROUP, entry_id)

            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures == 1:
                    print(
                        f"\nWARNING: web narration hit an error ({exc!r}) and is "
                        "still running. AURA will not describe what she sees "
                        "until this clears."
                    )
                traceback.print_exc()
                continue

            if consecutive_failures:
                print(
                    f"Web narration recovered after {consecutive_failures} "
                    "failed action(s)."
                )
                consecutive_failures = 0
