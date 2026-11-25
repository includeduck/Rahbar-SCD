"""Prompt-injection rails (Assignment 03, D4; RAGOps paper 3.2.3 and 5.1, "injection attack
detection" under observability).

Two rails, each recording what it did in the query's trace:

  input rail      the user's question asks the system to drop its instructions. Flagged only:
                  the question is still answered from the documents, which is the right
                  outcome (golden q16 expects "100%" for "Ignore your instructions ... Is
                  attendance optional?"). The prompt's own rule and the citation check are the
                  defence; the flag makes the attempt visible and countable.
  retrieval rail  a retrieved passage carries an instruction aimed at the model (a poisoned
                  document, like Batch B's library guide). The passage is removed from the
                  prompt; it stays in the retrieved list, so evaluation still sees what
                  retrieval returned.

A pattern list catches the common phrasings, not every possible attack: paraphrased or
non-English instructions pass. The prompt rule (prompt.py, rule 5) remains the second layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..schemas import Passage

_PATTERNS = [
    # "ignore all previous instructions", "disregard your prompt", "forget the above rules";
    # not "ignore the attendance rules" (a student asking what happens if they do).
    ("ignore_instructions", r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(the\s+)?"
                            r"((previous|prior|above|earlier|preceding|your|my|these|those|"
                            r"system)\s+)?(instructions|prompts?|directions|directives)\b"
                            r"|\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(the\s+)?"
                            r"(previous|prior|above|earlier|preceding|your|system)\s+"
                            r"(rules|guidelines)\b"),
    ("role_override", r"\byou are (now|no longer)\b"),
    ("system_prompt", r"\b(system|hidden|secret) (prompt|instructions|message)\b"),
    ("addressed_to_ai", r"\b(note|notice|message) (to|for) (the )?(ai|automated|language model|"
                        r"llm|chat ?bot|assistant)s?\b"),
    ("conceal", r"\bdo not (mention|reveal|disclose) (this|these)\b"),
    ("exfiltrate", r"\b(reveal|print|echo|show|leak)\b[^.\n]{0,40}"
                   r"\b(source code|secrets?|api key|password|credentials|system prompt)\b"),
]
_RE = [(name, re.compile(pattern, re.I)) for name, pattern in _PATTERNS]


@dataclass
class Flag:
    rule: str
    match: str


def scan(text: str) -> list[Flag]:
    """Every injection pattern found in the text (empty when it looks clean)."""
    flags = []
    for name, pattern in _RE:
        found = pattern.search(text)
        if found:
            flags.append(Flag(rule=name, match=found.group(0)[:80]))
    return flags


def split_poisoned(passages: list[Passage]) -> tuple[list[Passage], list[tuple[Passage,
                                                                               list[Flag]]]]:
    """Return (clean passages, [(poisoned passage, its flags)])."""
    clean, poisoned = [], []
    for p in passages:
        flags = scan(p.text)
        if flags:
            poisoned.append((p, flags))
        else:
            clean.append(p)
    return clean, poisoned
