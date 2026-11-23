"""
What may be logged about a failed database write: the exception's type.

Never its message. SQLAlchemy's text for a DBAPIError carries the SQL
statement and its bound parameters, and the driver's own message names
tables, columns, constraints and values (a foreign-key violation quotes the
offending owner id). So the log line holds a fixed operation name, chosen by
the caller, and a type name: for a DBAPIError such as IntegrityError, the
type of the driver exception it wraps (`error.orig`, e.g. psycopg's
ForeignKeyViolation), which says what kind of fault it was.
"""
import logging

from sqlalchemy.exc import DBAPIError


def database_failure_type(exc: BaseException) -> str:
    """The driver exception's type name for a DBAPIError, else the
    exception's own type name."""
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        return type(exc.orig).__name__
    return type(exc).__name__


def log_database_failure(logger: logging.Logger, operation: str, exc: BaseException) -> None:
    """One warning line per failed database write. `operation` must be a
    fixed literal chosen by the caller, never request data."""
    logger.warning("database %s failed: type=%s", operation, database_failure_type(exc))
