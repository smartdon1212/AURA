"""
InteractionGate: coordinates the two things that can make any Body speak.

Moved here from bodies/desktop/conversation.py when the web Body landed.
Nothing about it was ever desktop-specific — it is the *Body-side* half of
the ambient-narration policy, and every Body has both halves:

  - unprompted narration arriving on the bus (a background thread)
  - answers to questions the user just asked (a foreground thread)

so every Body needs to keep the two from overlapping. The desktop Body
imports it from here via conversation.py; the web Body imports it
directly. Duplicating it per Body would mean the drop-don't-queue policy
could drift between them, which is exactly the kind of divergence that is
invisible until narration starts firing into the middle of a recording.

The policy itself is stated in the class docstring below — it was agreed
with the owner and is not style, it is behaviour.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

# An exchange holds narration off. If whatever is supposed to end it never
# does — a browser tab closed mid-recording, a page reloaded while AURA
# was answering — the flag stays set and ambient narration is suppressed
# for the rest of the session, silently. The user would see a face that
# has stopped noticing anything, and no error anywhere.
#
# So the hold is bounded. It has to be generous: a slow Gemini answer plus
# a long spoken reply can plausibly run to a minute. Two minutes is the
# point at which "still answering" stops being the likelier explanation.
EXCHANGE_MAX_SECONDS = 120.0


class InteractionGate:
    """
    Coordinates the two things that can make the Body speak: ambient
    narration arriving on the bus (background thread) and answers to
    questions the user asks (foreground thread).

    A single lock and a single flag, because all three constraints below
    reduce to "these two must not overlap":

      - Speech is exclusive. Two concurrent speak calls fight over one
        audio output and the loser is dropped silently — the desktop
        speaker builds a fresh pyttsx3 engine and calls runAndWait(), and
        two of those cannot share the one SAPI5 voice; the browser's
        speechSynthesis has the same single-utterance limit.
      - The gate is held across *recording*, not just playback. The
        microphone is open for the whole capture window, so anything AURA
        says in that window is transcribed back as though the user had
        said it.
      - Ambient narration is suppressed, not queued, while an exchange is
        in progress. The Body is responsible for making the drop
        permanent — it must ack the action it did not speak, or the
        consumer group's PEL redelivers it later, which is queuing under
        a different name.
    """

    def __init__(self) -> None:
        self._active = threading.Event()
        self._speak_lock = threading.Lock()
        self._started_at = 0.0

    @property
    def is_active(self) -> bool:
        """True while an exchange holds ambient narration off."""
        return self._active.is_set()

    def begin_exchange(self) -> None:
        """
        Opens an exchange without holding it on this thread.

        The desktop Body uses exchange() below, because its recording and
        playback both happen in this process. The web Body cannot: it
        records in the browser and speaks with the browser's own
        speechSynthesis, so the exchange spans several HTTP requests and
        there is no single block to wrap. It opens the exchange on
        /exchange/begin and closes it on /exchange/end.

        Deliberately no speech lock here. The lock exists to serialise
        access to one in-process audio device; in the web Body the audio
        device belongs to the browser, so there is nothing to serialise
        server-side. What both Bodies need is the flag, and that is what
        this sets.
        """
        self._started_at = time.monotonic()
        self._active.set()

    def end_exchange(self) -> None:
        self._active.clear()

    def expire_stale_exchange(self, max_age_seconds: float = EXCHANGE_MAX_SECONDS) -> bool:
        """
        Releases an exchange that was begun and never ended. Returns True
        if it had to — the caller should say so once, out loud, because
        the alternative is narration quietly suppressed for the rest of
        the session.
        """
        if not self._active.is_set():
            return False
        if time.monotonic() - self._started_at < max_age_seconds:
            return False
        self._active.clear()
        return True

    @contextmanager
    def exchange(self):
        """
        Held for the whole question-to-answer-finished window.

        The flag is set before the lock is taken so that a narration
        already in flight finishes and the loop then waits for it, rather
        than recording AURA's own voice mid-sentence.
        """
        self.begin_exchange()
        with self._speak_lock:
            try:
                yield
            finally:
                self.end_exchange()

    @contextmanager
    def ambient_speech(self):
        """
        Yields True if narration should be spoken, False if it should be
        dropped.

        The flag is tested *before* the lock, and that ordering is the
        whole point. An exchange holds the lock for its entire duration,
        so a caller that went straight for the lock would block until the
        exchange ended and then speak — narration piling up behind the
        conversation and firing the moment it finished. That is queuing;
        the agreed policy is to drop instead.

        The second test inside the lock closes the remaining race: this
        call may have waited for an earlier narration to finish, during
        which the user started an exchange.
        """
        if self._active.is_set():
            yield False
            return

        with self._speak_lock:
            yield not self._active.is_set()
