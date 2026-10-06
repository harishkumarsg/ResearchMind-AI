import { useRef, useState } from "react";
import { AlertCircle, FileText, Loader2, Quote, ScanSearch } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  generatePaperIntelligence,
  getPaperIntelligence,
  INTELLIGENCE_SECTION_ORDER,
  PaperIntelligenceNotFoundError,
  type IntelligenceEvidence,
  type IntelligenceSection,
  type IntelligenceSectionName,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { queryKeys } from "@/lib/query-keys";

/**
 * The stored Paper Intelligence for the paper the workspace is showing,
 * with an inline Evidence Inspector.
 *
 * READS ON LOAD, GENERATES ONLY ON A CLICK. Opening a paper fetches what
 * is already in the database — a read costs no provider call — and a
 * paper with no analysis shows an empty state rather than silently
 * triggering a billable generation. Generating or regenerating is an
 * explicit user action against the existing metered endpoint, which owns
 * ownership, quota, validation and persistence; this panel duplicates
 * none of it.
 *
 * THE CHAIN THIS PANEL MAKES VISIBLE
 * ----------------------------------
 *   section  ->  evidence  ->  page  ->  PDF
 *
 * Clicking a page reference does two things at once: it drives the
 * existing PDF viewer (unchanged, through the same `onEvidenceClick` ->
 * `setPage` path the Ask panel uses) and it opens the inspector for that
 * page. Every value the inspector shows comes from the ALREADY-LOADED
 * response — there is no second request per click, so navigation stays
 * instant and no provider is ever touched.
 *
 * WHY AN INLINE DISCLOSURE RATHER THAN A POPOVER
 * ----------------------------------------------
 * This panel lives in a scrollable column beside the viewer, and an
 * anchored popover inside a scroll container fights its own positioning
 * for no benefit. An inline block stays in flow, needs no portal and no
 * animation, and keeps the evidence directly under the claim it
 * supports — which is the relationship the inspector exists to show.
 *
 * TRUST BOUNDARY
 * --------------
 * The summary is the model's reading of the paper and is labelled as
 * such. A quote, when one was persisted, is a verbatim passage the
 * validator checked against the chunk it came from, and is labelled
 * separately. Nothing here invents a passage: evidence with no stored
 * quote shows its LOCATION only, never generated text dressed up as a
 * quotation.
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

/** Distinct pages cited by a section, in the order they first appear.
 *  Two chunks on the same page are one reference to a reader. */
function citedPages(evidence: IntelligenceEvidence[]): number[] {
  const seen: number[] = [];
  for (const item of evidence) {
    const page = Number(item.page);
    if (Number.isFinite(page) && page >= 1 && !seen.includes(page)) {
      seen.push(page);
    }
  }
  return seen;
}

/** "22 Sep 2026"-style date, or null for a missing or unparseable stamp. */
function formatGeneratedAt(value: string | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function EvidenceDetail({
  page,
  items,
}: {
  page: number;
  items: IntelligenceEvidence[];
}) {
  const quoted = items.filter(
    (item) => typeof item.quote === "string" && item.quote.trim().length > 0,
  );

  return (
    <div
      role="region"
      aria-label={`Evidence on page ${page}`}
      className="mt-3 rounded-lg border border-border bg-background/60 p-3"
    >
      <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
        <Quote className="h-3 w-3" aria-hidden="true" />
        Evidence · Page {page}
      </div>

      {quoted.length > 0 ? (
        <>
          <p className="mt-2 text-[11px] text-muted-foreground">
            Passage from the paper
          </p>
          {quoted.map((item, index) => (
            <blockquote
              key={`${item.page}-${item.chunk_id}-${index}`}
              className="mt-1.5 border-l-2 border-border pl-3 text-[13px] leading-[1.7] text-foreground/85"
            >
              {item.quote}
            </blockquote>
          ))}
        </>
      ) : (
        // No stored passage. Say so plainly — never synthesise one, and
        // never reuse the model's summary as though the paper said it.
        <p className="mt-2 text-[13px] leading-[1.7] text-muted-foreground">
          Evidence location: Page {page}. No passage was stored for this
          reference — open the page to read it in the paper.
        </p>
      )}

      {/* Evidence identity, kept available but deliberately secondary:
          (page, chunk_id) is what the reference IS, but "chunk 2" means
          nothing to a reader. */}
      <p className="mt-2 text-[10px] font-mono text-muted-foreground/70">
        Reference:{" "}
        {items.map((item) => `page ${item.page} · chunk ${item.chunk_id}`).join("  |  ")}
      </p>
    </div>
  );
}

function SectionBlock({
  name,
  section,
  onEvidenceClick,
}: {
  name: IntelligenceSectionName;
  section: IntelligenceSection;
  onEvidenceClick: (page: number) => void;
}) {
  const [openPage, setOpenPage] = useState<number | null>(null);

  const answered = section.status === "answered";
  const evidence = answered ? (section.evidence ?? []) : [];
  const pages = citedPages(evidence);

  const handlePageClick = (page: number) => {
    // 1. Existing behaviour, unchanged and always first: drive the PDF.
    onEvidenceClick(page);
    // 2. Then reveal what that reference actually is.
    setOpenPage((current) => (current === page ? null : page));
  };

  const openItems =
    openPage === null
      ? []
      : evidence.filter((item) => Number(item.page) === openPage);

  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <h3 className="text-[13px] font-medium tracking-tight text-foreground">
        {SECTION_LABELS[name]}
      </h3>

      {answered ? (
        <>
          <p className="mt-1.5 text-[13px] leading-[1.7] text-foreground/85">
            {section.summary}
          </p>
          {/* The summary is the model's reading, not the paper's words. */}
          <p className="mt-1.5 text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            AI interpretation
          </p>
        </>
      ) : (
        // Not a failure: the paper genuinely does not state this.
        <p className="mt-1.5 text-[13px] italic text-muted-foreground">Not specified</p>
      )}

      {pages.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Sources
          </span>
          {pages.map((page) => (
            <button
              key={page}
              type="button"
              onClick={() => handlePageClick(page)}
              aria-label={`Go to page ${page}`}
              aria-expanded={openPage === page}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2 py-0.5 text-[11px] font-mono text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring aria-expanded:border-foreground/30 aria-expanded:text-foreground"
            >
              <FileText className="h-3 w-3" aria-hidden="true" />
              Page {page}
            </button>
          ))}
        </div>
      )}

      {openPage !== null && openItems.length > 0 && (
        <EvidenceDetail page={openPage} items={openItems} />
      )}
    </div>
  );
}

export function PaperIntelligencePanel({
  paperId,
  onEvidenceClick,
}: {
  paperId: string;
  onEvidenceClick: (page: number) => void;
}) {
  const { user } = useAuth();
  const userId = user?.id;
  const qc = useQueryClient();
  const intelligenceKey = queryKeys.paperIntelligence(userId, paperId);

  const { data, isLoading, error } = useQuery({
    // Scoped to BOTH the signed-in user and this paper.
    queryKey: intelligenceKey,
    queryFn: () => getPaperIntelligence(paperId),
    // Nothing is fetched until there is an authenticated user AND a
    // resolved paper, so a signed-out render never reaches the API.
    enabled: !!userId && !!paperId,
  });

  // Local generation state, matching the relationship panel's generation
  // flow rather than introducing useMutation for one action.
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<Error | null>(null);
  const [superseded, setSuperseded] = useState(false);

  // A REF, not the `generating` state, is what actually prevents a double
  // POST — each of which would spend a unit. Two clicks dispatched in one
  // React batch both run the handler before any re-render, so `disabled`
  // has not been applied yet and a state read would still see `false`. A
  // ref updates synchronously. Same latch as the relationship panel.
  const inFlight = useRef(false);

  // A 404 means the paper is missing or not this user's: there is nothing
  // to analyse, so no action is offered. Any other read failure — a stored
  // row that no longer parses, say — can be fixed by generating again,
  // and the server refuses a doomed request before charging for it.
  const notFound = error instanceof PaperIntelligenceNotFoundError;
  const canGenerate = !!userId && !!paperId && !isLoading && !notFound;

  const handleGenerate = async () => {
    if (!canGenerate || inFlight.current) return;
    inFlight.current = true;

    setGenerating(true);
    setGenerateError(null);
    setSuperseded(false);
    try {
      const result = await generatePaperIntelligence(paperId);
      // The POST returns the stored, current object — even when a newer
      // run won and this one was discarded — so the cache is written
      // directly, with no GET round trip. The comparison matrix reads this
      // same key and picks the new analysis up too.
      qc.setQueryData(intelligenceKey, result);
      setSuperseded(result.superseded === true);
      // Any relationship built on this paper's previous analysis may now
      // be stale. The server decides that on read, so cached copies are
      // invalidated; relationship generation itself is untouched.
      qc.invalidateQueries({ queryKey: queryKeys.paperRelationshipsForUser(userId) });
    } catch (err) {
      setGenerateError(
        err instanceof Error ? err : new Error("The paper analysis could not be completed."),
      );
      // One free re-read, never a retry. A request that failed on the
      // client side (a dropped connection, a proxy timeout) may still have
      // finished and been saved on the server; the GET makes no provider
      // call and spends no generation allowance.
      qc.invalidateQueries({ queryKey: intelligenceKey, exact: true });
    } finally {
      inFlight.current = false;
      setGenerating(false);
    }
  };

  const generateButton = (label: string, variant: "default" | "outline") => (
    <Button
      type="button"
      size="sm"
      variant={variant}
      onClick={handleGenerate}
      disabled={generating}
      aria-busy={generating}
    >
      {generating ? (
        <>
          <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          Analysing…
        </>
      ) : (
        label
      )}
    </Button>
  );

  const generatedOn = data ? formatGeneratedAt(data.generated_at) : null;

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-medium">
            <ScanSearch className="h-4 w-4" aria-hidden="true" />
            Paper Intelligence
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            A structured read of this paper. Select a page to see the evidence
            behind a point.
          </p>
          {generatedOn && (
            <p className="mt-1 text-[11px] text-muted-foreground">Generated {generatedOn}</p>
          )}
        </div>
        {/* Regenerate is secondary: the analysis already exists. */}
        {data && canGenerate && generateButton("Regenerate", "outline")}
      </div>

      {generating && (
        <div
          role="status"
          className="flex items-center gap-2 text-[13px] text-muted-foreground"
        >
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          Analysing this paper… This can take up to about a minute.
        </div>
      )}

      {generateError && (
        <div className="flex items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div>
            {/* Authored by the backend, or by api.ts; never a raw exception. */}
            <p>{generateError.message}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Your existing analysis, if any, was not changed.
            </p>
          </div>
        </div>
      )}

      {superseded && data && (
        <p className="text-xs text-muted-foreground">
          A newer analysis finished first and is shown.
        </p>
      )}

      {isLoading && (
        <div
          role="status"
          className="flex items-center gap-2 text-sm text-muted-foreground"
        >
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Loading paper intelligence…
        </div>
      )}

      {error && (
        <div className="flex flex-col items-start gap-3">
          <div className="flex w-full items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
            {/* Authored by the backend; never a raw exception. */}
            {(error as Error).message}
          </div>
          {/* With data on screen, Regenerate in the header already covers it. */}
          {!data && canGenerate && generateButton("Generate intelligence", "default")}
        </div>
      )}

      {!isLoading && !error && !data && (
        // Empty state. Generation is offered but never started on its own:
        // it spends a unit of the user's daily AI allowance.
        <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center">
          <p className="text-sm text-foreground/80">No analysis yet</p>
          <p className="mx-auto mt-1 max-w-[38ch] text-xs text-muted-foreground">
            Generate a structured read of this paper — its problem, method,
            data, results and limitations, each linked to the pages it comes
            from. Uses one AI request from your daily allowance.
          </p>
          {canGenerate && (
            <div className="mt-4 flex justify-center">
              {generateButton("Generate intelligence", "default")}
            </div>
          )}
        </div>
      )}

      {data && (
        <div className="space-y-3">
          {INTELLIGENCE_SECTION_ORDER.map((name) => {
            const section = data.intelligence?.[name];
            if (!section) return null;
            return (
              <SectionBlock
                key={name}
                name={name}
                section={section}
                onEvidenceClick={onEvidenceClick}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
