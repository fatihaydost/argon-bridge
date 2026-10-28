"""Output forms Argon must answer in. Limits are also enforced locally by gateway.clip."""

_text = lambda n: {"type": "string", "maxLength": n}  # noqa: E731


def findings(n):
    """The findings form with room for `n` findings."""
    return {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "maxItems": n,
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": _text(300),
                        "file": _text(300),
                        "line": {"type": "integer"},
                        "evidence": _text(400),
                        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                        "kind": {
                            "type": "string",
                            "enum": ["intent_gap", "incomplete_fix", "same_pattern_elsewhere", "bug", "plan_gap"],
                        },
                    },
                    "required": ["claim", "file", "line", "evidence", "severity", "kind"],
                },
            }
        },
        "required": ["findings"],
    }


FINDINGS = findings(8)  # a code or plan review
ASK_FINDINGS = findings(15)  # `ask --findings`: a many-point assessment (an audit report, a UI review)

OPINION = {
    "type": "object",
    "properties": {
        "position": _text(600),
        "reasons": {"type": "array", "maxItems": 3, "items": _text(300)},
        "risks": {"type": "array", "maxItems": 3, "items": _text(300)},
    },
    "required": ["position", "reasons", "risks"],
}

PHASE_REPORT = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "question", "blocked"]},
        "summary": _text(1500),
        "changes": {"type": "array", "maxItems": 40, "items": _text(300)},
        "commands_run": {"type": "array", "maxItems": 40, "items": _text(300)},
        "failures": {"type": "array", "maxItems": 20, "items": _text(500)},
        "question": _text(1500),
        "subagents": _text(1500),
    },
    "required": ["status", "summary", "changes", "commands_run", "failures"],
}
