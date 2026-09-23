import { useMemo } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { AlertCircle, FileText, Loader2, Columns2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  getPaperIntelligence,
  getPapersDetailed,
  INTELLIGENCE_SECTION_ORDER,
  type IntelligenceEvidence,
  type IntelligenceSection,
  type IntelligenceSectionName,
  type PaperDetail,
  type PaperIntelligenceResult,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { queryKeys } from "@/lib/query-keys";

/**
 * Two papers' PERSISTED Paper Intelligence, side by side.
 *
 * Grounded, not generated. Every cell is a summary that was already
 * validated against its own paper's evidence allowlist and stored; this
 * component writes no prose of its own, ranks nothing and scores
 * nothing. That is a structural property rather than a promise: there
 * is no model call here to produce a comparative claim with, so
 * "Paper A is better" has no way to appear.
 *
 * WHY TWO PER-PAPER QUERIES RATHER THAN ONE PAIR QUERY
 * ----------------------------------------------------
 * Each column reads `queryKeys.paperIntelligence(userId, paperId)` — the
 * SAME key the paper workspace uses. Three things fall out of that:
 * a paper already opened in its workspace is served from cache, each
 * paper is fetched at most once no matter how it is paired, and
 * swapping A and B costs zero requests. A combined (A, B) key would
 * need normalising to stop A+B and B+A caching twice; with per-paper
 * keys that problem cannot arise in the first place.
 *
 * Ownership is the server's business, unchanged: GET /paper-intelligence
 * resolves the paper against the caller's verified JWT and returns the
 * same 404 for "no such paper" and "someone else's paper". Nothing here
 * sends an owner id, and a paper the user does not own simply fails to
 * load in its column.
 */

const SECTION_LABELS: Record<IntelligenceSectionName, string> = {
  research_problem: "Research Problem",
  research_objective: "Research Objective",
  methodology: "Methodology",
  dataset: "Dataset",
  experimental_setup: "Experimental Setup",
  evaluation_metrics: "Evaluation Metrics",
  key_results: "Key Results",
  contributions: "Contributions",
  limitations: "Limitations",
  reproducibility: "Reproducibility",
};

const NOT_AVAILABLE = "Paper Intelligence is not available for this paper.";

/** Distinct pages cited, in first-appearance order. */
function citedPages(evidence: IntelligenceEvidence[]): number[] {
  const seen: number[] = [];
  for (const item of evidence ?? []) {
    const page = Number(item.page);
    if (Number.isFinite(page) && page >= 1 && !seen.includes(page)) {
      seen.push(page);
    }
  }
  return seen;
}

/** One paper's cell for one section. */
function Cell({
  section,
  paperTitle,
  columnLabel,
}: {
  section: IntelligenceSection | undefined;
  paperTitle: string;
  columnLabel: string;
}) {
  if (!section) {
    return <p className="text-[13px] text-muted-foreground">—</p>;
  }

  if (section.status !== "answered") {
    // The paper genuinely does not state this. Named per column so the
    // absence stays attached to the right paper.
    return (
      <p className="text-[13px] italic text-muted-foreground">
        Not specified in {columnLabel}.
      </p>
    );
  }

  const pages = citedPages(section.evidence);

  return (
    <div>
      <p className="text-[13px] leading-[1.7] text-foreground/85">{section.summary}</p>
      <p className="mt-1.5 text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        AI interpretation
      </p>

      {pages.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Sources
          </span>
          {pages.map((page) => (
            // Navigates to THIS paper's workspace at that page, reusing
            // the existing viewer rather than embedding a second one.
            <Link
              key={page}
              to="/paper/$paperName"
              params={{ paperName: paperTitle }}
              search={{ page }}
              aria-label={`Open ${columnLabel} at page ${page}`}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2 py-0.5 text-[11px] font-mono text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <FileText className="h-3 w-3" aria-hidden="true" />
              Page {page}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

/** One column's load state, rendered in place of its cells. */
function ColumnState({
  columnLabel,
  isLoading,
  error,
  missing,
}: {
  columnLabel: string;
  isLoading: boolean;
  error: Error | null;
  missing: boolean;
}) {
  if (isLoading) {
    return (
      <div role="status" className="flex items-center gap-2 text-[13px] text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        Loading {columnLabel}…
      </div>
    );
  }
  if (error) {
    return (
      <p className="text-[13px] text-destructive">
        {/* Authored by the backend; never a raw exception. */}
        {error.message}
      </p>
    );
  }
  if (missing) {
    return <p className="text-[13px] text-muted-foreground">{NOT_AVAILABLE}</p>;
  }
  return null;
}

function usePaperIntelligence(userId: string | undefined, paperId: string) {
  return useQuery({
    // The same key the workspace uses, so a paper opened there is
    // already warm here.
    queryKey: queryKeys.paperIntelligence(userId, paperId),
    queryFn: () => getPaperIntelligence(paperId),
    enabled: !!userId && !!paperId,
    // A stored analysis only changes when someone regenerates it, so
    // treating it as fresh for a few minutes is honest rather than
    // merely convenient. Without this React Query's default staleTime of
    // 0 makes every re-selection — including swapping A and B — refetch
    // both papers in the background.
    staleTime: 5 * 60 * 1000,
  });
}

export function PaperComparisonMatrix() {
  const { user } = useAuth();
  const userId = user?.id;

  // Selection lives in the URL, so a refresh restores it and a
  // comparison can be bookmarked or shared as a link. Only ids travel;
  // see validateSearch in routes/compare.tsx.
  const search = useSearch({ from: "/compare" }) as { a?: string; b?: string };
  const navigate = useNavigate({ from: "/compare" });

  const setSelection = (slot: "a" | "b", paperId: string) => {
    navigate({
      search: (prev: Record<string, unknown>) => {
        const next = { ...prev };
        // Clearing a slot removes its parameter rather than leaving an
        // empty one behind.
        if (paperId) next[slot] = paperId;
        else delete next[slot];
        return next;
      },
      // A selection change is not a navigation the Back button should
      // have to step through.
      replace: true,
    });
  };

  const { data: papers = [] } = useQuery({
    queryKey: queryKeys.papersDetailed(userId),
    queryFn: getPapersDetailed,
    enabled: !!userId,
  });

  // Only indexed papers can carry an analysis.
  const selectable = useMemo(
    () => (papers as PaperDetail[]).filter((p) => p.status === "indexed"),
    [papers],
  );

  const titleOf = (paperId: string) =>
    selectable.find((p) => p.paper_id === paperId)?.title ?? "";

  // An id from the URL counts as a selection only once it is found in
  // this account's own paper list. A pasted id belonging to someone else
  // is therefore never even requested — the server would refuse it
  // anyway, but there is no reason to ask. Before the list loads nothing
  // is selected, and the selects fill in once it arrives.
  const owned = (paperId: string | undefined) =>
    paperId && selectable.some((p) => p.paper_id === paperId) ? paperId : "";

  const paperAId = owned(search.a);
  const paperBId = owned(search.b);

  const samePaper = !!paperAId && paperAId === paperBId;

  // Each column keeps its own paper's key even while the pair is
  // invalid. Blanking the id here would churn the query key on every
  // half-finished selection — and swapping A for B passes through
  // exactly that state, so the churn would refetch both papers.
  // The same-paper case is handled by refusing to RENDER, below.
  const a = usePaperIntelligence(userId, paperAId);
  const b = usePaperIntelligence(userId, paperBId);

  const ready = !!paperAId && !!paperBId && !samePaper;

  const columns: Array<{
    label: string;
    paperId: string;
    title: string;
    query: ReturnType<typeof usePaperIntelligence>;
  }> = [
    { label: "Paper A", paperId: paperAId, title: titleOf(paperAId), query: a },
    { label: "Paper B", paperId: paperBId, title: titleOf(paperBId), query: b },
  ];

  return (
    <section aria-label="Structured comparison" className="mt-12">
      <h2 className="flex items-center gap-2 text-sm font-medium">
        <Columns2 className="h-4 w-4" aria-hidden="true" />
        Structured comparison
      </h2>
      <p className="mt-1 text-xs text-muted-foreground">
        Built from each paper&rsquo;s saved analysis. Nothing is generated here, and
        no paper is ranked.
      </p>

      {selectable.length < 2 ? (
        <p className="mt-4 text-sm text-muted-foreground">
          Two indexed papers are needed to compare.
        </p>
      ) : (
        <>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {[
              { label: "Paper A", value: paperAId, slot: "a" as const },
              { label: "Paper B", value: paperBId, slot: "b" as const },
            ].map(({ label, value, slot }) => (
              <div key={label} className="space-y-1.5">
                <label
                  htmlFor={`select-${label.replace(/\s/g, "-").toLowerCase()}`}
                  className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground"
                >
                  {label}
                </label>
                <select
                  id={`select-${label.replace(/\s/g, "-").toLowerCase()}`}
                  value={value}
                  onChange={(e) => setSelection(slot, e.target.value)}
                  className="w-full rounded-xl border border-border bg-surface px-4 py-2.5 text-sm outline-none focus:border-primary/50"
                >
                  <option value="">Select a paper…</option>
                  {selectable.map((p) => (
                    <option key={p.paper_id} value={p.paper_id}>
                      {p.title}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>

          {samePaper && (
            <div className="mt-4 flex items-center gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
              <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
              Choose two different papers to compare.
            </div>
          )}

          {!ready && !samePaper && (
            <p className="mt-4 text-sm text-muted-foreground">
              Select two papers to see their saved analyses side by side.
            </p>
          )}

          {ready && (
            <div className="mt-6 overflow-hidden rounded-2xl border border-border bg-surface">
              {/* Column headers — each paper named once, clearly. */}
              <div className="grid grid-cols-1 border-b border-border bg-muted/30 md:grid-cols-[170px_1fr_1fr]">
                <div className="hidden px-5 py-4 md:block">
                  <span className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                    Section
                  </span>
                </div>
                {columns.map((col) => (
                  <div key={col.label} className="px-5 py-4 md:border-l md:border-border">
                    <div className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
                      {col.label}
                    </div>
                    <div
                      className="mt-1 truncate text-[13.5px] font-medium leading-snug tracking-tight"
                      title={col.title}
                    >
                      {col.title}
                    </div>
                  </div>
                ))}
              </div>

              {INTELLIGENCE_SECTION_ORDER.map((name) => (
                <div
                  key={name}
                  className="grid grid-cols-1 border-b border-border last:border-b-0 md:grid-cols-[170px_1fr_1fr]"
                >
                  <div className="px-5 pt-4 md:py-4">
                    <span className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                      {SECTION_LABELS[name]}
                    </span>
                  </div>

                  {columns.map((col) => {
                    const result = col.query.data as PaperIntelligenceResult | null | undefined;
                    const state = (
                      <ColumnState
                        columnLabel={col.label}
                        isLoading={col.query.isLoading}
                        error={(col.query.error as Error) ?? null}
                        missing={!col.query.isLoading && !col.query.error && !result}
                      />
                    );

                    return (
                      <div
                        key={col.label}
                        className="px-5 py-4 md:border-l md:border-border"
                      >
                        {/* Mobile: repeat the column name so a stacked
                            cell is never orphaned from its paper. */}
                        <div className="mb-1.5 text-[10px] font-mono uppercase tracking-wider text-muted-foreground md:hidden">
                          {col.label}
                        </div>
                        {result ? (
                          <Cell
                            section={result.intelligence?.[name]}
                            paperTitle={col.title}
                            columnLabel={col.label}
                          />
                        ) : (
                          state
                        )}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}
