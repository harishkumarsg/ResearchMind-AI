"""
FastAPI dependencies that apply mechanisms A and B to an endpoint.

This is the only place the usage machinery meets the web layer, which is
why app/core/quota.py deliberately imports nothing from FastAPI: the
counter primitive cannot see a request even by accident, so no client
value can ever key a counter.

Every guard resolves the caller through the existing
get_current_owner_id, so identity remains the verified JWT `sub` and
nothing else. Overriding that dependency in tests continues to work
exactly as before.

Two guard shapes, because the right moment to charge differs:

  charge_*   — charges immediately. Correct where the endpoint's first
               action is provider work, so nothing can fail in between
               (research, summarize-paper, compare-papers).

  precheck_* — read-only check, charge later in the handler. Correct for
               /ask-stream, where a 429 is impossible once the response
               has started streaming, and for /index-document, which must
               first learn whether there is anything to index and whether
               the indexing slot is free.
"""
from typing import Callable

from fastapi import Depends

from app.core import limits as limits_config
from app.core import quota
from app.core.auth import get_current_owner_id
from app.core.rate_limit import enforce_burst


def _guard(metric: str, *, charge: bool) -> Callable[..., str]:
    def dependency(owner_id: str = Depends(get_current_owner_id)) -> str:
        # A first: a burst rejection is cheaper than a counter round trip,
        # and it must not consume daily allowance.
        enforce_burst(owner_id, metric)
        if charge:
            quota.charge(owner_id, metric)
        else:
            quota.ensure_within_quota(owner_id, metric)
        return owner_id

    return dependency


def charge_ai_unit(owner_id: str) -> None:
    """Consumes one AI generation unit, called from inside a handler at the
    last moment before its first provider call.

    Deliberately not a dependency: a dependency runs before the handler
    body, which would bill deterministic application failures — an unknown
    paper, an empty selection — that never cost anything upstream. The
    matching read-only pre-check (precheck_ai_generation) has already
    turned away the ordinary "out of allowance" case with a real 429, so
    reaching here and being refused means a concurrent request took the
    last unit.
    """
    quota.charge(owner_id, limits_config.AI_GENERATION)


#: Charges one AI generation up front. Retained for endpoints whose first
#: statement is a provider call; no route uses it today.
charge_ai_generation = _guard(limits_config.AI_GENERATION, charge=True)

#: Checks the AI allowance without consuming it. /ask-stream charges
#: inside its generator, immediately before the first provider call.
precheck_ai_generation = _guard(limits_config.AI_GENERATION, charge=False)

#: Checks the indexing allowance without consuming it. /index-document
#: charges only once it knows a run will actually start.
precheck_index_run = _guard(limits_config.INDEX_RUN, charge=False)

#: Charges one query embedding up front. /search and /paper-details both
#: call encode_query() as their FIRST statement, so a dependency that
#: charges is the correct shape here: the rejection happens before the
#: handler body runs, and therefore before any Voyage request. Exactly
#: one charge per request — the handlers never charge again.
charge_query_embedding = _guard(limits_config.QUERY_EMBEDDING, charge=True)

#: Checks the upload allowance without consuming it. /upload validates
#: the filename, the PDF magic bytes and the size cap before doing any
#: external work, and those deterministic rejections must not cost a
#: unit; charge_upload_unit() is called later, once the request is known
#: to be heading for Storage.
precheck_upload = _guard(limits_config.UPLOAD, charge=False)

#: Burst guard for cheap authenticated reads. CHEAP_READ has no per-day
#: limit, so quota.charge() returns immediately without touching the
#: database and mechanism A does the real work.
guard_cheap_read = _guard(limits_config.CHEAP_READ, charge=True)


def charge_upload_unit(owner_id: str) -> None:
    """Consumes one upload unit, immediately before the Storage write.

    The same reasoning as charge_ai_unit: a dependency would bill a
    missing filename, a non-PDF or an oversized file — deterministic
    rejections that never reach Supabase Storage and cost nothing
    externally. precheck_upload has already turned away the ordinary
    "out of allowance" case with a real 429.
    """
    quota.charge(owner_id, limits_config.UPLOAD)
