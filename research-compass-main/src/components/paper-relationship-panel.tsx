import { useMemo } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, FileText, Loader2, GitCompareArrows } from "lucide-react";
import {
  compareIntelligence,
  type SectionComparison,
  type Verdict,
} from "@/lib/paper-relationships";
import {
  getPaperIntelligence,
  getPapersDetailed,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { queryKeys } from "@/lib/query-keys";
import type {
  PaperDetail,
  PaperIntelligenceResult,
  IntelligenceEvidence,
  IntelligenceSection,
  IntelligenceSectionName,
  PaperIntelligenceSections,
} from "@/lib/api";

/**
 * Research Relationship — which of the ten canonical sections each paper
 * actually has a grounded claim for.
 *
 * Presentational only. It runs the deterministic engine over intelligence
 * the comparison matrix has ALREADY loaded, so it issues no query of its
 * own: no request per verdict, no provider, no generation. Nothing here
 * can trigger a POST.
 *
 * WHAT "SUPPORTED BY BOTH" MEANS, AND WHAT IT DOES NOT
 * ----------------------------------------------------
 * It means both papers contain a grounded claim for that section. It does
 * NOT mean the papers agree — nothing in this layer reads the claims'
 * meaning, compares their words or measures similarity. Saying "supported
 * by both" while implying agreement would be the exact invented
 * relationship this phase exists to avoid, so the panel states the
 * distinction in its own subheading rather than leaving it to be assumed.
 *
 * Three provenance levels are kept visually distinct:
 *   verdict  — "Directly supported": computed from stored data, not inferred
 *   summary  — "AI interpretation": the model's reading of that paper
 *   evidence — the pages the claim was drawn from, in the owning paper
 *
 * No score, no ranking, no winner. `Verdict` has no ordering and this
 * module renders no number.
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

/** Neutral presence labels. Deliberately not grades. */
const VERDICT_LABELS: Record<Verdict, string> = {
  comparable: "Supported by both",
  a_only: "Paper A only",
  b_only: "Paper B only",
  neither: "Insufficient evidence",
};

export interface RelationshipColumn {
  /** "Paper A" / "Paper B" — the column's identity, never a rank. */
  label: string;
  title: string;
  /** The ten stored sections, or null when this paper has no analysis. */
  intelligence: PaperIntelligenceSections | null;
  isLoading: boolean;
  error: Error | null;
}

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

function VerdictChip({ verdict }: { verdict: Verdict }) {
  return (
    <span className="inline-flex shrink-0 items-center rounded-full border border-border bg-background px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
      {VERDICT_LABELS[verdict]}
    </span>
  );
}

/** One paper's grounded claim, or a plain statement that it has none. */
function ClaimRow({
  column,
  claim,
}: {
  column: RelationshipColumn;
  claim: IntelligenceSection | null;
}) {
  if (!claim) {
    // No grounded claim. Stated, never filled in from the other paper.
    return (
      <div className="text-[12px] text-muted-foreground">
        <span className="font-mono uppercase tracking-wider">{column.label}</span>{" "}
        <span className="italic">no grounded claim for this section</span>
      </div>
    );
  }

  const pages = citedPages(claim.evidence);

  return (
    <div>
      <div className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
        {column.label}
      </div>
      <p className="mt-0.5 text-[13px] leading-[1.7] text-foreground/85">
        {claim.summary}
      </p>
      <div className="mt-1 flex flex-wrap items-center gap-1.5">
        <span className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          AI interpretation
        </span>
        {pages.length > 0 && (
          <>
            <span className="text-[10px] text-muted-foreground/60">·</span>
            <span className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
              Evidence
            </span>
            {pages.map((page) => (
              // Opens THIS column's paper at that page — never the other's.
              <Link
                key={page}
                to="/paper/$paperName"
                params={{ paperName: column.title }}
                search={{ page }}
                aria-label={`Open ${column.label} at page ${page}`}
                className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2 py-0.5 text-[11px] font-mono text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <FileText className="h-3 w-3" aria-hidden="true" />
                Page {page}
              </Link>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

function SectionRow({
  comparison,
  a,
  b,
}: {
  comparison: SectionComparison;
  a: RelationshipColumn;
  b: RelationshipColumn;
}) {
  return (
    <div className="border-b border-border px-5 py-4 last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
          {SECTION_LABELS[comparison.section]}
        </h3>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <VerdictChip verdict={comparison.verdict} />
          {/* The verdict is computed from stored data, not inferred. */}
          <span className="text-[9px] uppercase tracking-[0.16em] text-muted-foreground/70">
            Directly supported
          </span>
        </div>
      </div>

      {comparison.verdict === "neither" ? (
        <p className="mt-2 text-[13px] text-muted-foreground">
          Insufficient evidence to compare.
        </p>
      ) : (
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <ClaimRow column={a} claim={comparison.paper_a_claim} />
          <ClaimRow column={b} claim={comparison.paper_b_claim} />
        </div>
      )}
    </div>
  );
}

export function PaperRelationshipPanel({
  a,
  b,
}: {
  a: RelationshipColumn;
  b: RelationshipColumn;
}) {
  const header = (
    <>
      <h2 className="flex items-center gap-2 text-sm font-medium">
        <GitCompareArrows className="h-4 w-4" aria-hidden="true" />
        Research Relationship
      </h2>
      <p className="mt-1 max-w-[70ch] text-xs text-muted-foreground">
        Which sections each paper has a grounded claim for.{" "}
        <span className="text-foreground/70">
          &ldquo;Supported by both&rdquo; means both papers contain a grounded claim
          for that section — it does not mean the papers agree.
        </span>{" "}
        No paper is ranked.
      </p>
    </>
  );

  // Loading. Deliberately NOT "Insufficient evidence": a verdict before
  // the data arrives would be a false statement about the papers.
  if (a.isLoading || b.isLoading) {
    return (
      <section aria-label="Research relationship" className="mt-12">
        {header}
        <div
          role="status"
          className="mt-4 flex items-center gap-2 text-sm text-muted-foreground"
        >
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Loading research relationship…
        </div>
      </section>
    );
  }

  const failure = a.error ?? b.error;
  if (failure) {
    return (
      <section aria-label="Research relationship" className="mt-12">
        {header}
        <div className="mt-4 flex items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
          {/* Authored by the backend; never a raw exception. */}
          {failure.message}
        </div>
      </section>
    );
  }

  const unanalysed = [a, b].filter((column) => !column.intelligence);
  const comparisons = compareIntelligence(a.intelligence, b.intelligence);

  return (
    <section aria-label="Research relationship" className="mt-12">
      {header}

      {unanalysed.length > 0 && (
        // Said plainly, and with no action attached: generating an
        // analysis is a billable operation and does not belong one click
        // from a comparison view.
        <div className="mt-4 rounded-xl border border-dashed border-border bg-surface px-4 py-3 text-[13px] text-muted-foreground">
          {unanalysed.length === 2
            ? "Neither paper has a structured analysis yet, so there is nothing to relate."
            : `${unanalysed[0].label} (${unanalysed[0].title}) has no structured analysis yet, so only ${unanalysed[0].label === a.label ? b.label : a.label} can contribute claims below.`}
        </div>
      )}

      <div className="mt-4 overflow-hidden rounded-2xl border border-border bg-surface">
        {comparisons.map((comparison) => (
          <SectionRow key={comparison.section} comparison={comparison} a={a} b={b} />
        ))}
      </div>
    </section>
  );
}

/**
 * Route-level wiring for the panel above.
 *
 * Reads the same `?a=&b=` selection the comparison matrix uses and the
 * same per-paper query keys, so React Query serves both analyses from
 * the cache the matrix already filled: rendering this section costs
 * ZERO additional requests, and it can never issue a POST.
 *
 * Kept separate from PaperComparisonMatrix rather than nested inside it.
 * Two sections that read the same cache stay independently testable, and
 * the matrix component is left exactly as it shipped.
 */
function useStoredIntelligence(userId: string | undefined, paperId: string) {
  return useQuery({
    queryKey: queryKeys.paperIntelligence(userId, paperId),
    queryFn: () => getPaperIntelligence(paperId),
    enabled: !!userId && !!paperId,
    staleTime: 5 * 60 * 1000,
  });
}

export function PaperRelationshipSection() {
  const { user } = useAuth();
  const userId = user?.id;

  const search = useSearch({ from: "/compare" }) as { a?: string; b?: string };

  const { data: papers = [] } = useQuery({
    queryKey: queryKeys.papersDetailed(userId),
    queryFn: getPapersDetailed,
    enabled: !!userId,
  });

  const selectable = useMemo(
    () => (papers as PaperDetail[]).filter((p) => p.status === "indexed"),
    [papers],
  );

  // An id from the URL counts only once it is found in this account's own
  // paper list — the URL is a bookmark, never an authorization.
  const owned = (paperId: string | undefined) =>
    paperId && selectable.some((p) => p.paper_id === paperId) ? paperId : "";

  const paperAId = owned(search.a);
  const paperBId = owned(search.b);
  const titleOf = (paperId: string) =>
    selectable.find((p) => p.paper_id === paperId)?.title ?? "";

  const a = useStoredIntelligence(userId, paperAId);
  const b = useStoredIntelligence(userId, paperBId);

  // Nothing to relate until two distinct papers are chosen. The matrix
  // above already explains that state, so this section stays silent
  // rather than repeating it.
  if (!paperAId || !paperBId || paperAId === paperBId) return null;

  const toColumn = (
    label: string,
    paperId: string,
    query: ReturnType<typeof useStoredIntelligence>,
  ): RelationshipColumn => ({
    label,
    title: titleOf(paperId),
    intelligence:
      (query.data as PaperIntelligenceResult | null | undefined)?.intelligence ?? null,
    isLoading: query.isLoading,
    error: (query.error as Error) ?? null,
  });

  return (
    <PaperRelationshipPanel
      a={toColumn("Paper A", paperAId, a)}
      b={toColumn("Paper B", paperBId, b)}
    />
  );
}
