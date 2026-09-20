"""
GET /latest-report — the owner's most recent stored report, as JSON.

/research already writes a durable Report row and returns the report in
the same response, but the Reports page held that response in component
state alone. A browser refresh therefore showed an empty form while the
row sat in Postgres untouched: a restoration gap, never a persistence
failure. This endpoint is the missing read.

It returns what was already stored and nothing else. It deliberately
does NOT generate: restoring a page by re-running /research would bill a
generation, add roughly half a minute of latency and INSERT a duplicate
row on every refresh, which is exactly why the restore path is a
separate read-only route rather than a second call to /research.

Reading is not an AI operation, so this route carries no usage guard —
it is the same kind of owner-scoped read as /papers and /stats.
"""
import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_owner_id
from app.core.usage_guard import guard_cheap_read
from app.db.models import Report
from app.db.session import get_db_session

router = APIRouter()


@router.get("/latest-report")
def latest_report(
    response: Response,
    owner_id: str = Depends(get_current_owner_id),
    _cheap_read: str = Depends(guard_cheap_read),
    db: Session = Depends(get_db_session),
):
    # owner_id is the verified JWT `sub`, never a query parameter or
    # request body, so a caller cannot read another owner's report. This
    # is the same filter /export-report applies to the same table.
    report_row = (
        db.query(Report)
        .filter(Report.owner_id == uuid.UUID(owner_id))
        .order_by(Report.created_at.desc())
        .first()
    )

    if report_row is None:
        # An account that has not generated anything yet is an ordinary
        # empty state, not a failure. 404 keeps it distinguishable from a
        # stored report whose markdown happens to be empty, and the body
        # names no table, column or driver.
        response.status_code = status.HTTP_404_NOT_FOUND

        return {
            "status": "empty",
            "message": "No report has been generated yet."
        }

    return {
        "status": "success",

        "query": report_row.query,

        "report_markdown": report_row.report_markdown,

        "citations": report_row.citations or []
    }
