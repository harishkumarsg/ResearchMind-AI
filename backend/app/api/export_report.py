from fastapi import APIRouter
from fastapi.responses import FileResponse

from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    PageBreak
)

from reportlab.lib.styles import (
    getSampleStyleSheet,
    ParagraphStyle
)

from reportlab.lib.enums import TA_CENTER

from reportlab.lib.pagesizes import letter

from datetime import datetime

import os
import re

import app.memory as memory

router = APIRouter()


@router.get("/export-report")
def export_report():

    try:

        # ==================================
        # Load Report
        # ==================================

        report = getattr(
            memory,
            "last_research_report",
            ""
        )

        citations = getattr(
            memory,
            "last_citations",
            []
        )

        query = getattr(
            memory,
            "last_research_query",
            ""
        )

        # ==================================
        # Fallback Report
        # ==================================

        if not report:

            if os.path.exists(
                "latest_report.txt"
            ):

                with open(
                    "latest_report.txt",
                    "r",
                    encoding="utf-8"
                ) as f:

                    report = f.read()

        # ==================================
        # Fallback Citations
        # ==================================

        if not citations:

            if os.path.exists(
                "latest_sources.txt"
            ):

                citations = []

                current = {}

                with open(
                    "latest_sources.txt",
                    "r",
                    encoding="utf-8"
                ) as f:

                    for line in f:

                        line = line.strip()

                        if line.startswith(
                            "Paper:"
                        ):

                            current["paper"] = (
                                line.replace(
                                    "Paper:",
                                    ""
                                ).strip()
                            )

                        elif line.startswith(
                            "Source:"
                        ):

                            current["source"] = (
                                line.replace(
                                    "Source:",
                                    ""
                                ).strip()
                            )

                        elif line.startswith(
                            "Page:"
                        ):

                            current["page"] = (
                                line.replace(
                                    "Page:",
                                    ""
                                ).strip()
                            )

                            citations.append(
                                current.copy()
                            )

                            current = {}

        # ==================================
        # Validation
        # ==================================

        if not report:

            return {

                "status": "error",

                "message":
                "No research report found. Run /research first."
            }

        # ==================================
        # Remove AI References
        # ==================================

        report = re.sub(
            r"(#+\s*)?references.*",
            "",
            report,
            flags=re.IGNORECASE | re.DOTALL
        )

        # ==================================
        # PDF Setup
        # ==================================

        pdf_path = "ResearchMind_Report.pdf"

        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=letter,
            leftMargin=40,
            rightMargin=40,
            topMargin=40,
            bottomMargin=40
        )

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "TitleStyle",
            parent=styles["Title"],
            alignment=TA_CENTER
        )

        subtitle_style = ParagraphStyle(
            "SubtitleStyle",
            parent=styles["BodyText"],
            alignment=TA_CENTER
        )

        content = []

        # ==================================
        # Cover Section
        # ==================================

        content.append(
            Paragraph(
                "ResearchMind AI Research Report",
                title_style
            )
        )

        content.append(
            Spacer(1, 20)
        )

        if query:

            content.append(
                Paragraph(
                    f"<b>Research Topic:</b> {query}",
                    subtitle_style
                )
            )

            content.append(
                Spacer(1, 8)
            )

        content.append(
            Paragraph(
                f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                subtitle_style
            )
        )

        content.append(
            Spacer(1, 25)
        )

        # ==================================
        # Report Body
        # ==================================

        for line in report.split("\n"):

            line = line.strip()

            if not line:
                continue

            line = line.replace(
                "&",
                "&amp;"
            )

            formatted_line = re.sub(
                r"\*\*(.*?)\*\*",
                r"<b>\1</b>",
                line
            )

            # ------------------------------
            # Heading
            # ------------------------------

            if line.startswith("###"):

                heading = (
                    line
                    .replace(
                        "###",
                        ""
                    )
                    .strip()
                )

                content.append(
                    Paragraph(
                        heading,
                        styles["Heading2"]
                    )
                )

                content.append(
                    Spacer(1, 8)
                )

                continue

            # ------------------------------
            # Bullet
            # ------------------------------

            if (
                line.startswith("-")
                or
                line.startswith("*")
            ):

                cleaned = (
                    line
                    .lstrip("-*")
                    .strip()
                )

                cleaned = re.sub(
                    r"\*\*(.*?)\*\*",
                    r"<b>\1</b>",
                    cleaned
                )

                content.append(
                    Paragraph(
                        f"• {cleaned}",
                        styles["BodyText"]
                    )
                )

                content.append(
                    Spacer(1, 4)
                )

                continue

            # ------------------------------
            # Paragraph
            # ------------------------------

            content.append(
                Paragraph(
                    formatted_line,
                    styles["BodyText"]
                )
            )

            content.append(
                Spacer(1, 6)
            )

        # ==================================
        # References
        # ==================================

        if citations:

            content.append(
                PageBreak()
            )

            content.append(
                Paragraph(
                    "References",
                    styles["Heading1"]
                )
            )

            content.append(
                Spacer(1, 12)
            )

            grouped_refs = {}

            for item in citations:

                paper = (
                    item.get(
                        "paper",
                        "Unknown Paper"
                    )
                    .replace("_", " ")
                    .strip()
                )

                source = item.get(
                    "source",
                    ""
                )

                page = str(
                    item.get(
                        "page",
                        ""
                    )
                )

                if paper not in grouped_refs:

                    grouped_refs[paper] = {

                        "source": source,

                        "pages": set()
                    }

                if page:
                    grouped_refs[
                        paper
                    ]["pages"].add(
                        page
                    )

            sorted_papers = sorted(
                grouped_refs.keys()
            )

            for index, paper in enumerate(
                sorted_papers,
                start=1
            ):

                data = grouped_refs[
                    paper
                ]

                pages = sorted(
                    list(
                        data["pages"]
                    ),
                    key=lambda x:
                    int(x)
                    if x.isdigit()
                    else 9999
                )

                page_text = ", ".join(
                    pages
                )

                ref_text = (
                    f"<b>[{index}]</b> "
                    f"{paper}<br/>"
                    f"Source: {data['source']}<br/>"
                    f"Pages Referenced: {page_text}"
                )

                content.append(
                    Paragraph(
                        ref_text,
                        styles["BodyText"]
                    )
                )

                content.append(
                    Spacer(1, 10)
                )

        # ==================================
        # Build PDF
        # ==================================

        doc.build(content)

        memory.last_exported_file = pdf_path

        # ==================================
        # Return PDF
        # ==================================

        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename="ResearchMind_Report.pdf"
        )

    except Exception as e:

        return {

            "status": "error",

            "message": str(e)
        }