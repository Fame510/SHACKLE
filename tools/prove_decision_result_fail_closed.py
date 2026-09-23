"""Demonstrate the gap nutstrut flagged: fail-closed is proven only when
decide() RAISES. When decide() RETURNS a malformed or unknown decision
result, the enforcement layer in shackle/core.py does not deny.

Run against UNPATCHED master to see the holes, and again after the patch.
"""
import os
import sys

# Runnable from anywhere: put the repo root on sys.path so this works as
# `python tools/prove_decision_result_fail_closed.py` from a fresh clone.
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
)

import shackle.conformance as conf  # noqa: E402
import shackle.core as core
from shackle.core import TriggerEngine, ExecutionState, ShackleInterrupt

# Every one of these is a decision result that is NOT ("ALLOW", <reason>).
# A fail-closed kernel must treat each as DENY.
BAD_RESULTS = [
    ("None", None),
    ("empty tuple", ()),
    ("1-tuple", ("DENY",)),
    ("3-tuple", ("DENY", "circuit_open", "extra")),
    ("bare string OK", "OK"),
    ("unknown verdict PERMIT", ("PERMIT", "looks_fine")),
    ("lowercase allow", ("allow", "within_thresholds")),
    ("ALLOW with whitespace", (" ALLOW ", "within_thresholds")),
    ("empty verdict", ("", "")),
    ("non-str verdict True", (True, "within_thresholds")),
    ("int verdict", (1, "ok")),
    ("dict result", {"verdict": "DENY", "reason": "circuit_open"}),
    ("list result", ["DENY", "circuit_open"]),
    ("None verdict", (None, None)),
]


def probe(label, fake):
    """Return 'DENIED' / 'ALLOWED (FAIL-OPEN)' / 'CRASH:<exc>' per path."""
    orig = conf.decide
    core._sp_decide = lambda *a, **k: fake
    out = {}
    try:
        eng = TriggerEngine(budget=10.0, max_repeat_calls=3)
        st = ExecutionState()
        try:
            eng.evaluate_tool_call("Agent", "send_email", {"to": "x@y.z"}, st)
            out["tool"] = "ALLOWED (FAIL-OPEN)"
        except ShackleInterrupt as si:
            out["tool"] = f"DENIED ({si.trigger_type})"
        except Exception as e:
            out["tool"] = f"CRASH:{type(e).__name__}"

        eng2 = TriggerEngine(budget=10.0)
        st2 = ExecutionState()
        try:
            eng2.evaluate_llm_call("gpt-4o", 1000, 500, st2)
            out["llm"] = "ALLOWED (FAIL-OPEN)"
        except ShackleInterrupt as si:
            out["llm"] = f"DENIED ({si.trigger_type})"
        except Exception as e:
            out["llm"] = f"CRASH:{type(e).__name__}"
    finally:
        core._sp_decide = orig
    return out


def main():
    print(f"{'malformed / unknown decision result':38} {'tool path':26} llm path")
    print("-" * 96)
    open_holes = 0
    crashes = 0
    for label, fake in BAD_RESULTS:
        r = probe(label, fake)
        if "FAIL-OPEN" in r["tool"] or "FAIL-OPEN" in r["llm"]:
            open_holes += 1
        if r["tool"].startswith("CRASH") or r["llm"].startswith("CRASH"):
            crashes += 1
        print(f"{label:38} {r['tool']:26} {r['llm']}")
    print("-" * 96)
    print(f"fail-open cases: {open_holes}   uncontrolled-crash cases: {crashes}"
          f"   total probed: {len(BAD_RESULTS)}")
    return 1 if (open_holes or crashes) else 0


if __name__ == "__main__":
    sys.exit(main())
