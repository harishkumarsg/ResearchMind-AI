import os
import time
from groq import Groq
from dotenv import load_dotenv

from app.core.providers import classify_provider_error, groq_client_options

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


def generate_answer(question, context):

    start_time = time.time()

    context = context[:4000]

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
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=512,
        )

        answer = response.choices[0].message.content.strip()

        elapsed = round(time.time() - start_time, 2)
        print(f"Answer generated in {elapsed} sec")

        if not answer:
            return "I could not find that information in the indexed papers."

        return answer

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
        return "I could not find that information in the indexed papers."