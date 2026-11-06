"""Deterministic signals used for duplicate detection and story clustering. No network, no AI calls.

- identifiers: arXiv ids and DOIs found in the URL (the same paper under abs/, pdf/ or another version)
- tokens: case-folded words, stop words removed, light suffix stemming; version strings stay whole
- shingles: overlapping word 3-grams of the excerpt (near-duplicate text)
- entities: organisations from a small gazetteer, model/product names with versions, benchmark names
- event type: a keyword lexicon (launch, pricing, safety, ...) weighted towards the title
- content hash: sha256 of the normalised title + excerpt, only when the excerpt is long enough

Every function is pure; the same input always gives the same output.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote, urlsplit

from app.processing.text import HYPHENS

# --- identifiers ----------------------------------------------------------------------------------

_ARXIV_HOSTS = {"arxiv.org", "export.arxiv.org"}
_ARXIV_PATH = re.compile(
    r"^/(?:abs|pdf|html|format)/((?:\d{4}\.\d{4,5})|(?:[a-z\-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?(?:\.pdf)?/?$", re.I
)
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s?#]+)", re.I)


def article_identifiers(url: str | None) -> list[str]:
    """Stable identifiers of the article itself, from its URL only (never guessed): `arxiv:2401.01234`, `doi:...`."""
    if not url:
        return []
    try:
        parts = urlsplit(url)
    except ValueError:
        return []
    host = (parts.hostname or "").lower().removeprefix("www.")
    found: list[str] = []
    if host in _ARXIV_HOSTS:
        match = _ARXIV_PATH.match(parts.path)
        if match:
            found.append(f"arxiv:{match.group(1).lower()}")
    if host in {"doi.org", "dx.doi.org"} or "/doi/" in parts.path:
        match = _DOI.search(unquote(parts.path))
        if match:
            found.append(f"doi:{match.group(1).rstrip('/.').lower()}")
    return found


# --- tokens -----------------------------------------------------------------------------------------

STOPWORDS = frozenset(
    """a about above after again against all also am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has have having
    he her here hers him his how i if in into is it its itself just me more most my new no nor not now of off
    on once only or other our ours out over own same she should so some such than that the their them then
    there these they this those through to too under until up very was we were what when where which while
    who whom why will with would you your yours via says said say today week year years day days using use
    used one two first latest according report reports reported""".split()
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-+][a-z0-9]+)*")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return text.replace("’", "'").replace("‘", "'").translate(HYPHENS)


def stem(token: str) -> str:
    """Tiny suffix stemmer: launches/launched/launching → launch, price/pricing → pric. Leaves short words,
    numbers and version strings alone. Only used to compare words with each other, never shown."""
    if len(token) <= 4 or any(ch.isdigit() for ch in token) or "-" in token or "." in token:
        return token
    if token.endswith("ies") and len(token) > 5:
        token = token[:-3] + "y"
    elif token.endswith("ing") and len(token) > 6:
        token = token[:-3]
    elif token.endswith("ed") and len(token) > 5:
        token = token[:-2]
    elif token.endswith("s") and not token.endswith("ss") and len(token) > 4:
        token = token[:-1]
    if token.endswith("e") and len(token) > 4:
        token = token[:-1]
    return token


def raw_tokens(text: str | None) -> list[str]:
    return _TOKEN.findall(_fold(text or "").replace("'s ", " "))


def content_tokens(text: str | None) -> list[str]:
    """Words that carry meaning: stop words removed, stemmed. Version strings ("gpt-5.1") are kept whole and
    also split, so "GPT-5.1" and "GPT 5.1" overlap."""
    out: list[str] = []
    for token in raw_tokens(text):
        if token in STOPWORDS:
            continue
        out.append(stem(token))
        if "-" in token or "." in token:
            out.extend(part for part in re.split(r"[.\-+]", token) if part and part not in STOPWORDS and len(part) > 1)
    return out


def shingles(text: str | None, size: int = 3) -> set[str]:
    words = raw_tokens(text)
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def char_ngrams(text: str | None, size: int = 3) -> set[str]:
    folded = " ".join(raw_tokens(text))
    if len(folded) < size:
        return {folded} if folded else set()
    return {folded[i : i + size] for i in range(len(folded) - size + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def numbers_in(text: str | None) -> set[str]:
    """Numeric tokens (prices, versions, counts) of a title. Different numbers usually mean different facts."""
    return {t for t in raw_tokens(text) if any(ch.isdigit() for ch in t)}


def content_hash(title: str, body: str | None) -> str:
    """sha256 over case-folded words of the title and excerpt: insensitive to punctuation, spacing and markup."""
    canonical = " ".join(raw_tokens(title)) + "\n" + " ".join(raw_tokens(body))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- entities -------------------------------------------------------------------------------------

# Canonical organisation names and the lower-case spellings that refer to them. Deliberately small and
# conservative: a miss only means a weaker signal, a wrong alias would merge unrelated stories.
ORGANISATIONS: dict[str, tuple[str, ...]] = {
    "OpenAI": ("openai",),
    "Anthropic": ("anthropic",),
    "Google DeepMind": ("google deepmind", "deepmind"),
    "Google": ("google", "alphabet"),
    "Meta": ("meta", "meta ai", "facebook"),
    "Microsoft": ("microsoft",),
    "Apple": ("apple",),
    "Amazon": ("amazon", "aws", "amazon web services"),
    "NVIDIA": ("nvidia",),
    "Hugging Face": ("hugging face", "huggingface"),
    "Mistral AI": ("mistral ai",),
    "xAI": ("xai",),
    "Cohere": ("cohere",),
    "Stability AI": ("stability ai",),
    "IBM": ("ibm",),
    "Intel": ("intel",),
    "AMD": ("amd",),
    "Qualcomm": ("qualcomm",),
    "Samsung": ("samsung",),
    "Baidu": ("baidu",),
    "Alibaba": ("alibaba",),
    "Tencent": ("tencent",),
    "DeepSeek": ("deepseek",),
    "Moonshot AI": ("moonshot ai",),
    "Zhipu AI": ("zhipu", "zhipu ai"),
    "Perplexity": ("perplexity",),
    "Databricks": ("databricks",),
    "Salesforce": ("salesforce",),
    "Adobe": ("adobe",),
    "Runway": ("runwayml", "runway ml"),
    "ElevenLabs": ("elevenlabs",),
    "Character.AI": ("character.ai", "character ai"),
    "AI21 Labs": ("ai21", "ai21 labs"),
    "Allen Institute for AI": ("allen institute for ai", "ai2"),
    "Sarvam AI": ("sarvam", "sarvam ai"),
    "Krutrim": ("krutrim",),
    "Infosys": ("infosys",),
    "Tata Consultancy Services": ("tcs", "tata consultancy services"),
    "Wipro": ("wipro",),
    "Reliance Jio": ("reliance jio", "jio"),
    "MeitY": ("meity", "ministry of electronics and information technology"),
    "IndiaAI Mission": ("indiaai", "india ai mission", "indiaai mission"),
    "European Commission": ("european commission",),
    "European Union": ("european union",),
    "US FTC": ("ftc", "federal trade commission"),
    "UK AI Security Institute": ("ai security institute", "ai safety institute"),
    "arXiv": ("arxiv",),
}
# Model and product families whose name is followed by a version or tier.
_FAMILIES = (
    "gpt", "chatgpt", "claude", "gemini", "gemma", "llama", "mistral", "mixtral", "codestral", "magistral",
    "qwen", "deepseek", "grok", "phi", "sora", "dall-e", "whisper", "codex", "copilot", "stable diffusion",
    "imagen", "veo", "flux", "nemotron", "granite", "olmo", "falcon", "kimi", "glm", "ernie", "jamba",
    "midjourney", "firefly", "nova", "palm", "bard", "alphafold", "alphago", "sarvam",
)  # fmt: skip
_TIERS = (
    "pro", "mini", "flash", "lite", "turbo", "ultra", "max", "nano", "large", "medium", "small", "instruct",
    "chat", "opus", "sonnet", "haiku", "vision", "coder", "omni", "thinking", "reasoning", "preview", "plus",
)  # fmt: skip
_VERSION = r"v?\d+(?:\.\d+)*[a-z]?"
_SEP = r"[\s\-]?"
_PRODUCT = re.compile(
    r"(?<![\w.])(?P<family>" + "|".join(re.escape(f) for f in sorted(_FAMILIES, key=len, reverse=True)) + r")"
    r"(?P<rest>(?:" + _SEP + r"(?:" + _VERSION + r"|" + "|".join(_TIERS) + r")(?![\w]))*)",
    re.I,
)
# OpenAI's o-series (o1, o3-mini, o4-mini-high).
_O_SERIES = re.compile(r"(?<![\w.])(o[1-9])((?:-(?:mini|pro|preview|high|low))*)(?![\w.])")
# Any other capitalised name directly followed by a dotted version ("Foo 2.5", "Bar v3"), from the original case.
_GENERIC_VERSIONED = re.compile(r"(?<![\w.])([A-Z][A-Za-z]{2,})[\s\-](v?\d+\.\d+(?:\.\d+)*|v\d+)(?![\w.])")
_NOT_PRODUCTS = {
    "version", "release", "update", "chapter", "section", "part", "phase", "stage", "series", "web", "industry",
    "model", "models", "edition", "gen", "generation", "level", "tier", "plan", "round", "top", "page", *_TIERS,
}  # fmt: skip

BENCHMARKS: dict[str, tuple[str, ...]] = {
    "MMLU": ("mmlu", "mmlu-pro"),
    "GPQA": ("gpqa",),
    "SWE-bench": ("swe-bench", "swe bench", "swebench"),
    "HumanEval": ("humaneval",),
    "ARC-AGI": ("arc-agi", "arc agi"),
    "GSM8K": ("gsm8k",),
    "MATH": ("math benchmark",),
    "AIME": ("aime",),
    "HellaSwag": ("hellaswag",),
    "BIG-bench": ("big-bench", "bigbench"),
    "LMArena": ("lmarena", "chatbot arena", "lmsys arena"),
    "MLPerf": ("mlperf",),
    "Humanity's Last Exam": ("humanity's last exam",),
    "Terminal-Bench": ("terminal-bench",),
}
_BENCHMARK_PATTERNS = [
    (name, re.compile(r"(?<![\w])(?:" + "|".join(re.escape(a) for a in aliases) + r")(?![\w])"))
    for name, aliases in BENCHMARKS.items()
]


_PARAMETER_COUNT = re.compile(r"\d+(?:\.\d+)?[bmk]")


def _product_key(family: str, rest: str) -> str | None:
    parts = [p for p in re.split(r"[\s\-]+", rest.strip(" -").lower()) if p]
    # "405B", "7b": parameter counts are sizes of one release, not versions.
    versions = [p.removeprefix("v") for p in parts if re.fullmatch(_VERSION, p) and not _PARAMETER_COUNT.fullmatch(p)]
    tiers = sorted({p for p in parts if p in _TIERS})
    if not versions and not tiers:
        return None  # a bare family word ("Copilot", "Claude") is not a specific product
    family = family.lower().replace(" ", "-")
    return " ".join([family, *versions, *tiers])


def extract_products(text: str | None) -> set[str]:
    """Model/product names with a version or tier, normalised: "Claude Opus 4.5" → "claude 4.5 opus",
    "GPT-5.1" and "GPT 5.1" → "gpt 5.1"."""
    if not text:
        return set()
    found: set[str] = set()
    folded = unicodedata.normalize("NFKC", text)
    for match in _PRODUCT.finditer(folded):
        key = _product_key(match.group("family"), match.group("rest"))
        if key:
            found.add(key)
    for match in _O_SERIES.finditer(folded):
        found.add(" ".join(["openai-o", match.group(1)[1:], *sorted(t for t in match.group(2).split("-") if t)]))
    for match in _GENERIC_VERSIONED.finditer(folded):
        name = match.group(1).lower()
        if name not in _NOT_PRODUCTS and name not in _FAMILIES:
            found.add(f"{name} {match.group(2).lower().removeprefix('v')}")
    return found


def product_family(key: str) -> str:
    return key.split(" ", 1)[0]


def product_version(key: str) -> str | None:
    """The main version of a product key ("gpt 5.1 mini" → "5.1"), or None for tier-only names."""
    for part in key.split(" ")[1:]:
        if part not in _TIERS:
            return part
    return None


def conflicting_products(a: set[str], b: set[str]) -> list[str]:
    """Families named with different versions in a and b ("GPT-5" vs "GPT-5.1", "Llama 3" vs "Llama 4").
    Tiers alone (mini, pro) and parameter counts never conflict: one launch often includes several."""
    conflicts = []
    for family in sorted({product_family(k) for k in a} & {product_family(k) for k in b}):
        va = {product_version(k) for k in a if product_family(k) == family} - {None}
        vb = {product_version(k) for k in b if product_family(k) == family} - {None}
        if va and vb and not (va & vb):
            conflicts.append(family)
    return conflicts


_ALIAS_TO_ORG = {alias: name for name, aliases in ORGANISATIONS.items() for alias in aliases}
_ANY_ORG = re.compile(
    r"(?<![\w.])(" + "|".join(re.escape(a) for a in sorted(_ALIAS_TO_ORG, key=len, reverse=True)) + r")(?![\w])"
)


def extract_organisations(text: str | None) -> set[str]:
    """Organisations named in the text. Longest alias wins, so "Google DeepMind" is not also "Google"
    unless Google is mentioned on its own."""
    if not text:
        return set()
    return {_ALIAS_TO_ORG[m.group(1)] for m in _ANY_ORG.finditer(_fold(text))}


def extract_benchmarks(text: str | None) -> set[str]:
    if not text:
        return set()
    folded = _fold(text)
    return {name for name, pattern in _BENCHMARK_PATTERNS if pattern.search(folded)}


# --- event type ----------------------------------------------------------------------------------

# Ordered: on a tie the earlier, more specific type wins ("updates API pricing" is pricing, not update).
EVENT_LEXICON: dict[str, tuple[str, ...]] = {
    "pricing": (r"pric(e|es|ed|ing)", r"cheaper", r"price cuts?", r"per million tokens", r"rate limits?", r"free tier",
                r"subscription", r"paid plans?", r"billing", r"discount(s|ed)?"),
    "safety": (r"safety", r"system card", r"red[- ]team(ing)?", r"alignment", r"safeguards?", r"preparedness",
               r"misuse", r"jailbreaks?", r"responsible (ai|scaling)", r"risk (report|assessment|framework)"),
    "legal": (r"lawsuits?", r"sue[sd]?", r"suing", r"court", r"copyright", r"settlement", r"antitrust", r"infring\w*"),
    "policy": (r"regulat\w*", r"legislation", r"\bbill\b", r"\bact\b", r"policy", r"policies", r"government",
               r"ministry", r"executive order", r"guidelines", r"compliance", r"\bban(s|ned)?\b"),
    "funding": (r"raises?", r"raised", r"funding", r"valuation", r"series [a-f]\b", r"invest(s|ed|ment|ments)?",
                r"\bipo\b", r"seed round"),
    "acquisition": (r"acquir(es|ed|e|ing)", r"acquisition", r"buys", r"bought", r"merger", r"merges?"),
    "partnership": (r"partner(s|ed|ship|ships)?", r"collaborat\w*", r"teams? up", r"alliance", r"joint venture"),
    "incident": (r"outages?", r"breach(es)?", r"leak(s|ed)?", r"vulnerabilit(y|ies)", r"exploit(s|ed)?",
                 r"incident", r"\bdown\b", r"disruption", r"attack"),
    "people": (r"hires?", r"hired", r"appoint(s|ed)?", r"steps? down", r"resign(s|ed)?", r"\bceo\b", r"departs?",
               r"joins", r"poach\w*"),
    "research": (r"paper", r"preprint", r"study", r"researchers?", r"we propose", r"benchmark(s)?", r"dataset",
                 r"findings", r"evaluation", r"arxiv"),
    "launch": (r"launch(es|ed|ing)?", r"releas(e|es|ed|ing)", r"introduc(es|ed|ing|e)", r"unveil(s|ed)?",
               r"debut(s|ed)?", r"rolls? out", r"rolled out", r"now available", r"generally available",
               r"open[- ]sourc(es|ed|ing)", r"ships", r"announc(es|ed|ing) (the )?new"),
    "update": (r"updat(e|es|ed)", r"upgrad(e|es|ed)", r"adds?", r"added", r"improv(es|ed|ements?)", r"expands?",
               r"deprecat\w*", r"retir(es|ed|ing)", r"new features?"),
}  # fmt: skip
_EVENT_PATTERNS = {
    name: re.compile(r"(?<![\w])(?:" + "|".join(patterns) + r")(?![\w])", re.I)
    for name, patterns in EVENT_LEXICON.items()
}
TITLE_WEIGHT = 3
MIN_EVENT_SCORE = 2
_ANY_EVENT_WORD = re.compile("|".join(f"(?:{p})" for patterns in EVENT_LEXICON.values() for p in patterns), re.I)
# Words of organisation names ("hugging", "face", "openai", "ai").
_ORG_WORDS = frozenset(w for aliases in ORGANISATIONS.values() for alias in aliases for w in raw_tokens(alias))


def topic_tokens(text: str | None) -> set[str]:
    """Content words minus organisation names and event words: what an article is about beyond "Company X
    launches ...". Two reports of one event share topic words; two launches by one company do not."""
    out: set[str] = set()
    for token in raw_tokens(text):
        if token in STOPWORDS or token in _ORG_WORDS or _ANY_EVENT_WORD.fullmatch(token):
            continue
        out.add(stem(token))
        if "-" in token or "." in token:
            out.update(part for part in re.split(r"[.\-+]", token) if len(part) > 1 and part not in STOPWORDS)
    return out


def classify_event(title: str, body: str | None) -> tuple[str | None, dict[str, int]]:
    """The most likely event type and the per-type scores. None when there is no clear signal."""
    scores: dict[str, int] = {}
    for name, pattern in _EVENT_PATTERNS.items():
        score = TITLE_WEIGHT * len(pattern.findall(title or "")) + len(pattern.findall(body or ""))
        if score:
            scores[name] = score
    if not scores:
        return None, {}
    best = max(scores.values())
    if best < MIN_EVENT_SCORE:
        return None, scores
    for name in EVENT_LEXICON:  # first in priority order among the top scores
        if scores.get(name) == best:
            return name, scores
    return None, scores


# --- the feature bundle --------------------------------------------------------------------------


@dataclass
class Features:
    """Everything the matcher needs for one article. Rebuilt from the staged row each run (cheap, pure)."""

    title_tokens: set[str]
    title_ngrams: set[str]
    text_tokens: set[str]
    title_topic: set[str]
    text_topic: set[str]
    body_shingles: set[str]
    body_token_count: int
    title_numbers: set[str]
    identifiers: list[str]
    organisations: set[str]
    products: set[str]
    title_products: set[str]
    benchmarks: set[str]
    event_type: str | None
    event_scores: dict[str, int]
    content_hash: str | None
    site: str | None
    event_time: datetime
    time_basis: str
    entity_keys: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.entity_keys = (
            {f"org:{o}" for o in self.organisations}
            | {f"product:{p}" for p in self.products}
            | {f"benchmark:{b}" for b in self.benchmarks}
        )


def build_features(
    *,
    title: str,
    body: str | None,
    url: str,
    site: str | None,
    event_time: datetime,
    time_basis: str,
    min_body_tokens: int,
) -> Features:
    body_words = raw_tokens(body)
    full = f"{title}\n{body or ''}"
    event_type, scores = classify_event(title, body)
    return Features(
        title_tokens=set(content_tokens(title)),
        title_ngrams=char_ngrams(title),
        text_tokens=set(content_tokens(full)),
        title_topic=topic_tokens(title),
        text_topic=topic_tokens(full),
        body_shingles=shingles(body) if len(body_words) >= min_body_tokens else set(),
        body_token_count=len(body_words),
        title_numbers=numbers_in(title),
        identifiers=article_identifiers(url),
        organisations=extract_organisations(full),
        products=extract_products(full),
        title_products=extract_products(title),
        benchmarks=extract_benchmarks(full),
        event_type=event_type,
        event_scores=scores,
        content_hash=content_hash(title, body) if len(body_words) >= min_body_tokens else None,
        site=site,
        event_time=event_time,
        time_basis=time_basis,
    )
