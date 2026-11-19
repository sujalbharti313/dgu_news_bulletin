"""Deterministic text signals used by verification. Pure functions over strings: no network, no model.

They are heuristics, and the results say so: every signal is reported with what triggered it, so an admin can
see why a story was flagged and the patterns can be tuned. They never decide on their own that a story is
false; they lower scores, add review flags and feed the quality gates.
"""

import re
from collections.abc import Iterable

from app.intelligence.evidence import normalise_number

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]*[A-Za-z0-9+#]|[A-Za-z]|\d+(?:\.\d+)?")
STOPWORDS = frozenset(
    """a about above after again against all also am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has have having he
    her here hers him his how i if in into is it its itself just me more most my new no nor not now of off on once
    only or other our ours out over own same she should so some such than that the their theirs them then there
    these they this those through to too under until up very was we were what when where which while who whom why
    will with would you your yours says said say according announced announces company its it's per via using
    use used one two first""".split()
)

# --- Hype and promotion -------------------------------------------------------------------------------------
_MARKETING = re.compile(
    r"(?i)\b(revolutionar\w*|game[- ]?chang\w*|groundbreaking|ground-breaking|mind[- ]?blowing|unprecedented|"
    r"cutting[- ]edge|best[- ]in[- ]class|industry[- ]leading|world[- ]class|next[- ]gen\w*|supercharg\w*|"
    r"seamless\w*|magic(al)?|unleash\w*|transformative|disrupt\w*|paradigm shift)\b"
)
_PROMO = re.compile(
    r"(?i)\b(we('| a)re (excited|thrilled|delighted|proud) to|thrilled to announce|sign up (now|today)|"
    r"book a demo|contact (our )?sales|limited[- ]time|available now|try it (now|today|free)|join the waitlist|"
    r"learn more at|request access today)\b"
)
_VAGUE_AI = re.compile(
    r"(?i)\b(ai[- ]powered|powered by ai|leverag\w* (the power of )?ai|harness\w* (the power of )?ai|"
    r"ai[- ]driven|ai[- ]first|smart(er)? ai|next[- ]level ai|intelligent (platform|solution)s?)\b"
)
_SUPERIORITY = re.compile(
    r"(?i)\b(outperform\w*|beats?|beating|surpass\w*|better than|superior to|best|most (powerful|capable|"
    r"advanced|accurate)|state[- ]of[- ]the[- ]art|sota|world'?s (first|best|fastest|largest|most)|"
    r"leading|fastest|smartest|top[- ]performing)\b"
)
_BENCHMARK = re.compile(
    r"(?i)\b(benchmark\w*|swe-?bench|mmlu|gsm8k|humaneval|arena|elo|leaderboard|accuracy|f1|bleu|"
    r"pass@\d|score[sd]?|evaluation|eval)\b"
)
_CERTAINTY_SPECULATION = re.compile(
    r"(?i)\b(will (replace|revolutioni[sz]e|transform|change|end|kill|make) \w+|is set to|poised to|"
    r"guarantees?|will never|will always|inevitabl\w*|certain(ly)? (to|will))\b"
)
_HEDGE_SPECULATION = re.compile(
    r"(?i)\b(rumou?r\w*|reportedly|allegedly|leak(ed|s)?|sources (say|said|familiar)|is said to|"
    r"expected to|could|might|may|speculat\w*|unconfirmed|plans? to|aims? to)\b"
)
_OPINION = re.compile(
    r"(?i)\b(opinion|op-ed|editorial|column(ist)?|commentary|i think|i believe|in my view|my take|"
    r"we believe|perspective|analysis:)\b"
)
# Absolute or sensational wording that a headline must not add on top of the source.
_ABSOLUTE = re.compile(
    r"(?i)\b(every|all|always|never|entirely|completely|totally|forever|solv(e|es|ed)|prov(e|es|ed|en)|"
    r"replac(e|es|ed|ing)|end of|thinks? like a human|human[- ]level|sentient|conscious|agi|"
    r"superintelligen\w*|breakthrough|first[- ]ever|world'?s first|historic)\b"
)
_LIMITING = re.compile(
    r"(?i)\b(not (been )?(yet )?(tested|peer[- ]reviewed|independently|verified|available|general)|"
    r"has not been|have not been|preliminary|in (the )?lab|limited|early[- ]stage|prototype|preprint|"
    r"pilot|experimental|small[- ]scale|does not|did not|not a general)\b"
)

# --- Technical substance --------------------------------------------------------------------------------
TECHNICAL_SIGNALS: dict[str, re.Pattern[str]] = {
    "research_method": re.compile(
        r"(?i)\b(we (present|propose|introduce|show|study)|method|approach|algorithm|architecture|"
        r"framework|ablation|experiment\w*|hypothes\w*|findings?|results? show)\b"
    ),
    "model_details": re.compile(
        r"(?i)\b(parameters?|weights|context (window|length)|tokens?|fine[- ]?tun\w*|pre[- ]?train\w*|"
        r"training (data|run|set)|inference|quantiz\w*|distill\w*|mixture[- ]of[- ]experts|moe|transformer|"
        r"diffusion|multimodal|reasoning model|embedding\w*|latency|throughput)\b"
    ),
    "evaluation": re.compile(
        r"(?i)\b(benchmark\w*|evaluat\w*|accuracy|state[- ]of[- ]the[- ]art|baseline|swe-?bench|mmlu|"
        r"leaderboard|error rate|reproduc\w*)\b"
    ),
    "open_release": re.compile(
        r"(?i)\b(open[- ]source\w*|open[- ]weights?|released? (the )?(code|weights|model|dataset)|github|"
        r"hugging ?face|apache[- ]2|mit licen[cs]e|open licen[cs]e|model card|dataset)\b"
    ),
    "hardware_robotics": re.compile(
        r"(?i)\b(gpus?|tpus?|chips?|accelerators?|data cent(er|re)s?|compute|semiconductor|robot\w*|"
        r"actuator\w*|embodied|autonomous|drone\w*|sensor\w*|manipulat\w*)\b"
    ),
    "policy_detail": re.compile(
        r"(?i)\b(regulation|draft rules?|bill|act|law|framework|guidelines?|standard|compliance|"
        r"public comment\w*|ministry|parliament|court|ruling)\b"
    ),
}
# Pointers to checkable primary material inside an article.
_EVIDENCE_LINKS = re.compile(
    r"(?i)(arxiv|doi\.org|\bdoi\b|github\.com|\bgithub\b|hugging ?face\.co|model card|paper|preprint|"
    r"technical report|release notes|dataset|demo|documentation|white ?paper|peer[- ]reviewed|journal|"
    r"proceedings|conference)"
)
# Numbers that look like quantities (with a unit or a following word), for cross-source conflict checks.
_QUANTITY = re.compile(
    r"(?i)(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(%|percent|per cent|billion|million|thousand|[kmb]\b|"
    r"hours?|days?|tokens?|parameters?|gpus?|languages?|users?|crore|lakh|x\b)\s*(?:on|of|in|for)?\s*"
    r"([A-Za-z][\w.-]*)?"
)


def words(text: str | None) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text or "")]


def content_words(text: str | None) -> set[str]:
    """Meaningful words (stopwords and one-letter tokens removed), casefolded."""
    return {w for w in words(text) if w not in STOPWORDS and (len(w) > 1 or w.isdigit())}


def coverage(claim: str | None, evidence: Iterable[str]) -> float:
    """Share of the claim's content words that appear anywhere in the evidence text (1.0 for an empty claim).
    A light lexical check: it catches statements with no footing in the source, not paraphrase errors."""
    claim_words = content_words(claim)
    if not claim_words:
        return 1.0
    pool: set[str] = set()
    for text in evidence:
        pool |= content_words(text)
    # Also accept simple plural/singular variants ("models" ~ "model").
    hits = sum(1 for w in claim_words if w in pool or w.rstrip("s") in pool or f"{w}s" in pool)
    return hits / len(claim_words)


def novelty(text: str | None, baseline: Iterable[str]) -> float:
    """Share of content words in `text` that are absent from the baseline texts."""
    mine = content_words(text)
    if not mine:
        return 0.0
    base: set[str] = set()
    for b in baseline:
        base |= content_words(b)
    return len(mine - base) / len(mine)


def jaccard(a: str | None, b: str | None) -> float:
    x, y = content_words(a), content_words(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def matches(pattern: re.Pattern[str], text: str | None) -> list[str]:
    """Distinct matched phrases, casefolded, in order of appearance."""
    out: list[str] = []
    for m in pattern.finditer(text or ""):
        phrase = m.group(0).casefold().strip()
        if phrase not in out:
            out.append(phrase)
    return out


def marketing_terms(text: str | None) -> list[str]:
    return matches(_MARKETING, text)


def promotional_phrases(text: str | None) -> list[str]:
    return matches(_PROMO, text)


def vague_ai_terms(text: str | None) -> list[str]:
    return matches(_VAGUE_AI, text)


def superiority_terms(text: str | None) -> list[str]:
    return matches(_SUPERIORITY, text)


def benchmark_terms(text: str | None) -> list[str]:
    return matches(_BENCHMARK, text)


def certainty_speculation(text: str | None) -> list[str]:
    return matches(_CERTAINTY_SPECULATION, text)


def hedged_speculation(text: str | None) -> list[str]:
    return matches(_HEDGE_SPECULATION, text)


def opinion_markers(text: str | None) -> list[str]:
    return matches(_OPINION, text)


def absolute_terms(text: str | None) -> list[str]:
    return matches(_ABSOLUTE, text)


def limiting_terms(text: str | None) -> list[str]:
    return matches(_LIMITING, text)


def evidence_links(text: str | None) -> list[str]:
    return matches(_EVIDENCE_LINKS, text)


def technical_signals(text: str | None) -> dict[str, list[str]]:
    out = {}
    for name, pattern in TECHNICAL_SIGNALS.items():
        found = matches(pattern, text)
        if found:
            out[name] = found
    return out


def quantities(text: str | None) -> list[tuple[str, str, str]]:
    """(number, unit, following word) triples, normalised: ('71.2', '%', 'swe-bench')."""
    out = []
    for m in _QUANTITY.finditer(text or ""):
        unit = m.group(2).casefold().replace("percent", "%").replace("per cent", "%")
        unit = unit.rstrip("s") if unit not in {"%", "x"} else unit
        following = (m.group(3) or "").casefold()
        if following in STOPWORDS:
            following = ""
        out.append((normalise_number(m.group(1)), unit, following))
    return out
