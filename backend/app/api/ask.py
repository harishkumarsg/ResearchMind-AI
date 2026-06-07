from fastapi import APIRouter
import time

from app.rag.embedder import model
from app.rag.vector_store import client
from app.rag.reranker import rerank_results

from app.agents.qa_agent import generate_answer

from app.memory import chat_history
import app.memory as memory

router = APIRouter()

COLLECTION_NAME = "researchmind"

FOLLOW_UP_WORDS = [
    "it",
    "they",
    "them",
    "this",
    "that",
    "these",
    "those",
    "its",
    "their",
    "algorithm",
    "hardware",
    "method",
    "approach",
    "system",
    "work",
    "works",
    "used",
    "use",
    "implementation",
    "architecture",
    "module",
    "sensor",
    "communication",
    "encryption",
    "signature",
    "detection",
    "anomaly",
    "security",
    "advantages",
    "limitations",
    "benefits",
    "future scope",
    "future work",
    "dataset",
    "results",
    "accuracy",
    "performance",
]

SEARCH_LIMIT = 15
TOP_CHUNKS = 5
MAX_CONTEXT = 4000


@router.get("/ask")
def ask(question: str):

    try:

        total_start = time.time()

        question_lower = question.lower()

        # ----------------------------------
        # Follow-up Detection
        # ----------------------------------

        is_followup = False

        if chat_history:

            is_followup = any(
                word in question_lower
                for word in FOLLOW_UP_WORDS
            )

        # ----------------------------------
        # Topic Aware Query
        # ----------------------------------

        search_query = question

        if (
            is_followup
            and getattr(memory, "current_topic", "")
        ):

            search_query = (
                memory.current_topic
                + " "
                + question
            )

        # ----------------------------------
        # Embedding
        # ----------------------------------

        embed_start = time.time()

        query_vector = model.encode(
            search_query
        )

        print(
            f"Embedding Time: "
            f"{round(time.time() - embed_start, 2)} sec"
        )

        # ----------------------------------
        # Vector Search
        # ----------------------------------

        search_start = time.time()

        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector.tolist(),
            limit=SEARCH_LIMIT
        ).points

        print(
            f"Vector Search Time: "
            f"{round(time.time() - search_start, 2)} sec"
        )

        if not results:

            return {
                "status": "error",
                "message": "No relevant papers found."
            }

        # ----------------------------------
        # Reranking
        # ----------------------------------

        rerank_start = time.time()

        ranked_results = rerank_results(
            search_query,
            results
        )

        print(
            f"Rerank Time: "
            f"{round(time.time() - rerank_start, 2)} sec"
        )

        # ----------------------------------
        # Top Results
        # ----------------------------------

        filtered_results = ranked_results[:TOP_CHUNKS]

        # ----------------------------------
        # Follow-up Paper Lock
        # ----------------------------------

        if (
            is_followup
            and getattr(memory, "current_paper", "")
        ):

            locked_results = [

                hit

                for hit in filtered_results

                if hit.payload.get(
                    "paper",
                    ""
                )
                == memory.current_paper

            ]

            if locked_results:

                filtered_results = locked_results

        # ----------------------------------
        # First Query Focus
        # ----------------------------------

        elif filtered_results:

            best_paper = filtered_results[0].payload.get(
                "paper",
                ""
            )

            filtered_results = [

                hit

                for hit in filtered_results

                if hit.payload.get(
                    "paper",
                    ""
                )
                == best_paper

            ]

        # ----------------------------------
        # Remove Duplicate Chunks
        # ----------------------------------

        unique_chunks = []
        seen_chunks = set()

        for hit in filtered_results:

            chunk_id = hit.payload.get(
                "chunk_id",
                ""
            )

            if chunk_id in seen_chunks:
                continue

            seen_chunks.add(chunk_id)

            unique_chunks.append(hit)

        filtered_results = unique_chunks

        # ----------------------------------
        # Build Context
        # ----------------------------------

        context = "\n\n".join(

            hit.payload.get(
                "text",
                ""
            )

            for hit in filtered_results
        )

        context = context[:MAX_CONTEXT]

        # ----------------------------------
        # Save Topic Context
        # ----------------------------------

        if len(context) > 100:

            memory.topic_context = context

        # ----------------------------------
        # History
        # ----------------------------------

        history_text = "\n".join(
            chat_history[-6:]
        )

        full_context = f"""
Conversation History:

{history_text}

Previous Topic Context:

{getattr(memory, 'topic_context', '')}

Current Retrieved Context:

{context}
"""

        # ----------------------------------
        # LLM
        # ----------------------------------

        llm_start = time.time()

        answer = generate_answer(
            question,
            full_context
        )

        print(
            f"LLM Time: "
            f"{round(time.time() - llm_start, 2)} sec"
        )

        # ----------------------------------
        # Citations
        # ----------------------------------

        citations = []

        seen = set()

        for hit in filtered_results:

            payload = hit.payload

            key = (
                payload.get("paper", ""),
                payload.get("page", "")
            )

            if key in seen:
                continue

            seen.add(key)

            citations.append(
                {
                    "paper":
                    payload.get(
                        "paper",
                        ""
                    ),

                    "source":
                    payload.get(
                        "source",
                        ""
                    ),

                    "page":
                    payload.get(
                        "page",
                        ""
                    ),

                    "paper_id":
                    payload.get(
                        "paper_id",
                        ""
                    )
                }
            )

        # ----------------------------------
        # Memory
        # ----------------------------------

        chat_history.append(
            f"User: {question}"
        )

        chat_history.append(
            f"Assistant: {answer}"
        )

        if len(chat_history) > 20:

            del chat_history[:-20]

        memory.last_question = question
        memory.last_answer = answer

        if not is_followup:

            memory.current_topic = question

        if filtered_results:

            memory.current_paper = (
                filtered_results[0]
                .payload
                .get(
                    "paper",
                    ""
                )
            )

        print(
            f"TOTAL TIME: "
            f"{round(time.time() - total_start, 2)} sec"
        )

        # ----------------------------------
        # Response
        # ----------------------------------

        return {

            "status":
            "success",

            "question":
            question,

            "search_query":
            search_query,

            "is_followup":
            is_followup,

            "current_topic":
            getattr(
                memory,
                "current_topic",
                ""
            ),

            "current_paper":
            getattr(
                memory,
                "current_paper",
                ""
            ),

            "answer":
            answer,

            "citations":
            citations,

            "sources": [

                {

                    "score":
                    float(
                        getattr(
                            hit,
                            "score",
                            0.0
                        )
                    ),

                    "paper":
                    hit.payload.get(
                        "paper",
                        ""
                    ),

                    "source":
                    hit.payload.get(
                        "source",
                        ""
                    ),

                    "page":
                    hit.payload.get(
                        "page",
                        ""
                    ),

                    "chunk_id":
                    hit.payload.get(
                        "chunk_id",
                        ""
                    ),

                    "text":
                    hit.payload.get(
                        "text",
                        ""
                    )[:300]

                }

                for hit in filtered_results
            ],

            "memory_size":
            len(chat_history)
        }

    except Exception as e:

        print(
            f"Ask Error: {str(e)}"
        )

        return {

            "status":
            "error",

            "message":
            str(e)
        }