"""
WebBody: wires the web Body's three moving parts together and holds the
one operation that is neither vision nor narration — answering a question.

Kept separate from app.py so that app.py is pure HTTP wiring and can be
constructed in a test from fakes, and separate from vision.py/narration.py
so neither of those needs to know about the other.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bodies.gate import InteractionGate
from bodies.web.narration import NarrationService
from bodies.web.state import AuraState
from bodies.web.vision import VisionService

BODY_ID = "web-01"


class WebBody:
    def __init__(
        self,
        state: AuraState | None = None,
        gate: InteractionGate | None = None,
        vision: VisionService | None = None,
        narration: NarrationService | None = None,
        transcriber=None,
        answerer=None,
    ) -> None:
        """
        Everything heavy is injectable and lazily defaulted, so a test can
        build a WebBody that touches no camera, no model, no Redis and no
        API key.

        `transcriber` and `answerer` default to None and are resolved on
        first use rather than here, because importing them drags in the
        provider stack — and because a Body that cannot reach Gemini
        should still serve the page rather than die at construction.
        """
        self.state = state or AuraState()
        self.gate = gate or InteractionGate()
        self.vision = vision or VisionService(self.state)
        self.narration = narration or NarrationService(self.state, self.gate)
        self.body_id = BODY_ID

        self._transcriber = transcriber
        self._answerer = answerer

    @property
    def transcriber(self):
        if self._transcriber is None:
            from mind.llm_provider import default_transcription_provider

            self._transcriber = default_transcription_provider()
        return self._transcriber

    @property
    def answerer(self):
        if self._answerer is None:
            from mind.query import answer_question

            self._answerer = answer_question
        return self._answerer

    def start(self) -> None:
        """
        Starts vision and narration on daemon threads. The HTTP server
        runs on the caller's thread; these two are the background half of
        the Body, exactly as in bodies/desktop/run.py.
        """
        threading.Thread(
            target=self.vision.run, name="web-vision", daemon=True
        ).start()
        threading.Thread(
            target=self.narration.run, name="web-narration", daemon=True
        ).start()

    def stop(self) -> None:
        self.vision.stop()

    def latest_jpeg(self) -> bytes | None:
        return self.vision.latest_jpeg()

    # ------------------------------------------------------------ exchange

    def begin_exchange(self) -> None:
        self.gate.begin_exchange()

    def end_exchange(self) -> None:
        self.gate.end_exchange()

    # ----------------------------------------------------------- answering

    def answer(self, audio: bytes) -> dict:
        """
        Transcribe a WAV and answer it. Blocking — call it from a worker
        thread (FastAPI's run_in_threadpool does this).

        The exchange window is opened by the *browser*, on
        /exchange/begin, before it starts recording, and closed on
        /exchange/end once AURA has finished speaking in the page. That
        is why this does not wrap itself in `gate.exchange()` when a
        window is already open — doing so would end the browser's window
        as soon as the answer was generated, and ambient narration could
        then talk over the reply while the page was still reading it out.

        The `if not is_active` branch is a fallback for a client that
        called /ask without /exchange/begin: narration is still held off
        for the duration of the answer, which is the part that matters
        most.
        """
        if self.gate.is_active:
            return self._answer(audio)

        with self.gate.exchange():
            return self._answer(audio)

    def _answer(self, audio: bytes) -> dict:
        try:
            self.state.set_state("thinking")
            transcript = self.transcriber.transcribe(audio)
            if not transcript:
                return {"transcript": "", "text": ""}

            action = self.answerer(transcript, target_body_id=self.body_id)
            return {"transcript": transcript, "text": action.params["text"]}
        finally:
            # Back to observing either way. The page takes over from here
            # and shows `speaking` for as long as the utterance lasts.
            self.state.set_state("observing")
