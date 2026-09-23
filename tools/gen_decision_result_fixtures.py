"""Generate fixtures/decision-result-conformance.json.

The SP/1.0 vectors in fixtures/conformance.json pin the INPUT side of the
decision surface: (config, state, call) -> (verdict, reason). They say nothing
about what an enforcement layer must do when the decision RESULT it receives is
itself out of contract, which is the gap this fixture family closes.

Each vector encodes a raw decision result in a language-neutral envelope, so a
non-Python implementation can reproduce the same table.

Envelope kinds:
  {"kind": "null"}                                  -> language's nil
  {"kind": "string",  "value": "..."}               -> text
  {"kind": "bytes",   "value": "..."}               -> UTF-8 byte string
  {"kind": "bool",    "value": true}                -> boolean
  {"kind": "number",  "value": 1}                   -> number
  {"kind": "sequence","container": "tuple"|"list"|"set", "items": [<env>...]}
  {"kind": "mapping", "entries": {"k": <env>, ...}}
  {"kind": "lying_string", "value": "ALLOW"}        -> text-like object whose
        equality operator returns true for ANY comparison (hostile producer).
        Marked language_specific: implementations without overridable equality
        may skip it and must record the skip.

Usage:  python tools/gen_decision_result_fixtures.py
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
sys.path.insert(0, _ROOT)

from shackle.conformance import (  # noqa: E402
    SPEC_REVISION,
    normalize_decision,
    vector_hash,
)

OUT = os.path.join(_ROOT, "fixtures", "decision-result-conformance.json")


def s(v):
    return {"kind": "string", "value": v}


def pair(a, b, container="tuple"):
    return {"kind": "sequence", "container": container, "items": [a, b]}


# (name, raw envelope, note)
CASES = [
    ("result_missing", {"kind": "null"},
     "No decision at all. A guard with no decision has not authorized anything."),
    ("result_bare_string_two_chars", s("OK"),
     "A 2-character string unpacks element-wise into ('O','K') in languages "
     "that destructure strings; it is not a decision pair."),
    ("result_bare_string_allow", s("ALLOW"),
     "Verdict without a reason is not a pair; an unrecordable release is denied."),
    ("result_bytes_allow", {"kind": "bytes", "value": "ALLOW"},
     "Byte string is not the text enum member."),
    ("result_mapping", {"kind": "mapping", "entries": {
        "verdict": s("ALLOW"), "reason": s("within_thresholds")}},
     "Object/dict shape is out of contract. Not silently re-read as a pair: a "
     "producer that cannot return the contract shape is not trusted to ALLOW."),
    ("result_unordered_set", {"kind": "sequence", "container": "set",
                              "items": [s("ALLOW"), s("within_thresholds")]},
     "An unordered container has no first element, so it can never be read as "
     "(verdict, reason)."),
    ("result_empty_sequence", {"kind": "sequence", "container": "tuple", "items": []},
     "Arity 0."),
    ("result_one_element", {"kind": "sequence", "container": "tuple",
                            "items": [s("DENY")]},
     "Arity 1."),
    ("result_three_elements", {"kind": "sequence", "container": "tuple",
                               "items": [s("ALLOW"), s("within_thresholds"),
                                         s("extra")]},
     "Arity 3: an extra field may carry meaning this enforcer cannot read."),
    ("result_verdict_true", pair({"kind": "bool", "value": True}, s("within_thresholds")),
     "Truthy non-string verdict. The classic fail-open: `if verdict` is true."),
    ("result_verdict_number", pair({"kind": "number", "value": 1}, s("ok")),
     "Numeric verdict code is not the SP/1.0 enum."),
    ("result_verdict_null", pair({"kind": "null"}, {"kind": "null"}),
     "Null verdict."),
    ("result_verdict_nested_pair", pair(pair(s("ALLOW"), s("x")), s("y")),
     "Nested structure where a verdict was expected."),
    ("result_unknown_verdict", pair(s("PERMIT"), s("looks_fine")),
     "Unrecognized verdict from a newer or foreign producer: DENY, never a "
     "pass-through."),
    ("result_verdict_lowercase_allow", pair(s("allow"), s("within_thresholds")),
     "Case variant. NOT normalized to ALLOW: case-folding an out-of-contract "
     "producer's verdict would invent an authorization."),
    ("result_verdict_padded_allow", pair(s(" ALLOW "), s("within_thresholds")),
     "Whitespace variant. Not trimmed into a release, same reasoning."),
    ("result_verdict_empty_string", pair(s(""), s("")),
     "Empty verdict."),
    ("result_verdict_lowercase_deny", pair(s("deny"), s("circuit_open")),
     "Case-variant DENY is still refused execution: coercion is one-directional "
     "toward the restrictive outcome."),
    ("result_verdict_lowercase_hitl", pair(s("hitl"), s("needs_review")),
     "Case-variant HITL becomes DENY, which is strictly more restrictive than "
     "the HITL it appears to request. Never less."),
    ("result_allow_reason_null", pair(s("ALLOW"), {"kind": "null"}),
     "In-contract verdict, unusable reason. Release requires an auditable "
     "reason, so this denies."),
    ("result_allow_reason_empty", pair(s("ALLOW"), s("   ")),
     "Whitespace-only reason is not a reason."),
    ("result_allow_reason_number", pair(s("ALLOW"), {"kind": "number", "value": 7}),
     "Non-text reason."),
    ("result_allow_reason_control_chars", pair(s("ALLOW"), s("ok\ninjected=DENY")),
     "Control characters would let a producer forge or split audit-log lines."),
    ("result_deny_reason_missing", pair(s("DENY"), {"kind": "null"}),
     "DENY with an unusable reason keeps its verdict; only the label is "
     "repaired. Enforcement is never weakened by a bad reason."),
    ("result_hitl_reason_missing", pair(s("HITL"), {"kind": "null"}),
     "Same for HITL."),
    ("result_deny_reason_overlong", pair(s("DENY"), s("x" * 500)),
     "Overlong reason is truncated to 200 characters, verdict preserved."),
    ("result_verdict_lying_string", pair({"kind": "lying_string", "value": "ALLOW"},
                                         s("within_thresholds")),
     "Text-like object whose equality returns true for everything. An "
     "exact-type check refuses it; an equality check alone would release it."),
    # ---- the two in-contract results, pinned so strictness cannot regress ----
    ("result_valid_allow", pair(s("ALLOW"), s("within_thresholds")),
     "The only shape that releases a call."),
    ("result_valid_deny", pair(s("DENY"), s("circuit_open")),
     "In-contract DENY passes through unchanged."),
    ("result_valid_hitl", pair(s("HITL"), s("fail_closed:opaque_context")),
     "In-contract HITL passes through unchanged."),
    ("result_valid_allow_as_list", pair(s("ALLOW"), s("within_thresholds"),
                                        container="list"),
     "An ordered 2-element list is the same pair; JSON has no tuple type, so "
     "list and tuple must behave identically."),
]


class LyingStr(str):
    """A str subclass that compares equal to anything."""

    def __eq__(self, other):  # noqa: D105
        return True

    def __ne__(self, other):  # noqa: D105
        return False

    __hash__ = str.__hash__


def decode(env):
    kind = env["kind"]
    if kind == "null":
        return None
    if kind == "string":
        return env["value"]
    if kind == "bytes":
        return env["value"].encode("utf-8")
    if kind in ("bool", "number"):
        return env["value"]
    if kind == "lying_string":
        return LyingStr(env["value"])
    if kind == "mapping":
        return {k: decode(v) for k, v in env["entries"].items()}
    if kind == "sequence":
        items = [decode(i) for i in env["items"]]
        return {"tuple": tuple, "list": list, "set": set}[env["container"]](items)
    raise ValueError("unknown envelope kind: " + kind)


def main():
    fixtures = []
    for name, env, note in CASES:
        verdict, reason = normalize_decision(decode(env))
        fx = {
            "name": name,
            "raw": env,
            "expected_verdict": verdict,
            "expected_reason": reason,
            "conformance_note": note,
        }
        if env.get("kind") == "lying_string":
            fx["language_specific"] = True
        fx["vector_hash"] = vector_hash(fx)
        fixtures.append(fx)

    doc = {
        "spec": "SP/1.0",
        "revision": SPEC_REVISION,
        "license": "Apache-2.0",
        "attribution": "Dante Bullock (@Fame510), Sovereign Logic",
        "description": (
            "DECISION-RESULT vectors. Additive under the existing SP/1.0.1 "
            "revision — no new revision label. Separate from the 15 published "
            "SP/1.0 vectors in conformance.json (unchanged) and the SP/1.0.1 "
            "adversarial vectors in conformance-1.0.1.json (unchanged). These "
            "pin the CONSUMER side of the decision surface: what an "
            "enforcement layer must do when the decision result it receives is "
            "malformed, unrecognized, or otherwise out of contract. Earlier "
            "work proved fail-closed only when the decision function RAISED; "
            "these vectors cover the case where it RETURNS. Release requires "
            "an exact (\"ALLOW\", <reason>) pair; every other result is coerced "
            "to a verdict at least as restrictive as the one it carries."
        ),
        "canonicalization": {
            "hash": "sha256",
            "serialization": "json, keys sorted ascending, separators (',',':'), UTF-8",
            "vector_hash": "sha256 over the whole vector minus the vector_hash key",
        },
        "envelope": {
            "null": "language nil",
            "string": "text",
            "bytes": "UTF-8 byte string",
            "bool": "boolean",
            "number": "number",
            "sequence": "container tuple|list|set over items[]",
            "mapping": "object over entries{}",
            "lying_string": (
                "text-like object whose equality operator returns true for any "
                "comparison; skip and record the skip if the language cannot "
                "express it (language_specific: true)"
            ),
        },
        "fixtures": fixtures,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    allow = sum(1 for f in fixtures if f["expected_verdict"] == "ALLOW")
    print(f"wrote {len(fixtures)} vectors to {os.path.relpath(OUT, _ROOT)} "
          f"({allow} ALLOW, {len(fixtures) - allow} DENY/HITL)")


if __name__ == "__main__":
    main()
