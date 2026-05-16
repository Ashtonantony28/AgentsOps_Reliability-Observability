import re
from typing import List, Tuple

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

INJECTION_PATTERNS = [
    r"ignore (all|previous|prior) instructions",
    r"disregard (the|any) (system|previous) prompt",
    r"you are now [a-z ]{3,40}",
    r"reveal (the|your) (system )?prompt",
    r"print your (system|hidden|initial) prompt",
    r"jailbreak",
    r"DAN mode",
    r"developer mode enabled",
    r"act as [a-z ]{3,40} without restrictions",
    r"forget (you are|everything)",
    r"</?system>",
    r"\bsudo\s+(rm|format|drop)\b",
    r"base64 decode (and run|then execute)",
]
INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def detect_pii(text: str) -> List[str]:
    if not text:
        return []
    flags = []
    if EMAIL_RE.search(text):
        flags.append("email")
    if PHONE_RE.search(text):
        flags.append("phone")
    if SSN_RE.search(text):
        flags.append("ssn")
    if CC_RE.search(text):
        flags.append("credit_card")
    return flags


def mask_pii(text: str) -> str:
    if not text:
        return text
    text = EMAIL_RE.sub("<email>", text)
    text = SSN_RE.sub("<ssn>", text)
    text = CC_RE.sub("<cc>", text)
    text = PHONE_RE.sub("<phone>", text)
    return text


def injection_score(text: str) -> Tuple[float, List[str]]:
    if not text:
        return 0.0, []
    hits = []
    for pat in INJECTION_RE:
        m = pat.search(text)
        if m:
            hits.append(m.group(0)[:80])
    score = min(1.0, len(hits) * 0.34)
    return score, hits


def cosine_similarity(a: str, b: str) -> float:
    """Lightweight token-set Jaccard fallback when no embedding model is loaded."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
