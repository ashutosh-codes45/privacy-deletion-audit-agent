"""Multimodal evidence extraction with a local content cache.

The deterministic baseline in :mod:`audit_engine` reads only text-shaped
evidence. This module adds the two remaining asset classes from AGENTS.md
section 6.1 — PDF documents and image captures — behind one uniform interface,
plus the local cache that guarantees each asset is analysed exactly once.

Design
------
``DocumentAnalyzer`` is the single entry point. It resolves an asset by
extension:

``.pdf``
    Extracted with a dependency-free reader. The corpus PDFs are ReportLab
    output whose page content is an ``ASCII85Decode``+``FlateDecode`` stream, so
    ``zlib`` and ``base64`` from the standard library are sufficient. Because
    PDF text is emitted as many short show-text operators, the reader returns
    *windowed* lines: every resource id is re-emitted together with the tokens
    around it, so a resource and the identifier that names its owner can be
    matched on one line (the indirect-reference case in AGENTS.md section 6.6).

``.png`` / ``.jpg`` / ``.webp``
    Screenshots carry no embedded text (verified: none of the eleven corpus
    PNGs contain ``tEXt``/``iTXt``/``zTXt`` chunks), so they need a vision
    model. :class:`VisionClient` is an environment-gated, OpenAI-compatible
    client. When no credential is configured the asset is reported as
    ``unresolved`` and contributes **no** facts: the audit never invents
    evidence it did not actually read.

Caching
-------
Every result is memoised in-process and persisted to a JSON cache keyed by
``(file_id, sha256(content))``, so a repeated run — or a second pass over the
same asset by a different case — re-uses the extraction instead of paying for
it again (token and latency saving).

Secrets are read from the environment only (AGENTS.md sections 4.4 and 6.7);
nothing here writes a credential anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:  # imported as part of the ``code`` package
    import config
except ImportError:  # pragma: no cover - direct script execution
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config


MULTIMODAL_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"})

# Approximate token accounting used when no provider is configured. English
# business text averages roughly four characters per token; a vision call costs
# a fixed image budget on the input side.
CHARS_PER_TOKEN = 4
VISION_IMAGE_TOKEN_BUDGET = 1105


# ---------------------------------------------------------------------------
# PDF reading (standard library only)
# ---------------------------------------------------------------------------

# ``endstream`` contains the substring ``stream``, so the opening keyword must
# be anchored to a non-letter character or the two halves of ``endstream`` split
# into separate matches.
_STREAM_RE = re.compile(rb"(?<![A-Za-z])stream\r?\n(.*?)endstream", re.S)
_SHOW_TEXT_RE = re.compile(rb"\(((?:[^()\\]|\\.)*)\)\s*(?:Tj|')")
_SHOW_ARRAY_RE = re.compile(rb"\[(.*?)\]\s*TJ", re.S)
_STRING_RE = re.compile(rb"\(((?:[^()\\]|\\.)*)\)", re.S)

_OCTAL_RE = re.compile(rb"\\([0-7]{1,3})")
_ESCAPES = {
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"b": b"\b",
    b"f": b"\f",
    b"(": b"(",
    b")": b")",
    b"\\": b"\\",
}


def _unescape_pdf_string(raw: bytes) -> str:
    """Decode a PDF literal string body, honouring octal and backslash escapes."""
    out = bytearray()
    index = 0
    while index < len(raw):
        char = raw[index : index + 1]
        if char != b"\\":
            out += char
            index += 1
            continue
        octal = _OCTAL_RE.match(raw, index)
        if octal:
            out.append(int(octal.group(1), 8) & 0xFF)
            index = octal.end()
            continue
        nxt = raw[index + 1 : index + 2]
        out += _ESCAPES.get(nxt, nxt)
        index += 2
    return out.decode("latin-1")


def _inflate(body: bytes) -> bytes:
    """Return the decoded stream body, trying plain Flate then ASCII85+Flate."""
    tail = body.strip(b"\r\n")
    for attempt in (
        lambda t: zlib.decompress(t),
        lambda t: zlib.decompress(base64.a85decode(b"<~" + t, adobe=True)),
    ):
        try:
            return attempt(tail)
        except Exception:
            continue
    return b""


def extract_pdf_pages(path: Path) -> List[List[str]]:
    """Return the text tokens of each page in ``path`` as a list of pages.

    Only the content streams are considered: font programs and the
    ``ToUnicode`` CMap streams are skipped because they carry no page text.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return []

    pages: List[List[str]] = []
    for body in _STREAM_RE.findall(raw):
        data = _inflate(body)
        if not data or b"BT" not in data:
            continue
        if b"begincmap" in data or b"FontFile" in data:
            continue
        tokens: List[str] = []
        for match in _SHOW_TEXT_RE.finditer(data):
            value = _unescape_pdf_string(match.group(1)).strip()
            if value:
                tokens.append(value)
        for array in _SHOW_ARRAY_RE.findall(data):
            parts = [_unescape_pdf_string(item).strip() for item in _STRING_RE.findall(array)]
            joined = "".join(part for part in parts if part).strip()
            if joined:
                tokens.append(joined)
        if tokens:
            pages.append(tokens)
    return pages


def pdf_window_lines(pages: Sequence[Sequence[str]], window: int = 6) -> List[str]:
    """Return one line per resource-bearing token, widened to its neighbourhood.

    A PDF page lists a value in one show-text operator and its label in the
    next, so a single token rarely carries both an identifier and a resource id.
    Re-emitting each token together with the ``window`` tokens on either side
    lets the corpus index join them, which is what resolves indirect references
    such as a ticket number quoted next to the customer's email.
    """
    lines: List[str] = []
    for tokens in pages:
        for index, token in enumerate(tokens):
            if not re.search(r"\b\w+_\d+\b", token):
                continue
            low = max(0, index - window)
            high = min(len(tokens), index + window + 1)
            lines.append(" | ".join(part for part in tokens[low:high] if part))
    return lines


# ---------------------------------------------------------------------------
# PNG metadata
# ---------------------------------------------------------------------------


def extract_png_text_chunks(path: Path) -> List[str]:
    """Return any textual metadata chunks embedded in a PNG (usually none)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    found: List[str] = []
    for marker in (b"tEXt", b"iTXt", b"zTXt"):
        start = 0
        while True:
            at = raw.find(marker, start)
            if at < 0:
                break
            start = at + 1
            payload = raw[at + 4 : at + 4 + 4096]
            if marker == b"zTXt":
                payload = _inflate(payload.split(b"\x00", 1)[-1])[:4096]
            text = re.sub(rb"[^\x20-\x7e]+", b" ", payload).decode("ascii", "ignore").strip()
            if text:
                found.append(text)
    return found


# ---------------------------------------------------------------------------
# Vision / LLM client (environment-gated)
# ---------------------------------------------------------------------------


class VisionClient:
    """OpenAI-compatible chat-completions client with image input.

    Configured entirely from the environment so no credential is ever stored in
    the repository::

        ORCHESTRATE_LLM_API_KEY      required to enable the client
        ORCHESTRATE_LLM_BASE_URL     default https://api.deepseek.com/v1
        ORCHESTRATE_VISION_MODEL     default DeepSeek-V4.1-Flash
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self.api_key = (api_key or os.environ.get(config.LLM_API_KEY_ENV, "")).strip()
        self.base_url = (
            base_url or os.environ.get(config.LLM_BASE_URL_ENV, "") or config.DEFAULT_VISION_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get(config.VISION_MODEL_ENV, "") or config.DEFAULT_VISION_MODEL
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        """True when a credential is present, so image analysis can run."""
        return bool(self.api_key)

    def describe_image(self, path: Path, prompt: str) -> Dict[str, Any]:
        """Send one image plus ``prompt`` and return the parsed model reply.

        Raises ``RuntimeError`` when the client is not configured and
        ``urllib.error.URLError``/``HTTPError`` when the provider call fails; the
        caller degrades to ``unresolved`` rather than fabricating facts.
        """
        if not self.configured:
            raise RuntimeError("vision client is not configured (no API key in the environment)")

        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        suffix = path.suffix.lower().lstrip(".").replace("jpg", "jpeg")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": config.EXTRACTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/{suffix};base64,{encoded}"},
                        },
                    ],
                },
            ],
            "temperature": 0,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = body.get("usage", {}) or {}
        return {
            "content": content,
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class ExtractionCache:
    """In-process memo plus an on-disk JSON cache keyed by content digest."""

    def __init__(self, path: Path = config.EVIDENCE_CACHE_JSON) -> None:
        self.path = Path(path)
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if isinstance(payload, dict):
            self.entries = {
                str(key): value for key, value in payload.items() if isinstance(value, dict)
            }

    @staticmethod
    def digest(path: Path) -> str:
        """Content digest so an edited asset is treated as a new extraction."""
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return ""

    @staticmethod
    def key(file_id: str, digest: str) -> str:
        return f"{file_id}:{digest}"

    def get(self, file_id: str, digest: str) -> Optional[Dict[str, Any]]:
        entry = self.entries.get(self.key(file_id, digest))
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, file_id: str, digest: str, payload: Dict[str, Any]) -> None:
        self.entries[self.key(file_id, digest)] = payload
        self._dirty = True

    def save(self) -> None:
        """Persist the cache; a failure to write is never fatal."""
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.entries, indent=2, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return
        self._dirty = False


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


@dataclass
class Extraction:
    """The result of reading one evidence asset."""

    file_id: str
    kind: str
    source: str
    lines: List[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False
    error: str = ""

    @property
    def resolved(self) -> bool:
        """True when the asset yielded usable facts."""
        return bool(self.lines)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Extraction":
        return cls(
            file_id=str(payload.get("file_id", "")),
            kind=str(payload.get("kind", "")),
            source=str(payload.get("source", "")),
            lines=[str(line) for line in payload.get("lines", [])],
            input_tokens=int(payload.get("input_tokens", 0) or 0),
            output_tokens=int(payload.get("output_tokens", 0) or 0),
            cached=True,
        )


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN) if text else 0


class DocumentAnalyzer:
    """Read PDF and image evidence once each, behind a cache."""

    def __init__(self, cache: Optional[ExtractionCache] = None, vision: Optional[VisionClient] = None) -> None:
        self.cache = cache if cache is not None else ExtractionCache()
        self.vision = vision if vision is not None else VisionClient()
        self.input_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0
        self.analyzed = 0

    # -- public API --------------------------------------------------------

    def analyze(self, file_id: str, path: Path, prompt: str = "") -> Extraction:
        """Return the extraction for ``path``, consulting the cache first.

        The cache is keyed by ``(file_id, sha256(content))``, so a second pass
        over the same asset — by another case, or by a later sweep — re-uses the
        extraction instead of paying for it again.
        """
        digest = self.cache.digest(path)
        cached = self.cache.get(file_id, digest)
        if cached is not None:
            extraction = Extraction.from_dict(cached)
            self._account(extraction)
            return extraction

        self.analyzed += 1
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            extraction = self._analyze_pdf(file_id, path)
        elif suffix in MULTIMODAL_SUFFIXES:
            extraction = self._analyze_image(file_id, path, prompt)
        else:
            extraction = Extraction(file_id=file_id, kind="unknown", source="unsupported")

        self._account(extraction)
        self.cache.put(file_id, digest, extraction.to_dict())
        return extraction

    def usage(self) -> Dict[str, int]:
        """Aggregate token and call counts for the run."""
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "assets_analyzed": self.analyzed,
            "cache_hits": self.cache.hits,
        }

    def finish(self) -> None:
        self.cache.save()

    # -- internals ---------------------------------------------------------

    def _account(self, extraction: Extraction) -> None:
        self.input_tokens += extraction.input_tokens
        self.output_tokens += extraction.output_tokens
        if extraction.source.endswith("vision"):
            self.model_calls += 1

    def _analyze_pdf(self, file_id: str, path: Path) -> Extraction:
        pages = extract_pdf_pages(path)
        lines = pdf_window_lines(pages)
        text = "\n".join(lines)
        return Extraction(
            file_id=file_id,
            kind="pdf",
            source="pdf-stdlib",
            lines=lines,
            input_tokens=_estimate_tokens(text),
            output_tokens=0,
        )

    def _analyze_image(self, file_id: str, path: Path, prompt: str) -> Extraction:
        embedded = extract_png_text_chunks(path)
        lines = list(embedded)
        source = "png-metadata" if embedded else "png-unresolved"
        # No call is made when the provider is unconfigured, so no provider
        # tokens are charged for the pixels in that case.
        input_tokens = _estimate_tokens("\n".join(embedded))
        output_tokens = 0
        error = ""

        if self.vision.configured:
            input_tokens += VISION_IMAGE_TOKEN_BUDGET
            try:
                reply = self.vision.describe_image(path, prompt)
            except (urllib.error.URLError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                source = "png-vision"
                facts = parse_vision_facts(str(reply.get("content", "")))
                lines.extend(_facts_to_lines(facts))
                input_tokens = int(reply.get("input_tokens", 0) or 0) or input_tokens
                output_tokens = int(reply.get("output_tokens", 0) or 0)
        else:
            error = "vision provider not configured; image left unresolved"

        return Extraction(
            file_id=file_id,
            kind="image",
            source=source,
            lines=lines,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=error,
        )


# ---------------------------------------------------------------------------
# Vision reply normalisation
# ---------------------------------------------------------------------------


def parse_vision_facts(content: str) -> List[Dict[str, Any]]:
    """Extract the JSON fact array from a model reply.

    The model is asked for a bare JSON array, but replies are frequently wrapped
    in prose or a fenced block, so the first balanced ``[``/``{`` payload is
    recovered. Anything unparseable yields no facts rather than an exception.
    """
    text = content.strip()
    if not text:
        return []
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start < 0 or end <= start:
            continue
        try:
            payload = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            payload = [payload]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    return []


def _facts_to_lines(facts: Sequence[Dict[str, Any]]) -> List[str]:
    """Render model facts as ``key=value`` lines the corpus can index."""
    lines: List[str] = []
    for fact in facts:
        parts = []
        for key in ("resource_id", "system_id", "identifier", "email", "phone", "email_hash",
                    "subject_reference", "state", "restore_allowed", "enabled", "note"):
            value = fact.get(key)
            if value not in (None, "", []):
                parts.append(f"{key}={value}")
        if parts:
            lines.append(" ".join(parts))
    return lines


__all__ = [
    "DocumentAnalyzer",
    "Extraction",
    "ExtractionCache",
    "MULTIMODAL_SUFFIXES",
    "VisionClient",
    "extract_pdf_pages",
    "extract_png_text_chunks",
    "parse_vision_facts",
    "pdf_window_lines",
]
