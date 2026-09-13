from fastapi import APIRouter, Depends, Response

from sqlalchemy.orm import Session

from app.core.auth import get_current_owner_id
from app.db.models import Report
from app.db.session import get_db_session

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

import io
import re
import uuid

router = APIRouter()


@router.get("/export-report")
def export_report(
    owner_id: str = Depends(get_current_owner_id),
    db: Session = Depends(get_db_session)
):

    try:

        # ==================================
        # Load Report — the owner's most recent row in Postgres
        #
        # The filter uses owner_id from get_current_owner_id, i.e. the
        # verified JWT `sub`. No client-supplied value reaches this query,
        # and the route exposes no report identifier at all, so a caller
        # cannot name — let alone read — another user's report. A report
        # belonging to someone else simply is not in this result set.
        #
        # This replaces the previous in-process memory read plus
        # latest_report_<owner>.txt / latest_sources_<owner>.txt fallback.
        # Both were lost on restart and on Render's ephemeral disk; the
        # row is the durable record written by /research.
        # ==================================

        report_row = (
            db.query(Report)
            .filter(Report.owner_id == uuid.UUID(owner_id))
            .order_by(Report.created_at.desc())
            .first()
        )

        report = report_row.report_markdown if report_row else ""

        citations = (report_row.citations or []) if report_row else []

        query = report_row.query if report_row else ""

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

        # Built entirely in memory. Previously this wrote
        # ResearchMind_Report_<owner>.pdf next to the process and served
        # it from there, which left one file per user accumulating on
        # disk — a disk that is ephemeral on Render anyway, so the file
        # was never a durable artifact, only litter. reportlab accepts
        # any binary file-like object, so a BytesIO needs no other change.
        buffer = io.BytesIO()

        doc = SimpleDocTemplate(
            buffer,
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

        # ==================================
        # Return PDF — bytes straight from the buffer, nothing on disk
        # ==================================

        return Response(
            content=buffer.getvalue(),
            media_type="application/pdf",
            headers={
                "Content-Disposition":
                'attachment; filename="ResearchMind_Report.pdf"'
            }
        )

    except Exception as e:

        return {

            "status": "error",

            "message": str(e)
        }