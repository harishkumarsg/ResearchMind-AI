import os
import time
from dataclasses import dataclass
from typing import Optional

from groq import Groq
from dotenv import load_dotenv

from app.core.providers import (
    IncompleteGeneration,
    classify_provider_error,
    groq_client_options,
)

load_dotenv()

# Explicit timeouts and a bounded retry count; see app/core/providers.py.
_groq_client = Groq(api_key=os.environ["GROQ_API_KEY"], **groq_client_options())

GROQ_MODEL = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """
You are ResearchMind AI.

You are an academic research assistant.

RULES:

1. Use ONLY the provided context.
2. Never use external knowledge.
3. Never hallucinate facts.
4. Never invent datasets, results, methods, or conclusions.
5. If information is unavailable, respond exactly:

I could not find that information in the indexed papers.

6. Keep answers concise.
7. Avoid repetition.
8. Use bullet points when appropriate.
9. Use numbered steps for methodologies.
10. Limit answers to 3-8 sentences unless explicitly asked for details.
11. Return only the answer.
"""

#: The context budget applied when a caller does not state its own.
#:
#: This used to be a bare `context[:4000]` inside the function, applied
#: blindly after each caller had already built and capped a much larger
#: context of its own. That second, structure-blind cut is what silently
#: discarded two thirds of a report's evidence and — because Compare
#: concatenates paper 1 before paper 2 — every character of Compare's
#: second paper. The budget now belongs to the caller; this default only
#: preserves today's behaviour for callers that have not yet been given
#: one. Pass max_context_chars=None to disable truncation entirely.
DEFAULT_MAX_CONTEXT_CHARS = 4000

#: The completion budget applied when a caller does not state its own.
#:
#: 512 was enough for a one-paragraph Ask answer and hopelessly short for
#: anything with sections: the report prompt alone demands thirteen of
#: them, which is roughly 39 tokens each. That is what produced a report
#: ending mid-sentence. Like the context budget, the number now belongs to
#: the caller; this default preserves today's behaviour for callers that
#: have not been given one. It also covers reasoning tokens, which this
#: model draws from the same completion budget as visible text.
DEFAULT_MAX_TOKENS = 512

REFUSAL = "I could not find that information in the indexed papers."


@dataclass(frozen=True)
class GenerationResult:
    """One completion, with the metadata needed to tell a finished answer
    from a truncated one. `finish_reason` is the field that distinguishes
    them; without it a length-stopped completion is indistinguishable from
    a complete one, which is how a half-written report was stored as a
    finished record."""

    text: str
    finish_reason: Optional[str] = None
    completion_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None


def generate(
    question,
    context,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    max_context_chars: Optional[int] = DEFAULT_MAX_CONTEXT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> GenerationResult:
    """Generate one completion and report how it ended.

    Raises IncompleteGeneration when the model ran out of completion
    budget, so a truncated answer can never be returned as a finished one.
    A genuine "no evidence" result is kept separate: that is an empty
    completion which stopped normally, and still yields REFUSAL.
    """

    start_time = time.time()

    if max_context_chars is not None:
        context = context[:max_context_chars]

    user_prompt = f"""
CONTEXT

{context}

----------------------------------------

QUESTION

{question}

----------------------------------------

ANSWER
"""

    try:

        print("\n==========================")
        print("Generating Answer...")
        print("==========================")

        response = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)

        # `content` is None, not "", when the whole completion budget went
        # to something other than visible text.
        answer = (getattr(choice.message, "content", None) or "").strip()

        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)

        elapsed = round(time.time() - start_time, 2)
        print(
            f"Answer generated in {elapsed} sec "
            f"(finish_reason={finish_reason}, reasoning_tokens={reasoning_tokens})"
        )

        # Checked BEFORE the empty-answer branch: a completion that ran out
        # of budget is an incomplete generation even when it produced no
        # visible text, and must not be reported as "no evidence found".
        if finish_reason == "length":
            raise IncompleteGeneration(finish_reason)

        if not answer:
            return GenerationResult(
                text=REFUSAL,
                finish_reason=finish_reason,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
            )

        return GenerationResult(
            text=answer,
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
        )

    except IncompleteGeneration:
        # Already classified and neutral; re-raised unchanged so it is not
        # reported as a generic provider fault.
        raise

    except Exception as e:
        # A provider failure is re-raised, classified, rather than disguised
        # as the refusal below. Returning the refusal made a Groq timeout
        # look exactly like "no evidence found" in compare, summarize and
        # research — and /research then saved that refusal as a report.
        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            print(f"QA Agent provider failure: {provider_failure.code}")
            raise provider_failure from e

        print(f"QA Agent Error: {str(e)}")
        return GenerationResult(text=REFUSAL)


def generate_answer(
    question,
    context,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    max_context_chars: Optional[int] = DEFAULT_MAX_CONTEXT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """The answer text alone, for callers that need nothing else.

    A thin wrapper over generate(); the metadata is available there. Both
    raise IncompleteGeneration on a length-stopped completion, so neither
    can hand back a truncated answer that looks finished.
    """
    return generate(
        question,
        context,
        system_prompt=system_prompt,
        max_context_chars=max_context_chars,
        max_tokens=max_tokens,
    ).text