from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import json
import os
import time

from groq import Groq
from dotenv import load_dotenv

from app.rag.embedder import model
from app.rag.vector_store import client
from app.rag.reranker import rerank_results
from app.memory import chat_history
import app.memory as memory

load_dotenv()
_groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
GROQ_MODEL = "llama-3.1-8b-instant"

router = APIRouter()

COLLECTION_NAME = "researchmind"
SEARCH_LIMIT = 8
TOP_CHUNKS = 4
MAX_CONTEXT = 4000

FOLLOW_UP_WORDS = [
    "it", "they", "them", "this", "that", "these", "those",
    "its", "their", "algorithm", "hardware", "method", "approach",
    "system", "work", "works", "used", "use", "implementation",
    "architecture", "dataset", "results", "accuracy", "performance",
    "advantages", "limitations", "future scope", "future work",
]

SYSTEM_PROMPT = """
You are ResearchMind AI.

You are an academic research assistant.

RULES:

1. Use ONLY the provided context.
2. Never use external knowledge.
3. Never hallucinate facts.
4. If information is unavailable, respond exactly:

I could not find that information in the indexed papers.

5. Keep answers concise.
6. Use bullet points when appropriate.
7. Limit answers to 3-8 sentences unless explicitly asked for details.
8. Return only the answer.
"""


def generate_sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@router.get("/ask-stream")
def ask_stream(question: str):

    def stream():

        try:

            question_lower = question.lower()

            # Follow-up detection
            is_followup = bool(chat_history) and any(
                word in question_lower for word in FOLLOW_UP_WORDS
            )

            search_query = question
            if is_followup and getattr(memory, "current_topic", ""):
                search_query = memory.current_topic + " " + question

            # Emit: searching
            yield generate_sse_event({"type": "status", "text": "Searching papers…"})

            query_vector = model.encode(search_query)

            results = client.query_points(
                collection_name=COLLECTION_NAME,
                query=query_vector.tolist(),
                limit=SEARCH_LIMIT,
            ).points

            if not results:
                yield generate_sse_event({
                    "type": "error",
                    "text": "No relevant papers found. Make sure you have indexed papers first."
                })
                return

            # Emit: reranking
            yield generate_sse_event({"type": "status", "text": "Reranking results…"})

            ranked = rerank_results(search_query, results)
            filtered = ranked[:TOP_CHUNKS]

            # Paper lock for follow-ups
            if is_followup and getattr(memory, "current_paper", ""):
                locked = [h for h in filtered if h.payload.get("paper") == memory.current_paper]
                if locked:
                    filtered = locked
            elif filtered:
                best_paper = filtered[0].payload.get("paper", "")
                filtered = [h for h in filtered if h.payload.get("paper") == best_paper]

            # Dedupe
            unique, seen = [], set()
            for h in filtered:
                cid = h.payload.get("chunk_id", "")
                if cid not in seen:
                    seen.add(cid)
                    unique.append(h)
            filtered = unique

            context = "\n\n".join(h.payload.get("text", "") for h in filtered)
            context = context[:MAX_CONTEXT]

            if len(context) > 100:
                memory.topic_context = context

            history_text = "\n".join(chat_history[-6:])
            full_context = f"""Conversation History:\n{history_text}\n\nPrevious Topic Context:\n{getattr(memory, 'topic_context', '')}\n\nCurrent Retrieved Context:\n{context}"""

            user_prompt = f"CONTEXT\n\n{full_context}\n\n----------------------------------------\n\nQUESTION\n\n{question}\n\n----------------------------------------\n\nANSWER\n"

            # Emit: generating
            yield generate_sse_event({"type": "status", "text": "Generating answer…"})

            # Stream tokens from Groq
            answer_parts = []

            stream_response = _groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=512,
                stream=True,
            )

            for chunk in stream_response:
                token = chunk.choices[0].delta.content
                if token:
                    answer_parts.append(token)
                    yield generate_sse_event({"type": "token", "text": token})

            full_answer = "".join(answer_parts).strip()

            # Build citations
            citations = []
            seen_keys = set()
            for hit in filtered:
                p = hit.payload
                key = (p.get("paper", ""), p.get("page", ""))
                if key not in seen_keys:
                    seen_keys.add(key)
                    citations.append({
                        "paper": p.get("paper", ""),
                        "source": p.get("source", ""),
                        "page": p.get("page", ""),
                        "paper_id": p.get("paper_id", ""),
                    })

            # Update memory
            chat_history.append(f"User: {question}")
            chat_history.append(f"Assistant: {full_answer}")
            if len(chat_history) > 20:
                del chat_history[:-20]

            memory.last_question = question
            memory.last_answer = full_answer
            if not is_followup:
                memory.current_topic = question
            if filtered:
                memory.current_paper = filtered[0].payload.get("paper", "")

            # Emit: done with citations
            yield generate_sse_event({
                "type": "done",
                "citations": citations,
                "sources": citations,
                "is_followup": is_followup,
            })

        except Exception as e:
            yield generate_sse_event({"type": "error", "text": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
