from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ====================================
# Core APIs
# ====================================

from app.api.upload import router as upload_router
from app.api.index_document import router as index_router
from app.api.search import router as search_router
from app.api.ask import router as ask_router
from app.api.ask_stream import router as ask_stream_router

# ====================================
# Research APIs
# ====================================

from app.api.summarize_paper import (
    router as summarize_router
)

from app.api.compare_papers import (
    router as compare_router
)

from app.api.research import (
    router as research_router
)

from app.api.paper_details import (
    router as paper_details_router
)

from app.api.export_report import (
    router as export_router
)

from app.api.stats import (
    router as stats_router
)

from app.api.papers import (
    router as papers_router
)

from app.api.delete_paper import (
    router as delete_paper_router
)

# ====================================
# FastAPI App
# ====================================

app = FastAPI(
    title="ResearchMind AI",
    description="""
ResearchMind AI

AI-powered academic research platform with:

• PDF Upload
• Automatic Paper Indexing
• Semantic Search
• Research Q&A
• Literature Review Generation
• Paper Comparison
• Report Export
• Citation Support
• Qdrant Vector Search
• Ollama Local LLM
""",
    version="3.1.0"
)

# ====================================
# CORS
# ====================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        # Vercel deployment — update with your actual domain after deploy
        "https://researchmind-ai.vercel.app",
        # Allow all vercel preview URLs
        "https://*.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====================================
# Core Routes
# ====================================

app.include_router(
    upload_router,
    tags=["Upload"]
)

app.include_router(
    index_router,
    tags=["Indexing"]
)

app.include_router(
    search_router,
    tags=["Search"]
)

app.include_router(
    ask_router,
    tags=["Ask AI"]
)

app.include_router(
    ask_stream_router,
    tags=["Ask AI Stream"]
)

# ====================================
# Research Routes
# ====================================

app.include_router(
    summarize_router,
    tags=["Summarization"]
)

app.include_router(
    compare_router,
    tags=["Comparison"]
)

app.include_router(
    research_router,
    tags=["Research"]
)

app.include_router(
    paper_details_router,
    tags=["Paper Details"]
)

app.include_router(
    export_router,
    tags=["Export"]
)

app.include_router(
    stats_router,
    tags=["Stats"]
)

app.include_router(
    papers_router,
    tags=["Papers"]
)

app.include_router(
    delete_paper_router,
    tags=["Delete Paper"]
)

# ====================================
# Health Check
# ====================================

@app.get("/health")
def health_check():

    return {
        "status": "healthy",
        "service": "ResearchMind AI",
        "version": "3.1.0"
    }

# ====================================
# Root Endpoint
# ====================================

@app.get("/")
def root():

    return {
        "message":
        "ResearchMind AI Running Successfully",

        "version":
        "3.1.0",

        "frontend":
        "http://localhost:8080",

        "docs":
        "http://127.0.0.1:8000/docs",

        "features": [

            "PDF Upload",

            "Research Paper Indexing",

            "Semantic Search",

            "Question Answering",

            "Follow-Up Memory",

            "Cross Encoder Reranking",

            "Paper Summarization",

            "Paper Comparison",

            "Research Report Generation",

            "Paper Details Extraction",

            "PDF Report Export",

            "Vector Database (Qdrant)",

            "Ollama LLM Integration",

            "Local LLM Research Assistant",

            "Citation Tracking",

            "Page Level Sources"
        ]
    }