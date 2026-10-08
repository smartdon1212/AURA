"""
EventBus: thin wrapper around Redis Streams.

Design rule: nothing outside this file talks to Redis directly.
Publishers and subscribers only ever see typed Pydantic models
(Percept, Action) going in and out. This is what lets Memory and Mind
both subscribe to `percepts` independently without knowing about each
other, and what lets a Body only ever see the `actions:{body_id}`
stream meant for it.

Consumer groups (not plain pub/sub) are used deliberately: if Memory is
down for a few seconds, Percepts queue up in Redis instead of being lost,
and Memory picks up where it left off on restart.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TypeVar

import redis
from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)

logger = logging.getLogger(__name__)

PERCEPTS_STREAM = "percepts"


def actions_stream(body_id: str) -> str:
    """Each Body gets its own Action stream — a Body only ever sees intents meant for it."""
    return f"actions:{body_id}"


class EventBus:
    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0) -> None:
        self._redis = redis.Redis(
            host=host, port=port, db=db, decode_responses=True, socket_timeout=30
        )

    def publish(self, stream: str, model: BaseModel) -> str:
        """Publishes a Pydantic model to a stream. Returns the Redis entry ID."""
        payload = {"data": model.model_dump_json()}
        return self._redis.xadd(stream, payload)

    def ensure_group(self, stream: str, group: str) -> None:
        """Creates a consumer group if it doesn't already exist. Safe to call repeatedly."""
        try:
            self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        model_cls: type[ModelT],
        block_ms: int = 5000,
        count: int = 10,
    ) -> Iterator[tuple[str, ModelT]]:
        """
        Blocks for new entries on `stream` under `group`/`consumer`, yields
        (entry_id, parsed_model) pairs. Caller must call `ack` once an entry
        is fully processed — unacked entries are redelivered on restart.
        """
        self.ensure_group(stream, group)
        while True:
            try:
                response = self._redis.xreadgroup(
                    group, consumer, {stream: ">"}, count=count, block=block_ms
                )
            except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError):
                # A stray timeout on an idle stream is expected, not fatal —
                # just loop and try again rather than killing a long-running consumer.
                continue
            if not response:
                continue
            for _stream_name, entries in response:
                for entry_id, fields in entries:
                    model = self._parse(
                        model_cls, fields, entry_id, stream, group, consumer
                    )
                    if model is None:
                        # Unparseable. Acked inside _parse — see the note
                        # there on why the entry is dropped rather than left
                        # in the PEL.
                        continue
                    yield entry_id, model

    def _parse(
        self,
        model_cls: type[ModelT],
        fields: dict,
        entry_id: str,
        stream: str,
        group: str,
        consumer: str,
    ) -> ModelT | None:
        """
        Parses one entry, returning None if it cannot be parsed — instead of
        raising.

        G-8: this parse used to be unguarded, so a single malformed message
        propagated out of the generator, out of the consumer's `for` loop,
        and killed a long-running service. One bad entry permanently stopped
        memory writes (or reasoning) until someone restarted AURA.

        The catch is deliberately narrow. `ValidationError` is schema drift
        between producer and consumer; `KeyError` is a missing `data` field;
        `TypeError` is a payload that isn't the shape the model expects.
        A genuine bug in the consumer still raises and still gets noticed —
        this is not a blanket `except Exception`.

        The bad entry is **XACKed**, not left unacked. An entry that cannot
        be parsed can never become parseable, so redelivering it is an
        infinite crash loop rather than a recovery; leaving it in the PEL
        would also mean step 17's PEL drain re-delivers it on every restart.
        The warning is the record of what was dropped.
        """
        try:
            payload = fields["data"]
        except (KeyError, TypeError):
            logger.warning(
                "Dropping malformed entry %s on stream '%s' (group '%s', "
                "consumer '%s'): no 'data' field. Fields present: %s",
                entry_id,
                stream,
                group,
                consumer,
                sorted(fields) if hasattr(fields, "keys") else type(fields).__name__,
            )
            self.ack(stream, group, entry_id)
            return None

        try:
            return model_cls.model_validate_json(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "Dropping malformed entry %s on stream '%s' (group '%s', "
                "consumer '%s'): %s does not validate as %s — %s",
                entry_id,
                stream,
                group,
                consumer,
                "payload",
                model_cls.__name__,
                exc,
            )
            self.ack(stream, group, entry_id)
            return None

    def ack(self, stream: str, group: str, entry_id: str) -> None:
        self._redis.xack(stream, group, entry_id)
