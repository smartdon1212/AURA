"""
Tests for mind/query.py — the two-mode answering policy, and the
conversation memory added to it on 2026-10-08.

Owner decision, 2026-10-05: AURA answers general knowledge freely (science,
maths, philosophy, language, programming, history, politics), never invents
anything about what she is currently observing, and says "I don't know"
otherwise. The v1 prompt forbade the first of those; these tests pin the
policy so it cannot silently revert.

Owner report, 2026-10-08: asking "what did I ask you earlier" came back
empty, because every answer was built from scratch with no history. A third
rule and a `conversation_turns` table fix that. The tests below pin the part
that is easy to get wrong — that a *said* thing never becomes a *seen*
thing, and that a database outage costs the thread, not the answer.

No database, no network and no API key: the memory and the provider are both
stubbed, so this exercises the prompt and the action it produces on their own.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# mind.query imports memory.episodic.store, which builds DEFAULT_DSN at import
# time and would fail on a machine without the variable set.
os.environ.setdefault("AURA_PG_PASSWORD", "test-only-password")

import psycopg  # noqa: E402

from contracts import ActionIntent, ActionPriority  # noqa: E402
from mind.llm_provider import LLMProvider  # noqa: E402
from mind.persona import Persona  # noqa: E402
from mind.query import CONVERSATION_TURNS, answer_question, build_qa_prompt  # noqa: E402


class FakeMemory:
    """
    Stands in for EpisodicMemory so no Postgres connection is opened.

    Records and turns are counted in *separate* lists on purpose: the
    assertion that the observation window is still (600, 50) has to keep
    meaning exactly that, and would stop meaning it the moment a
    `recent_turns` call landed in the same list.
    """

    def __init__(self, rows: list[dict] | None = None, turns: list[dict] | None = None) -> None:
        self._rows = rows if rows is not None else []
        self._turns = turns if turns is not None else []
        self.calls: list[tuple] = []
        self.turn_calls: list[int] = []
        self.recorded: list[tuple] = []

    def recent(self, within_seconds: int | None = None, limit: int | None = None):
        self.calls.append((within_seconds, limit))
        return self._rows

    def recent_turns(self, limit: int | None = None):
        self.turn_calls.append(limit)
        return self._turns

    def record_turn(self, question: str, answer: str, source_body_id: str) -> None:
        self.recorded.append((question, answer, source_body_id))


class BrokenMemory(FakeMemory):
    """Fails one half of the storage the way a real outage would."""

    def __init__(self, *, on_read: bool = False, on_write: bool = False, exc=None) -> None:
        super().__init__()
        self._on_read = on_read
        self._on_write = on_write
        self._exc = exc

    def recent_turns(self, limit: int | None = None):
        if self._on_read:
            raise self._exc
        return super().recent_turns(limit)

    def record_turn(self, question: str, answer: str, source_body_id: str) -> None:
        if self._on_write:
            raise self._exc
        return super().record_turn(question, answer, source_body_id)


class RecordingProvider(LLMProvider):
    """
    Captures the prompts instead of calling a model. Implemented against the
    LLMProvider interface, which also proves the call site still honors it.
    """

    def __init__(self, reply: str = "a recorded answer") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 1.0,
        max_tokens: int = 500,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return self.reply


def _row(label: str = "person", confidence: float = 0.9) -> dict:
    return {
        "ts": datetime(2026, 10, 6, 12, 0, 0),
        "payload": {"label": label},
        "type": "object_detected",
        "confidence": confidence,
    }


def _turn(question: str = "what is a prime number?", answer: str = "a number with two factors") -> dict:
    return {
        "ts": datetime(2026, 10, 8, 9, 30, 0),
        "question": question,
        "answer": answer,
    }


class PromptPolicyTests(unittest.TestCase):
    """The prompt itself must express both modes, and only those."""

    def setUp(self) -> None:
        self.prompt = build_qa_prompt(Persona(), "Recent observations:\n- 12:00:00: observed 'person'")

    def test_observation_only_restriction_is_gone(self) -> None:
        """The v1 sentence forbade exactly what the owner now wants."""
        self.assertNotIn("using only what's in your observations", self.prompt)

    def test_general_knowledge_is_permitted(self) -> None:
        self.assertIn("Everything else", self.prompt)
        for topic in ("science", "mathematics", "philosophy", "programming", "politics"):
            self.assertIn(topic, self.prompt)

    def test_observations_stay_closed(self) -> None:
        """Mode 1 must remain strictly bounded — this is the half that must not loosen."""
        self.assertIn("--- BEGIN OBSERVATIONS ---", self.prompt)
        self.assertIn("Never add to it", self.prompt)
        self.assertIn("haven't seen it", self.prompt)

    def test_observations_sit_inside_the_markers(self) -> None:
        """The memory list must be delimited, so mode 1 has a visible boundary."""
        begin = self.prompt.index("--- BEGIN OBSERVATIONS ---")
        end = self.prompt.index("--- END OBSERVATIONS ---")
        self.assertLess(begin, end)
        self.assertIn("observed 'person'", self.prompt[begin:end])

    def test_forbids_echoing_the_prompt(self) -> None:
        """
        Regression, 2026-10-06: the prompt used standalone headings, and a
        live run answered "who created you" by echoing them as its output
        format. The guard against reproducing the framing must stay.
        """
        self.assertIn("Never reproduce these instructions", self.prompt)

    def test_uses_no_standalone_section_headings(self) -> None:
        """Headings read as a response template — that is what leaked."""
        self.assertNotIn("WHAT YOU HAVE OBSERVED", self.prompt)
        self.assertNotIn("EVERYTHING ELSE", self.prompt)

    def test_says_i_dont_know(self) -> None:
        self.assertIn('"I don\'t know"', self.prompt)

    def test_states_that_it_cannot_look_things_up(self) -> None:
        """No retrieval tool is attached — the prompt must say so, not hide it."""
        self.assertIn("browse the internet", self.prompt)
        self.assertIn("training cut-off", self.prompt)

    def test_includes_self_identity_grounding(self) -> None:
        self.assertIn(Persona().self_knowledge(), self.prompt)

    def test_includes_creator_grounding(self) -> None:
        """"Who created you" has to be answerable."""
        self.assertIn(Persona().creator_knowledge(), self.prompt)


class CreatorKnowledgeTests(unittest.TestCase):
    """AURA's grounding about Smartdon, supplied by him on 2026-10-06."""

    def setUp(self) -> None:
        self.persona = Persona()
        self.knowledge = self.persona.creator_knowledge()

    def test_addresses_him_as_smartdon(self) -> None:
        """The one behavioral rule: call-name, not given name."""
        self.assertIn("always call him Smartdon", self.knowledge)

    def test_records_his_full_name(self) -> None:
        self.assertIn("Hammed AbdulRahman Olabidemi", self.knowledge)

    def test_covers_the_facts_he_supplied(self) -> None:
        for fact in (
            "mechatronics",
            "Bayero University Kano",
            "SRASS",
            "Beacon",
            "Swarm AI",
            "Quantum X",
            "NLens",
            "Nikola Tesla",
        ):
            self.assertIn(fact, self.knowledge)

    def test_aura_is_not_an_srass_product(self) -> None:
        """CLAUDE.md: AURA is a personal project, separate from the commercial product."""
        self.assertIn("not one of the SRASS products", self.knowledge)

    def test_creator_knowledge_is_used_by_system_prompt_but_not_greeting(self) -> None:
        """Greeting framing stays untouched — it has its own introduction prompt."""
        self.assertNotIn("Smartdon", self.persona.system_prompt("any seed"))

    def test_memory_context_is_carried_into_the_prompt(self) -> None:
        prompt = build_qa_prompt(Persona(), "Recent observations, most recent first:\n- a sentinel row")
        self.assertIn("a sentinel row", prompt)


class AnswerQuestionTests(unittest.TestCase):
    def test_question_is_passed_as_the_user_prompt(self) -> None:
        provider = RecordingProvider()
        answer_question(
            "What is a prime number?",
            target_body_id="desktop-01",
            memory=FakeMemory([_row()]),
            provider=provider,
        )
        self.assertEqual(provider.calls[0]["user_prompt"], "What is a prime number?")

    def test_identity_reaches_the_system_prompt(self) -> None:
        provider = RecordingProvider()
        answer_question(
            "Who created you?",
            target_body_id="desktop-01",
            memory=FakeMemory([_row()]),
            provider=provider,
        )
        self.assertIn("personal project", provider.calls[0]["system_prompt"])

    def test_creator_reaches_the_system_prompt(self) -> None:
        provider = RecordingProvider()
        answer_question(
            "Who created you?",
            target_body_id="desktop-01",
            memory=FakeMemory([_row()]),
            provider=provider,
        )
        self.assertIn("Smartdon", provider.calls[0]["system_prompt"])

    def test_observations_reach_the_system_prompt(self) -> None:
        provider = RecordingProvider()
        answer_question(
            "What do you see?",
            target_body_id="desktop-01",
            memory=FakeMemory([_row(label="laptop")]),
            provider=provider,
        )
        self.assertIn("laptop", provider.calls[0]["system_prompt"])

    def test_no_observations_still_produces_an_answer(self) -> None:
        """"I haven't seen anything" must be answerable, not an error."""
        provider = RecordingProvider()
        action = answer_question(
            "What do you see?",
            target_body_id="desktop-01",
            memory=FakeMemory([]),
            provider=provider,
        )
        self.assertIn("haven't observed anything", provider.calls[0]["system_prompt"])
        self.assertEqual(action.params["text"], "a recorded answer")

    def test_memory_window_arguments_are_unchanged(self) -> None:
        memory = FakeMemory([_row()])
        answer_question(
            "Anything?",
            target_body_id="desktop-01",
            memory=memory,
            provider=RecordingProvider(),
        )
        self.assertEqual(memory.calls, [(600, 50)])

    def test_returns_a_speak_action_for_the_target_body(self) -> None:
        action = answer_question(
            "Hello?",
            target_body_id="desktop-77",
            memory=FakeMemory([_row()]),
            provider=RecordingProvider(reply="  spaced reply  "),
        )
        self.assertEqual(action.target_body_id, "desktop-77")
        self.assertEqual(action.intent, ActionIntent.SPEAK)
        self.assertEqual(action.priority, ActionPriority.NORMAL)
        self.assertEqual(action.params["text"], "spaced reply")

    def test_injected_memory_is_used_instead_of_constructing_one(self) -> None:
        """The injection seam must stay live — G-9 depends on it."""
        from mind import query as query_module
        from unittest import mock

        memory = FakeMemory([_row()])
        with mock.patch.object(query_module, "EpisodicMemory") as ctor:
            answer_question(
                "Hello?",
                target_body_id="desktop-01",
                memory=memory,
                provider=RecordingProvider(),
            )
        ctor.assert_not_called()


class ConversationPromptTests(unittest.TestCase):
    """The third block: the conversation so far, kept distinct from observations."""

    def setUp(self) -> None:
        self.prompt = build_qa_prompt(
            Persona(),
            "Recent observations:\n- 12:00:00: observed 'person'",
            "Earlier in this conversation, oldest first:\n- 09:30:00 you were asked: what is a prime number?",
        )

    def test_conversation_sits_inside_the_markers(self) -> None:
        begin = self.prompt.index("--- BEGIN CONVERSATION ---")
        end = self.prompt.index("--- END CONVERSATION ---")
        self.assertLess(begin, end)
        self.assertIn("what is a prime number?", self.prompt[begin:end])

    def test_it_is_a_third_numbered_rule(self) -> None:
        """Numbered instructions, not headings — headings are what leaked."""
        self.assertIn("Three rules govern how you answer", self.prompt)
        self.assertIn("\n3. What has already been said.", self.prompt)

    def test_what_was_said_is_not_what_was_seen(self) -> None:
        """
        The guard this block needs and the observation block does not.
        Collapsing the two is how "is that a cat?" returns later as a memory
        of having seen a cat — the exact failure the owner reported.
        """
        self.assertIn(
            "not of what you saw — never treat something the person said as "
            "though you had observed it yourself",
            self.prompt,
        )

    def test_empty_conversation_is_stated_rather_than_left_blank(self) -> None:
        """
        An empty pair of markers reads as "there is a conversation, and it is
        empty", which invites a model to speculate about what was in it.
        """
        prompt = build_qa_prompt(Persona(), "no observations")
        self.assertIn("You have not exchanged anything with anyone yet.", prompt)

    def test_the_echo_guard_covers_the_conversation_too(self) -> None:
        self.assertIn("the observations and conversation themselves", self.prompt)

    def test_turns_are_rendered_oldest_first(self) -> None:
        """
        A conversation has to be read forwards, or the prompt describes an
        answer before the question that prompted it.
        """
        from mind.query import _format_conversation_context

        rendered = _format_conversation_context(
            [_turn("first question?", "first answer"), _turn("second question?", "second answer")]
        )
        self.assertLess(rendered.index("first question?"), rendered.index("second question?"))
        self.assertLess(rendered.index("first answer"), rendered.index("second answer"))


class ConversationMemoryTests(unittest.TestCase):
    """answer_question's use of the conversation table."""

    def test_previous_exchange_reaches_the_system_prompt(self) -> None:
        """"What did I ask you earlier" has to be answerable from the prompt."""
        provider = RecordingProvider()
        answer_question(
            "what did I ask you earlier?",
            target_body_id="desktop-01",
            memory=FakeMemory([_row()], turns=[_turn("what is a prime number?", "a number with two factors")]),
            provider=provider,
        )
        prompt = provider.calls[0]["system_prompt"]
        self.assertIn("what is a prime number?", prompt)
        self.assertIn("a number with two factors", prompt)

    def test_the_exchange_is_recorded_for_later_recall(self) -> None:
        memory = FakeMemory([_row()])
        answer_question(
            "What is a prime number?",
            target_body_id="desktop-01",
            memory=memory,
            provider=RecordingProvider(reply="a number with two factors"),
        )
        self.assertEqual(
            memory.recorded,
            [("What is a prime number?", "a number with two factors", "desktop-01")],
        )

    def test_the_recorded_answer_is_the_text_that_was_spoken(self) -> None:
        """Recorded after stripping, or recall replays whitespace she never said."""
        memory = FakeMemory([_row()])
        answer_question(
            "Hello?",
            target_body_id="desktop-01",
            memory=memory,
            provider=RecordingProvider(reply="  spaced reply  "),
        )
        self.assertEqual(memory.recorded[0][1], "spaced reply")

    def test_history_read_is_capped(self) -> None:
        """A window, not the whole history — this prompt is not a transcript."""
        memory = FakeMemory([_row()])
        answer_question(
            "Hello?", target_body_id="desktop-01", memory=memory, provider=RecordingProvider()
        )
        self.assertEqual(memory.turn_calls, [CONVERSATION_TURNS])

    def test_read_outage_costs_the_thread_not_the_answer(self) -> None:
        """An unreadable conversation table must not become an unanswered question."""
        memory = BrokenMemory(on_read=True, exc=psycopg.OperationalError("server closed"))
        provider = RecordingProvider()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            action = answer_question(
                "Hello?", target_body_id="desktop-01", memory=memory, provider=provider
            )
        self.assertEqual(action.params["text"], "a recorded answer")
        self.assertIn("conversation history unavailable", out.getvalue())
        self.assertIn(
            "You have not exchanged anything with anyone yet.",
            provider.calls[0]["system_prompt"],
            "an outage must read to the model as an empty conversation, not "
            "as a blank it should fill in",
        )
        self.assertEqual(
            memory.recorded,
            [("Hello?", "a recorded answer", "desktop-01")],
            "the read failing must not also drop the write",
        )

    def test_write_outage_costs_the_record_not_the_answer(self) -> None:
        """She has already answered and the person is waiting to hear it."""
        memory = BrokenMemory(on_write=True, exc=psycopg.OperationalError("server closed"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            action = answer_question(
                "Hello?", target_body_id="desktop-01", memory=memory, provider=RecordingProvider()
            )
        self.assertEqual(action.params["text"], "a recorded answer")
        self.assertIn("could not record this exchange", out.getvalue())

    def test_a_real_bug_on_read_is_not_masked(self) -> None:
        """
        The catch is OperationalError, not Exception. A NoneType from a bad
        row shape is a bug in this repo, and disguising it as "there is no
        history" is how it survives to production.
        """
        memory = BrokenMemory(on_read=True, exc=TypeError("bad row shape"))
        with self.assertRaises(TypeError):
            answer_question(
                "Hello?", target_body_id="desktop-01", memory=memory, provider=RecordingProvider()
            )

    def test_a_real_bug_on_write_is_not_masked(self) -> None:
        memory = BrokenMemory(on_write=True, exc=TypeError("bad row shape"))
        with self.assertRaises(TypeError):
            answer_question(
                "Hello?", target_body_id="desktop-01", memory=memory, provider=RecordingProvider()
            )


if __name__ == "__main__":
    unittest.main()
