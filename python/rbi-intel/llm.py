"""Provider-agnostic JSON-mode LLM access for the analysis layer.

Ported from the standalone pipeline's `gemini_helper.py`, generalised so the
backend is a configuration choice rather than an import scattered through five
scripts. The original project hit this the hard way: `03_extract_requirements.py`
was switched from Anthropic to Gemini, and `04`, `06` and the Streamlit app
were each left behind on a dead `import anthropic` — three separate places to
remember, three separate ways to be broken.

Everything a caller needs is `get_provider()` and `provider.json_call(...)`.

Selection, in order:
    RBI_INTEL_LLM=gemini|anthropic|stub   explicit choice
    GEMINI_API_KEY / GOOGLE_API_KEY set   -> gemini
    ANTHROPIC_API_KEY set                 -> anthropic
    otherwise                             -> error naming both env vars

The `stub` provider returns deterministic canned JSON. It exists so the whole
ingest -> chunk -> extract -> scaffold -> validate chain can be exercised in
tests and on an air-gapped machine without spending a single token.

What is preserved verbatim from `gemini_helper.py`, because it was learned
against the real free tier and is the difference between a 30-second failure
and a 30-minute one:

  * a temporary 429 (per-minute throttle) is retried with Google's own
    suggested delay, parsed out of the error text;
  * a *daily* quota exhaustion is detected and raised immediately — retrying
    it only burns wall-clock time, since nothing resets until tomorrow;
  * a 404 is reported as "the model name is wrong", not as a transient fault.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Protocol


class QuotaExhausted(RuntimeError):
    """A hard, non-retryable quota limit — daily cap, or credit exhausted.

    Callers should catch this and checkpoint rather than abort: extraction is
    resumable, so a run that dies at clause 240 of 396 should leave those 240
    rows committed and say so.
    """


class LLMError(RuntimeError):
    """Any other unrecoverable provider failure."""


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class Provider(Protocol):
    name: str
    model: str

    def json_call(
        self,
        system_prompt: str,
        user_content: str,
        response_schema: dict | None = None,
        max_output_tokens: int = 1200,
    ) -> dict:
        """Return a parsed JSON object. Raises QuotaExhausted / LLMError."""
        ...


def strip_json_fences(raw: str) -> str:
    """Defensive cleanup for providers not using native structured output."""
    text = raw.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _parse_json(raw: str, provider: str) -> dict:
    try:
        return json.loads(strip_json_fences(raw))
    except json.JSONDecodeError as e:
        # Truncation is by far the most common cause: JSON-schema mode adds
        # framing overhead, and a max_output_tokens that was fine for simple
        # clauses silently cuts long ones off mid-string.
        raise LLMError(
            f"{provider} returned unparseable JSON ({e}). "
            f"If this is frequent, raise max_output_tokens. "
            f"First 200 chars: {raw[:200]!r}"
        ) from e


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

_DAILY_MARKERS = (
    "generate_requestsperday",
    "generaterequestsperday",
    "perdayperprojectpermodel",
)


def _is_daily_quota(text: str) -> bool:
    low = text.lower()
    if any(m in low for m in _DAILY_MARKERS):
        return True
    return "perday" in low and "quota" in low


_RETRY_DELAY_RE = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.I)


def _suggested_delay(text: str, default: float) -> float:
    m = _RETRY_DELAY_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


class GeminiProvider:
    name = "gemini"
    # Change the default in one place, not in every caller.
    DEFAULT_MODEL = "gemini-3.5-flash-lite"

    def __init__(self, model: str | None = None, max_retries: int = 5):
        try:
            from google import genai  # noqa: F401
        except ImportError as e:
            raise LLMError(
                "google-genai is not installed.  pip install google-genai"
            ) from e
        from google import genai

        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LLMError("Set GEMINI_API_KEY (get one free at aistudio.google.com/apikey).")

        self.model = model or os.environ.get("RBI_INTEL_MODEL") or self.DEFAULT_MODEL
        self.max_retries = max_retries
        self._client = genai.Client(api_key=key)

    def json_call(self, system_prompt, user_content, response_schema=None, max_output_tokens=1200) -> dict:
        from google.genai import errors, types

        cfg: dict[str, Any] = dict(
            system_instruction=system_prompt,
            max_output_tokens=max_output_tokens,
            temperature=0.2,
        )
        if response_schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = response_schema

        delay = 8.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._client.models.generate_content(
                    model=self.model,
                    contents=user_content,
                    config=types.GenerateContentConfig(**cfg),
                )
                text = (resp.text or "").strip()
                if not text:
                    raise LLMError(f"Gemini returned an empty response (model={self.model}).")
                return _parse_json(text, "gemini")

            except errors.ClientError as e:
                msg = str(e)
                if e.code == 429 and _is_daily_quota(msg):
                    raise QuotaExhausted(
                        f"Gemini daily quota exhausted for model '{self.model}'. "
                        f"This is a per-day cap, not a per-minute throttle — retrying "
                        f"will not help. Wait for the reset, switch model, or set "
                        f"RBI_INTEL_LLM=anthropic."
                    ) from e
                if e.code == 404:
                    raise LLMError(
                        f"Gemini model '{self.model}' not found. Check the model list in "
                        f"Google AI Studio and set RBI_INTEL_MODEL."
                    ) from e
                if e.code == 429:
                    if attempt >= self.max_retries:
                        raise LLMError(
                            f"Gemini still rate-limited after {self.max_retries} attempts."
                        ) from e
                    wait = min(_suggested_delay(msg, delay), 120.0)
                    print(
                        f"[llm] gemini 429; waiting {wait:.1f}s "
                        f"(attempt {attempt}/{self.max_retries})",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    delay = min(delay * 1.7, 120.0)
                    continue
                raise
        raise LLMError(f"Gemini request failed after {self.max_retries} attempts.")


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

class AnthropicProvider:
    name = "anthropic"
    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(self, model: str | None = None, max_retries: int = 5):
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise LLMError("anthropic is not installed.  pip install anthropic") from e
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("Set ANTHROPIC_API_KEY.")
        self.model = model or os.environ.get("RBI_INTEL_MODEL") or self.DEFAULT_MODEL
        self.max_retries = max_retries
        self._client = anthropic.Anthropic()

    def json_call(self, system_prompt, user_content, response_schema=None, max_output_tokens=1200) -> dict:
        import anthropic

        # No native JSON-schema mode: append the schema to the system prompt and
        # strip fences on the way out. Same contract, weaker enforcement.
        sys_prompt = system_prompt
        if response_schema is not None:
            sys_prompt += (
                "\n\nReturn ONLY a single JSON object conforming to this schema. "
                "No prose, no markdown fences.\n"
                + json.dumps(response_schema, indent=2)
            )

        delay = 5.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_output_tokens,
                    system=sys_prompt,
                    temperature=0.2,
                    messages=[{"role": "user", "content": user_content}],
                )
                text = "".join(b.text for b in resp.content if b.type == "text").strip()
                if not text:
                    raise LLMError(f"Anthropic returned an empty response (model={self.model}).")
                return _parse_json(text, "anthropic")

            except anthropic.RateLimitError as e:
                if attempt >= self.max_retries:
                    raise LLMError(f"Anthropic still rate-limited after {self.max_retries} attempts.") from e
                print(f"[llm] anthropic 429; waiting {delay:.1f}s "
                      f"(attempt {attempt}/{self.max_retries})", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 1.7, 120.0)
            except anthropic.APIStatusError as e:
                # A zero credit balance surfaces as 400 with a specific message,
                # not as a rate limit. It is the same class of problem as a
                # Gemini daily cap: stop, do not retry.
                if "credit balance" in str(e).lower():
                    raise QuotaExhausted(
                        "Anthropic credit balance is too low. Top up, or set "
                        "RBI_INTEL_LLM=gemini to use the free tier."
                    ) from e
                if e.status_code == 404:
                    raise LLMError(f"Anthropic model '{self.model}' not found.") from e
                raise
        raise LLMError(f"Anthropic request failed after {self.max_retries} attempts.")


# ---------------------------------------------------------------------------
# Stub — offline, deterministic, free
# ---------------------------------------------------------------------------

class StubProvider:
    """Deterministic canned responses. No network, no key, no cost.

    Used by the test suite and by `--provider stub` so the pipeline's plumbing
    can be verified independently of whether any API is reachable — which on
    the target network is a real and recurring question.
    """

    name = "stub"
    model = "stub-v1"

    def json_call(self, system_prompt, user_content, response_schema=None, max_output_tokens=1200) -> dict:
        head = user_content.strip().splitlines()[0][:60] if user_content.strip() else ""
        low = user_content.lower()

        # Shape the reply from the schema's own required keys so the stub keeps
        # working when a prompt changes.
        keys = set((response_schema or {}).get("properties", {}))

        if "skip" in keys:
            trivial = any(t in low for t in ("short title", "commencement", "definitions"))
            return {
                "skip": trivial,
                "reason": "stub: heading/definition clause" if trivial else "",
                "clause_title": f"Stub title for {head}"[:70],
                "requirement": "Stub requirement generated offline; not derived from the clause text.",
                "obligation_type": "Process",
                "branch_relevance": "Medium",
                "timeline": "",
                "keywords": ["stub", "offline", "placeholder"],
            }

        if "mapping" in keys:
            return {
                "business_area_guess": "Stub Business Area",
                "mapping": {
                    "policy": "Stub Policy", "process": "Stub Process", "control": "Stub Control",
                    "control_type": "Preventive",
                    "owner_process": "Branch Risk & Compliance Officer",
                    "owner_control": "Head — Internal Audit",
                    "evidence_required": "Stub evidence",
                },
                "assessment": {
                    "classification": "To Be Confirmed",
                    "finding": "Stub finding produced offline with no model call.",
                    "recommendation": "Replace with a real assessment.",
                    "severity": "Medium",
                },
            }

        return {"answer": "Stub answer produced offline with no model call.", "citations": []}


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_PROVIDERS = {"gemini": GeminiProvider, "anthropic": AnthropicProvider, "stub": StubProvider}


def get_provider(name: str | None = None, model: str | None = None) -> Provider:
    choice = (name or os.environ.get("RBI_INTEL_LLM") or "").strip().lower()

    if not choice:
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            choice = "gemini"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            choice = "anthropic"
        else:
            raise LLMError(
                "No LLM provider configured. Set one of:\n"
                "  GEMINI_API_KEY     free tier, aistudio.google.com/apikey\n"
                "  ANTHROPIC_API_KEY  paid\n"
                "Or run with --provider stub for an offline dry run."
            )

    if choice not in _PROVIDERS:
        raise LLMError(f"Unknown provider '{choice}'. Choose from: {', '.join(_PROVIDERS)}")

    cls = _PROVIDERS[choice]
    return cls(model=model) if choice != "stub" else cls()  # type: ignore[call-arg]


def default_sleep(provider: Provider) -> float:
    """Polite inter-call delay.

    4.5s keeps the Gemini free tier under ~13 RPM, which is where the original
    pipeline settled after hitting 429s at anything faster.
    """
    return {"gemini": 4.5, "anthropic": 0.3, "stub": 0.0}.get(provider.name, 1.0)
