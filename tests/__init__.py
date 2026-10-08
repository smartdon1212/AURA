"""
Test harness for AURA.

Stdlib `unittest` only — deliberately no new dependency (pytest, etc.),
per the standing constraint against unnecessary additions. Run from the
repository root with:

    python -m unittest discover -s tests

`python -m` puts the repository root on sys.path, which is what lets the
test modules import application packages (`memory.episodic.store`, ...)
the same way the entry points do.

Every test here uses stubs. Nothing in this suite should require a live
Redis, Postgres, Gemini key, or camera — see step 20 of the remediation
plan in docs/PROJECT_STATE.md.
"""
