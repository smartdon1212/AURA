AURA should solve **trustworthy continuity**: maintaining an accurate, user-controlled understanding of a person, their environment, goals, and activities over months or years—while knowing when to remember, forget, speak, act, or abstain.

## 1. Long-term memory that stays correct

AURA should not merely store camera events or conversation logs. It should maintain different kinds of memory:

- What happened.
- What the user explicitly said.
- What the user prefers.
- What the user is trying to achieve.
- What commitments remain unfinished.
- What routines are normal.
- What changed over time.

The difficult part is keeping memory current. AURA must revise outdated facts, resolve contradictions, track when something happened, and avoid treating an old assumption as present truth. Current long-term-memory benchmarks test information extraction, multi-session reasoning, temporal reasoning, knowledge updates, and abstention; newer benchmarks specifically evaluate conversations spanning weeks or months and penalize reliance on obsolete memories. [aclanthology](https://aclanthology.org/2026.findings-acl.1337.pdf)

**AURA’s target:** The user should be able to say something once, correct it later, and trust AURA to use the newest valid version.

## 2. Selective remembering and intentional forgetting

AURA should solve the problem of deciding **what deserves memory**.

It should not permanently retain every visual observation. Nor should it forget important commitments because they were mentioned months ago. It needs policies for:

- What to store.
- What to summarize.
- What to discard.
- What should expire.
- What requires user confirmation.
- What is too sensitive to retain automatically.

The user should be able to inspect, correct, export, and delete memories. Deletion must remove the memory from indexes, summaries, embeddings, and derived inferences—not only from the visible database row.

**AURA’s target:** Memory becomes a user-owned asset, not an invisible vendor feature.

## 3. Understanding the user’s goals and current state

AURA should maintain a structured model of the user’s evolving state:

- Active projects.
- Long-term goals.
- Current tasks.
- Deadlines.
- Constraints.
- Preferences.
- Repeated obstacles.
- Pending decisions.
- Changes in priorities.

This is more important than remembering isolated facts. A Personal OS should understand why a fact matters now.

For example, AURA should know that the user is not merely “building a Python project,” but is balancing AURA with university, a separate SaaS product, limited API funds, and a long-term robotics ambition.

**AURA’s target:** It should preserve the user’s intent across interruptions, not force the user to restate the project every session.

## 4. Attention management without constant interruption

AURA’s ambient design creates a central problem: **when should it speak?**

It should distinguish among:

- Something worth silently recording.
- Something worth mentioning later.
- Something worth asking about now.
- Something requiring immediate attention.
- Something it should ignore.

The decision should depend on urgency, risk, relevance to the user’s goals, current activity, time, social setting, previous feedback, and confidence.

AURA should learn that a user prefers no interruptions while studying, but may welcome a safety warning or a reminder about a critical deadline.

**AURA’s target:** It should protect the user’s attention rather than compete for it.

## 5. Temporal and activity understanding

AURA should move beyond isolated object detection. It needs to understand **what changed and what is happening over time**:

- A person entered the room.
- A work session began.
- The user has been inactive for an unusual period.
- A tool was moved from its normal location.
- A routine was interrupted.
- The same problem has occurred repeatedly.
- An activity started but was never completed.

This requires temporal models and event consolidation, not just a list of timestamped YOLO detections.

**AURA’s target:** It should understand situations and patterns, not merely identify objects in individual frames.

## 6. Honest uncertainty and provenance

AURA should never silently convert a weak perception into a confident fact.

Every memory or conclusion should indicate:

- Whether it was observed, inferred, or told by the user.
- When it was created.
- What evidence supports it.
- How confident AURA is.
- Whether the user confirmed it.
- Whether it may have become outdated.

For example:

| Level | AURA should say |
|---|---|
| Observation | “A knife-like object was detected.” |
| Interpretation | “You may be preparing food.” |
| Uncertainty | “I am not confident enough to identify the activity.” |
| Confirmed fact | “You told me you are preparing dinner.” |

**AURA’s target:** It should prefer “I’m not sure” over a confident hallucination, especially for safety, health, identity, or household events.

## 7. Privacy-preserving ambient perception

AURA’s greatest advantage—continuous vision and audio—is also its greatest risk. Local processing helps, but local data can still expose intimate routines, identities, health conditions, relationships, and household behavior.

AURA should solve:

- How to process raw media locally.
- How to avoid storing unnecessary video or audio.
- How to handle guests and multiple household members.
- How to create private zones.
- How to restrict sensitive inferences.
- How to show when sensors are active.
- How to control cloud transmission.
- How to delete derived inferences.
- How to operate in offline mode.

Smart-home research continues to identify privacy, cybersecurity, interoperability, data ownership, and explainability as unresolved problems. [link.springer](https://link.springer.com/article/10.1007/s44163-026-02157-4?error=cookies_not_supported&code=6939939c-3848-4ca4-8be9-504b8490b545)

**AURA’s target:** It should provide useful ambient intelligence without becoming a private surveillance system.

## 8. Reliable long-horizon action

AURA should eventually do more than answer questions. It should help execute tasks over time.

That requires it to:

- Break goals into steps.
- Track progress.
- Remember what has already been attempted.
- Detect when a plan is blocked.
- Ask for clarification at the right point.
- Recover from failure.
- Re-plan when the environment changes.
- Cancel actions safely.
- Report what it did and did not complete.

Current agents often perform individual steps well but remain unreliable across long workflows and changing contexts. Long-horizon reliability is still an active research problem. [arxiv](https://arxiv.org/html/2602.16666v2)

**AURA’s target:** It should be dependable over multi-step and multi-day tasks, not merely impressive in a single conversation.

## 9. Safe separation between thinking and acting

The Mind/Body architecture should ensure that reasoning does not directly trigger unsafe actions.

Every Action should include:

- Intended goal.
- Target.
- Required capability.
- Preconditions.
- Confidence.
- Risk level.
- Whether confirmation is required.
- Whether the action is reversible.
- Timeout.
- Verification plan.
- Recovery behavior.

This matters especially when AURA moves from speech to physical robotics. A spoken recommendation and a robot movement cannot have the same safety policy.

**AURA’s target:** The system should be able to reason flexibly while acting conservatively.

## 10. Embodiment portability that is actually demonstrated

AURA’s current contract-first architecture is promising, but portability is presently a design claim because only the desktop Body has been implemented.

AURA should prove portability with at least:

- The existing desktop Body.
- A simulated robot Body.
- Another interface, such as a dashboard or mobile Body.

The same high-level intention should be expressible across different embodiments, while each Body handles its own capabilities, timing, safety limits, and execution details. Embodied-AI research still identifies long-term memory, complex-intention understanding, task decomposition, and sim-to-real transfer as major limitations. [arxiv](https://arxiv.org/html/2407.06886v8)

**AURA’s target:** The Mind should express goals and constraints, not device-specific commands.

## 11. Offline and low-resource resilience

Because AURA targets consumer hardware and free-tier cloud models, it should remain useful when:

- The internet is unavailable.
- API quotas are exhausted.
- The cloud model changes.
- The machine is under CPU load.
- The camera is unavailable.
- A service crashes.
- PostgreSQL or Redis restarts.

The system should degrade gracefully:

- Perception continues locally.
- Memory remains available offline.
- Simple rules work without an LLM.
- Nonurgent reasoning is queued.
- Sensitive information does not automatically leave the device.
- Failed services restart safely.

**AURA’s target:** It should be a dependable local system with optional cloud intelligence—not a cloud chatbot attached to a webcam.

## 12. Recovery, observability, and self-monitoring

AURA needs to know when its own pipeline is failing.

It should monitor:

- Service health.
- Event delays.
- Dropped or duplicated events.
- Model timeouts.
- Audio failures.
- Memory-write failures.
- Stale perceptions.
- Contradictory outputs.
- Repeated user corrections.
- Action failures.

It should expose a clear system status: what is running, what is unavailable, and what information may be incomplete.

**AURA’s target:** It should fail visibly and safely rather than producing plausible output from a broken pipeline.

# Highest-priority sequence

AURA should address the problems in this order:

1. **User-controlled long-term memory.**
2. **Temporal state and goal tracking.**
3. **Uncertainty and provenance.**
4. **Attention and interruption management.**
5. **Privacy-preserving sensing and deletion.**
6. **Reliability, recovery, and offline operation.**
7. **Household activity and situation understanding.**
8. **Capability-aware actions.**
9. **A second Body for portability validation.**
10. **Robotics and advanced coaching.**

## The central problem statement

AURA’s most important research problem should be:

> How can a low-cost, privacy-preserving, ambient cognitive system maintain an accurate and user-controlled model of one person and their environment over months and years, while deciding when to remember, forget, speak, act, and abstain?

If AURA solves that, it will be more than a vision assistant. It will have a credible foundation for a **personal cognitive operating system** and, later, an embodiment-portable Physical AI system.