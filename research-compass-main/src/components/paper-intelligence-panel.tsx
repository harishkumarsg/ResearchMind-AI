import { AlertCircle, FileText, Loader2, ScanSearch } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  getPaperIntelligence,
  INTELLIGENCE_SECTION_ORDER,
  type IntelligenceEvidence,
  type IntelligenceSection,
  type IntelligenceSectionName,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { queryKeys } from "@/lib/query-keys";

/**
 * The stored Paper Intelligence for the paper the workspace is showing.
 *
 * READ ONLY, and deliberately so. This panel never generates: it fetches
 * what is already in the database, and a paper with no analysis yet shows
 * an empty state rather than silently triggering a billable generation on
 * page load. Reading costs no provider call on the server either.
 *
 * Evidence is rendered by PAGE, because a page is the thing a reader can
 * act on — clicking one drives the existing PDF viewer through the same
 * `onEvidenceClick` → `setPage` path the Ask panel already uses, so the
 * document is not re-fetched. chunk_id is part of evidence identity and
 * is kept in the data and in the accessible label, but it is not the
 * primary label: "chunk 2" means nothing to a reader.
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

function SectionBlock({
  name,
  section,
  onEvidenceClick,
}: {
  name: IntelligenceSectionName;
  section: IntelligenceSection;
  onEvidenceClick: (page: number) => void;
}) {
  const answered = section.status === "answered";
  const pages = answered ? citedPages(section.evidence ?? []) : [];

  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <h3 className="text-[13px] font-medium tracking-tight text-foreground">
        {SECTION_LABELS[name]}
      </h3>

      {answered ? (
        <p className="mt-1.5 text-[13px] leading-[1.7] text-foreground/85">
          {section.summary}
        </p>
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
              onClick={() => onEvidenceClick(page)}
              aria-label={`Go to page ${page}`}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2 py-0.5 text-[11px] font-mono text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <FileText className="h-3 w-3" aria-hidden="true" />
              Page {page}
            </button>
          ))}
        </div>
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

  const { data, isLoading, error } = useQuery({
    // Scoped to BOTH the signed-in user and this paper.
    queryKey: queryKeys.paperIntelligence(userId, paperId),
    queryFn: () => getPaperIntelligence(paperId),
    // Nothing is fetched until there is an authenticated user AND a
    // resolved paper, so a signed-out render never reaches the API.
    enabled: !!userId && !!paperId,
  });

  return (
    <div className="flex h-full flex-col gap-4">
      <div>
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <ScanSearch className="h-4 w-4" aria-hidden="true" />
          Paper Intelligence
        </h2>
        <p className="mt-1 text-xs text-muted-foreground">
          A structured read of this paper, with the pages each point came from.
        </p>
      </div>

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
        <div className="flex items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
          {/* Authored by the backend; never a raw exception. */}
          {(error as Error).message}
        </div>
      )}

      {!isLoading && !error && !data && (
        // Empty state. Deliberately offers no generate action: this
        // phase ships the reader, and an action here would be one click
        // away from an unmetered production generation.
        <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center">
          <p className="text-sm text-foreground/80">No analysis yet</p>
          <p className="mx-auto mt-1 max-w-[34ch] text-xs text-muted-foreground">
            This paper has not been analysed. Once an analysis exists it will
            appear here automatically.
          </p>
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
