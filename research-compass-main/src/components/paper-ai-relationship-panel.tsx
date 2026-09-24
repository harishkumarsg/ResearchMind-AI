import { useMemo, useRef, useState } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, FileText, History, Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  INTELLIGENCE_SECTION_ORDER,
  RelationshipStateError,
  generatePaperRelationship,
  getPaperRelationship,
  getPapersDetailed,
  type PaperDetail,
  type PaperRelationshipResult,
  type RelationshipEvidence,
  type RelationshipSection,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { queryKeys } from "@/lib/query-keys";

/**
 * AI relationship analysis — the fourth comparison tier.
 *
 * The three tiers above it state facts about stored data: the structured
 * matrix lays out each paper's saved analysis, and the Research
 * Relationship panel says which sections each paper grounds. Neither reads
 * the claims' MEANING. This tier does, which is exactly why it is kept
 * separate, labelled as interpretation in three places, and placed last.
 *
 * It replaces nothing. The deterministic tiers render identically whether
 * this section is mounted or not, and nothing here can change them.
 *
 * WHY THE CITATIONS ARE MAPPED BY PAPER ID, NOT BY COLUMN POSITION
 * ---------------------------------------------------------------
 * The stored relationship is keyed on a CANONICAL pair — the database
 * enforces `paper_a_id < paper_b_id` — so `cites_a` belongs to whichever
 * paper sorted first, which is NOT necessarily the paper the reader put in
 * the "Paper A" select. Rendering `cites_a` under the left column would
 * therefore attribute Paper B's pages to Paper A whenever the selection
 * happens to be reversed, and it would look perfectly well-formed.
 *
 * So every citation list is resolved through the paper id it belongs to
 * (`citesFor` below) and never through its position. This is the same
 * invariant the backend store refuses to compromise: it will not reorient
 * content to fit a different order, and neither will this panel.
 *
 * NO QUOTES, EVER
 * ---------------
 * The response carries `(page, chunk_id)` and no chunk text. This panel
 * therefore shows page PROVENANCE only — the same chips the other tiers
 * use — and fetches nothing further. There is no code path here that could
 * present a reconstructed passage as a quotation.
 */

/** Canonical section labels. Duplicated from the sibling compare tiers
 *  deliberately: extracting a shared module would mean editing two shipped
 *  files for no behavioural gain. */
const SECTION_LABELS: Record<string, string> = {
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

/**
 * The controlled vocabulary, rendered as neutral words.
 *
 * Deliberately no colour and no ordering: "divergent" is not worse than
 * "aligned", and tinting it red would turn a neutral description into a
 * judgement. The meaning is carried entirely by the text, so it survives
 * greyscale, high-contrast mode and a screen reader.
 */
const RELATION_LABELS: Record<string, string> = {
  aligned: "Aligned",
  divergent: "Divergent",
  complementary: "Complementary",
  not_comparable: "Not comparable",
};

const PROVENANCE_NOTE =
  "Generated from evidence rehydrated from both papers. An interpretation, " +
  "not a measurement — the structured comparison above is the factual record.";

export interface AiRelationshipColumn {
  /** "Paper A" / "Paper B" — the reader's own slot, never a rank. */
  label: string;
  title: string;
  paperId: string;
}

/** Distinct pages cited, in first-appearance order. */
function citedPages(evidence: RelationshipEvidence[] | undefined): number[] {
  const seen: number[] = [];
  for (const item of evidence ?? []) {
    const page = Number(item?.page);
    if (Number.isFinite(page) && page >= 1 && !seen.includes(page)) {
      seen.push(page);
    }
  }
  return seen;
}

/** An ISO timestamp as an unambiguous UTC string.
 *
 *  Not toLocaleString(): the backend already sends UTC, and a locale-shaped
 *  rendering would vary by machine — including between a reader's browser
 *  and the tests. */
function utcLabel(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())} ` +
    `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())} UTC`
  );
}

function Timestamp({ iso }: { iso: string }) {
  return <time dateTime={iso}>{utcLabel(iso)}</time>;
}

/** One paper's page provenance for one section. */
function EvidenceRow({
  column,
  evidence,
}: {
  column: AiRelationshipColumn;
  evidence: RelationshipEvidence[];
}) {
  const pages = citedPages(evidence);

  return (
    <div>
      <div className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
        {column.label} evidence
      </div>
      {pages.length === 0 ? (
        <p className="mt-0.5 text-[12px] italic text-muted-foreground">
          No evidence cited for {column.label}.
        </p>
      ) : (
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
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
        </div>
      )}
    </div>
  );
}

function RelationChip({ relation }: { relation: string }) {
  return (
    <span className="inline-flex shrink-0 items-center rounded-full border border-border bg-background px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
      {RELATION_LABELS[relation] ?? relation}
    </span>
  );
}

function RelationSectionCard({
  name,
  section,
  columns,
  citesFor,
}: {
  name: string;
  section: RelationshipSection;
  columns: AiRelationshipColumn[];
  citesFor: (column: AiRelationshipColumn) => RelationshipEvidence[];
}) {
  return (
    <div className="border-b border-border px-5 py-4 last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
          {SECTION_LABELS[name] ?? name}
        </h3>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <RelationChip relation={section.relation} />
          {/* Marked on every statement, not once at the top of the page. */}
          <span className="text-[9px] uppercase tracking-[0.16em] text-muted-foreground/70">
            AI interpretation
          </span>
        </div>
      </div>

      <p className="mt-2 text-[13px] leading-[1.7] text-foreground/85">
        {section.statement}
      </p>

      {/* Stacked on mobile with each paper named, so a wrapped row is
          never orphaned from the paper it belongs to. */}
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        {columns.map((column) => (
          <EvidenceRow
            key={column.label}
            column={column}
            evidence={citesFor(column)}
          />
        ))}
      </div>
    </div>
  );
}

/**
 * Which papers a 422 `source_intelligence_missing` refers to.
 *
 * Resolved through the ids in the payload, NOT through the
 * paper_a_analysed / paper_b_analysed flag positions: those are oriented to
 * the canonical pair, so reading them positionally would name the wrong
 * paper whenever the reader's selection is reversed.
 */
function unanalysedColumns(
  detail: Record<string, unknown>,
  columns: AiRelationshipColumn[],
): AiRelationshipColumn[] {
  const missing: AiRelationshipColumn[] = [];

  for (const [idKey, flagKey] of [
    ["paper_a_id", "paper_a_analysed"],
    ["paper_b_id", "paper_b_analysed"],
  ] as const) {
    if (detail[flagKey] !== false) continue;
    const paperId = detail[idKey];
    const column = columns.find((c) => c.paperId === paperId);
    if (column) missing.push(column);
  }

  return missing;
}

export function PaperAiRelationshipPanel({
  a,
  b,
  data,
  isLoading,
  error,
  generating,
  generateError,
  onGenerate,
}: {
  a: AiRelationshipColumn;
  b: AiRelationshipColumn;
  data: PaperRelationshipResult | null;
  isLoading: boolean;
  error: Error | null;
  generating: boolean;
  generateError: Error | null;
  onGenerate: () => void;
}) {
  const columns = [a, b];

  const header = (
    <>
      <h2 className="flex items-center gap-2 text-sm font-medium">
        <Sparkles className="h-4 w-4" aria-hidden="true" />
        AI relationship analysis
      </h2>
      <p className="mt-1 max-w-[80ch] text-xs text-muted-foreground">
        {PROVENANCE_NOTE}
      </p>
    </>
  );

  const section = (children: React.ReactNode) => (
    <section aria-label="AI relationship analysis" className="mt-12">
      {header}
      {children}
    </section>
  );

  if (isLoading) {
    return section(
      <div
        role="status"
        className="mt-4 flex items-center gap-2 text-sm text-muted-foreground"
      >
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        Loading AI relationship analysis…
      </div>,
    );
  }

  if (error) {
    return section(
      <div className="mt-4 flex items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
        {/* Authored by the backend; never a raw exception. */}
        {error.message}
      </div>,
    );
  }

  // --- what the failed generation, if any, means -----------------------
  const stateError =
    generateError instanceof RelationshipStateError ? generateError : null;
  const conflict = stateError?.code === "source_intelligence_changed";
  const missingSource = stateError?.code === "source_intelligence_missing";
  const nothingComparable = stateError?.code === "no_comparable_sections";

  // A stored analysis whose source is GONE cannot be regenerated — the
  // server would answer 422 — so the action is withheld rather than
  // offered and then refused.
  const sourceGone = data?.stale_reason === "source_intelligence_missing";

  const canGenerate = !missingSource && !nothingComparable && !sourceGone;

  const actionLabel = conflict
    ? "Try again"
    : data
      ? "Regenerate"
      : "Generate AI relationship analysis";

  const action = canGenerate ? (
    <Button
      type="button"
      variant={data ? "outline" : "default"}
      size="sm"
      onClick={onGenerate}
      disabled={generating}
      aria-busy={generating}
    >
      {generating ? (
        <>
          <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          Analysing…
        </>
      ) : (
        actionLabel
      )}
    </Button>
  ) : null;

  const generationStatus = generating ? (
    <div
      role="status"
      className="mt-3 flex items-center gap-2 text-[13px] text-muted-foreground"
    >
      <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
      Analysing both papers&rsquo; evidence…
    </div>
  ) : null;

  const failure = generateError ? (
    <div className="mt-4 space-y-2">
      <div className="flex items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
        <div>
          {/* Authored by the backend — a sanitized message, never a raw
              exception and never the model's own text. */}
          <p>{generateError.message}</p>

          {conflict && (
            <p className="mt-1 text-[12.5px] text-destructive/85">
              One of the papers was re-analysed while this comparison was
              running, so the result was discarded rather than saved over the
              newer analysis.
            </p>
          )}

          {nothingComparable && (
            <p className="mt-1 text-[12.5px] text-destructive/85">
              No section has grounded evidence on both sides, so there is
              nothing to relate.
            </p>
          )}

          {missingSource && (
            <div className="mt-1 text-[12.5px] text-destructive/85">
              <p>Analyse the missing paper first, then try again.</p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {unanalysedColumns(stateError?.detail ?? {}, columns).map((column) => (
                  <Link
                    key={column.label}
                    to="/paper/$paperName"
                    params={{ paperName: column.title }}
                    aria-label={`Open ${column.label} to analyse it`}
                    className="inline-flex items-center gap-1 rounded-full border border-destructive/40 bg-background px-2 py-0.5 text-[11px] font-mono text-destructive transition-colors hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  >
                    <FileText className="h-3 w-3" aria-hidden="true" />
                    {column.label}
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  ) : null;

  // --- nothing generated yet ------------------------------------------
  if (!data) {
    return section(
      <>
        {failure}
        <div className="mt-4 rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center">
          <p className="text-sm text-foreground/80">No AI relationship analysis yet</p>
          <p className="mx-auto mt-1 max-w-[46ch] text-xs text-muted-foreground">
            These two papers have not been related yet. Generating one reads the
            exact evidence each paper cited and costs one AI generation.
          </p>
          {action && <div className="mt-4 flex justify-center">{action}</div>}
          {generationStatus}
        </div>
      </>,
    );
  }

  // --- a stored analysis ----------------------------------------------
  // Rendered in canonical section order rather than object-key order, and
  // only for the sections the comparison actually covered. The absent
  // sections were never asked about; the deterministic tier above already
  // accounts for them.
  const sections = INTELLIGENCE_SECTION_ORDER.filter(
    (name) => data.relationship?.sections?.[name],
  );

  // `cites_a` belongs to data.paper_a_id, which is the CANONICAL first
  // paper and not necessarily the reader's "Paper A". Resolved by id, never
  // by column position — see the module docstring.
  const citesForSection = (
    sectionData: RelationshipSection,
    column: AiRelationshipColumn,
  ): RelationshipEvidence[] =>
    column.paperId === data.paper_a_id ? sectionData.cites_a : sectionData.cites_b;

  return section(
    <>
      <div className="mt-4 flex flex-wrap items-start justify-between gap-3">
        <div className="text-[11px] text-muted-foreground">
          <div>
            Analysis generated <Timestamp iso={data.generated_at} /> ·{" "}
            <span className="font-mono">{data.model}</span> · schema v
            {data.schema_version}
          </div>
          <div className="mt-0.5">
            Compared the analyses saved at{" "}
            <Timestamp iso={data.paper_a_generated_at} /> and{" "}
            <Timestamp iso={data.paper_b_generated_at} />.
          </div>
        </div>
        {action}
      </div>

      {data.stale && (
        // Text, not colour alone: the state has to survive greyscale and a
        // screen reader.
        <div className="mt-3 flex items-start gap-3 rounded-xl border border-border bg-muted/30 px-4 py-3 text-[13px] text-foreground/80">
          <History className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div>
            <span className="inline-flex items-center rounded-full border border-border bg-background px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Out of date
            </span>
            <p className="mt-1.5">
              {data.stale_reason === "source_intelligence_missing"
                ? "A paper's saved analysis is no longer available, so this comparison " +
                  "describes claims that are not currently stored. Analyse that paper " +
                  "again before relating them."
                : "A paper was re-analysed after this comparison was generated, so it " +
                  "describes an earlier version of that paper's claims."}
            </p>
          </div>
        </div>
      )}

      {generationStatus}
      {failure}

      {sections.length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">
          This analysis covers no sections.
        </p>
      ) : (
        <div className="mt-4 overflow-hidden rounded-2xl border border-border bg-surface">
          {sections.map((name) => {
            const sectionData = data.relationship.sections[name];
            return (
              <RelationSectionCard
                key={name}
                name={name}
                section={sectionData}
                columns={columns}
                citesFor={(column) => citesForSection(sectionData, column)}
              />
            );
          })}
        </div>
      )}
    </>,
  );
}

/**
 * Route-level wiring.
 *
 * Reads the same `?a=&b=` selection the other compare tiers use and
 * applies the same ownership filter: an id counts only once it appears in
 * this account's own paper list, so a pasted foreign id is never even
 * requested. The server would refuse it regardless — this just means not
 * asking.
 */
export function PaperAiRelationshipSection() {
  const { user } = useAuth();
  const userId = user?.id;
  const qc = useQueryClient();

  const search = useSearch({ from: "/compare" }) as { a?: string; b?: string };

  // Local generation state, matching every other mutation in this app
  // (reports, compare, delete). React Query's mutation state is not needed
  // and a second locking mechanism would be one more thing to keep honest:
  // the UNIQUE(owner_id, paper_a_id, paper_b_id) constraint already makes a
  // duplicate POST an in-place upsert.
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<Error | null>(null);

  // A REF, not the `generating` state, is what actually prevents a double
  // POST. Two clicks dispatched in one React batch both run the handler
  // before any re-render, so at that point `disabled` has not been applied
  // yet AND the state read below is still the stale `false` from this
  // render's closure. A ref updates synchronously, so the second call sees
  // the latch the first one set. Verified by mutation: removing this makes
  // the batched-double-click test fail, while removing the `disabled`
  // attribute alone does not.
  const inFlight = useRef(false);

  const { data: papers = [] } = useQuery({
    queryKey: queryKeys.papersDetailed(userId),
    queryFn: getPapersDetailed,
    enabled: !!userId,
  });

  const selectable = useMemo(
    () => (papers as PaperDetail[]).filter((p) => p.status === "indexed"),
    [papers],
  );

  const owned = (paperId: string | undefined) =>
    paperId && selectable.some((p) => p.paper_id === paperId) ? paperId : "";

  const paperAId = owned(search.a);
  const paperBId = owned(search.b);
  const titleOf = (paperId: string) =>
    selectable.find((p) => p.paper_id === paperId)?.title ?? "";

  const ready = !!paperAId && !!paperBId && paperAId !== paperBId;

  const relationship = useQuery({
    queryKey: queryKeys.paperRelationship(userId, paperAId, paperBId),
    queryFn: () => getPaperRelationship(paperAId, paperBId),
    enabled: !!userId && ready,
    // A stored relationship changes only when someone regenerates it.
    staleTime: 5 * 60 * 1000,
  });

  // The matrix above already explains "select two papers", so this section
  // stays silent rather than repeating it.
  if (!ready) return null;

  const handleGenerate = async () => {
    // Belt and braces with the disabled button, and the part that actually
    // works in the same-tick case — see the `inFlight` note above.
    if (inFlight.current) return;
    inFlight.current = true;

    setGenerating(true);
    setGenerateError(null);
    try {
      const result = await generatePaperRelationship(paperAId, paperBId);
      // The POST returns the complete stored object, so the cache is
      // written directly — no refetch round trip. Deliberately scoped to
      // the canonical pair key: regenerating a relationship does not change
      // either paper's stored analysis, so paper-intelligence keys are left
      // untouched.
      qc.setQueryData(
        queryKeys.paperRelationship(userId, paperAId, paperBId),
        result,
      );
    } catch (err) {
      setGenerateError(
        err instanceof Error ? err : new Error("The comparison could not be completed."),
      );
    } finally {
      inFlight.current = false;
      setGenerating(false);
    }
  };

  return (
    <PaperAiRelationshipPanel
      a={{ label: "Paper A", title: titleOf(paperAId), paperId: paperAId }}
      b={{ label: "Paper B", title: titleOf(paperBId), paperId: paperBId }}
      data={(relationship.data as PaperRelationshipResult | null | undefined) ?? null}
      isLoading={relationship.isLoading}
      error={(relationship.error as Error) ?? null}
      generating={generating}
      generateError={generateError}
      onGenerate={handleGenerate}
    />
  );
}
