# RESEARCHMIND AI — MASTER PROJECT CONTEXT

## PROJECT IDENTITY

Project Name: ResearchMind AI

ResearchMind AI is an AI-native academic research workspace designed for researchers, master's students, PhD students, professors, research labs, and R&D teams.

The platform allows users to:

* Upload research papers
* Build private research libraries
* Index papers into a vector database
* Perform semantic search
* Ask grounded questions
* Compare papers
* Generate literature reviews
* Generate research reports
* Export publication-ready documents

Core Product Vision:

"Perplexity + Elicit + NotebookLM for academic research."

The system follows a Retrieval-Augmented Generation (RAG) architecture.

---

# CURRENT DEVELOPMENT PHASE

Current Phase:

Frontend–Backend Integration

The backend is functional.

The frontend exists but several pages still use mock data and require API integration.

Primary objective:

Transform the current project into a fully working end-to-end academic research platform.

---

# TECHNOLOGY STACK

## Backend

Python

FastAPI

Qdrant Vector Database

Sentence Transformers

Cross Encoder Reranker

Ollama

Qwen 2.5 3B

PDF Processing Pipeline

RAG Architecture

## Frontend

React

TypeScript

TanStack Router

TanStack Start

Tailwind CSS

Lucide Icons

React Query (recommended)

## AI Components

Embedding Model:

BAAI/bge-small-en-v1.5

Reranker:

cross-encoder/ms-marco-MiniLM-L-6-v2

LLM:

qwen2.5:3b

Inference:

Local Ollama

---

# SYSTEM ARCHITECTURE

User Uploads PDF

↓

PDF Storage

uploads/papers/

↓

PDF Parsing

↓

Text Cleaning

↓

Chunking

↓

Embedding Generation

↓

Qdrant Vector Storage

↓

Semantic Search

↓

Cross Encoder Reranking

↓

Context Assembly

↓

Ollama (Qwen)

↓

Grounded Response

---

# CURRENT FOLDER STRUCTURE

Backend

backend/

├── app/

│   ├── api/

│   │   ├── upload.py

│   │   ├── index_document.py

│   │   ├── search.py

│   │   ├── ask.py

│   │   ├── summarize_paper.py

│   │   ├── compare_papers.py

│   │   ├── research.py

│   │   ├── paper_details.py

│   │   └── export_report.py

│   ├── rag/

│   │   ├── embedder.py

│   │   ├── reranker.py

│   │   ├── chunker.py

│   │   └── vector_store.py

│   ├── agents/

│   │   └── qa_agent.py

│   ├── services/

│   └── main.py

Frontend

src/

├── routes/

│   ├── dashboard.tsx

│   ├── search.tsx

│   ├── ask.tsx

│   ├── reports.tsx

│   ├── compare.tsx

│   └── upload.tsx

├── components/

│   └── app-shell.tsx

├── lib/

└── routeTree.gen.ts

---

# BACKEND STATUS

## Upload API

File:

backend/app/api/upload.py

Endpoint:

POST /upload

Purpose:

Store uploaded PDF files.

Status:

WORKING

Response:

{
"message": "PDF uploaded successfully",
"filename": "...",
"saved_path": "..."
}

---

## Indexing API

File:

backend/app/api/index_document.py

Endpoint:

GET /index-document

Purpose:

Create chunks, embeddings, and upload vectors to Qdrant.

Status:

WORKING

---

## Search API

File:

backend/app/api/search.py

Endpoint:

GET /search?query=...

Purpose:

Semantic search.

Status:

WORKING

Response Structure:

{
"status": "success",
"query": "...",
"results": [
{
"paper": "...",
"authors": "...",
"abstract": "...",
"text": "...",
"page": ...
}
]
}

---

## Ask API

File:

backend/app/api/ask.py

Endpoint:

GET /ask?question=...

Purpose:

Grounded RAG Question Answering

Status:

WORKING

Response Structure:

{
"status": "success",
"answer": "...",
"citations": [...],
"sources": [...]
}

---

## Research APIs

Implemented:

summarize_paper.py

compare_papers.py

research.py

paper_details.py

export_report.py

Status:

BACKEND COMPLETE

Frontend Integration Required

---

# PERFORMANCE

Current Performance

Embedding:

~0.15 sec

Vector Search:

~0.06 sec

Reranking:

~0.65 sec

LLM:

~25 sec

Total:

~26 sec

Current Model:

qwen2.5:3b

Inference:

CPU

Target:

Search < 1 sec

Answer Generation < 10 sec

Upload + Index < 30 sec

---

# FRONTEND STATUS

Completed:

dashboard.tsx

search.tsx

ask.tsx

reports.tsx

compare.tsx

app-shell.tsx

upload.tsx

Current State:

Mostly functional UI

Some pages still use mock data

Requires backend integration

---

# COMPLETED FEATURES

✓ PDF Upload

✓ PDF Indexing

✓ Embedding Pipeline

✓ Vector Database

✓ Semantic Search

✓ Cross Encoder Reranking

✓ Ask AI

✓ Citation Generation

✓ Follow-up Memory

✓ Topic Tracking

✓ Paper Tracking

✓ Literature Review Backend

✓ Report Generation Backend

✓ Comparison Backend

✓ Export Backend

---

# REMAINING FEATURES

Priority 1

Upload UI

* Drag and drop
* Multi-file upload
* Upload progress
* Error handling

Priority 2

Auto Indexing

Current:

Upload

↓

Manual Index

Target:

Upload

↓

Auto Index

↓

Ready

Priority 3

Paper Details Page

Route:

/paper/$paperId

Features:

* Metadata
* Abstract
* Keywords
* Pages
* Ask about this paper

Priority 4

Dashboard Integration

Show:

* Total papers
* Total pages
* Total chunks
* Recent uploads

Priority 5

Reports Integration

Connect:

research.py

export_report.py

Priority 6

Compare Integration

Connect:

compare_papers.py

Priority 7

Library Management

* View papers
* Delete papers
* Re-index papers
* View metadata

---

# KNOWN ISSUES

1. Upload route may not appear until routeTree.gen.ts regenerates.

2. Ollama currently runs CPU-only.

3. Ask AI responses still take ~25 seconds.

4. No streaming responses yet.

5. Dashboard still partially uses static data.

6. Reports page not connected.

7. Compare page not connected.

---

# CODING RULES

When generating code:

1. Preserve existing architecture.

2. Do not rewrite working backend systems.

3. Do not replace Qdrant.

4. Do not replace Ollama.

5. Do not introduce new frameworks.

6. Maintain FastAPI compatibility.

7. Maintain TanStack Router compatibility.

8. Use TypeScript.

9. Use Tailwind CSS.

10. Use reusable components.

11. Prefer production-ready implementations.

12. Never provide placeholder code.

13. Never omit imports.

14. Always provide complete files when requested.

15. Explicitly state:

* Files to create
* Files to modify
* Reason for changes

16. If modifying existing code:

* Preserve working functionality
* Minimize breaking changes

17. Assume backend APIs already exist unless explicitly told otherwise.

18. Optimize for maintainability and performance.

---

# OUTPUT FORMAT REQUIREMENT

For every task:

Step 1:
Explain the change.

Step 2:
List files to modify.

Step 3:
List files to create.

Step 4:
Provide complete code.

Step 5:
Explain how to test.

Never provide partial snippets unless specifically requested.

Act as a senior staff-level full-stack engineer helping complete ResearchMind AI into a production-ready academic RAG platform.
