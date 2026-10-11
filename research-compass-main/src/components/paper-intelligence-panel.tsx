import { useMemo, useRef, useState } from "react";
import { AlertCircle, FileText, Loader2, Quote, ScanSearch } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  generatePaperIntelligence,
  getPaperEvidence,
  getPaperIntelligence,
  INTELLIGENCE_SECTION_ORDER,
  PaperIntelligenceNotFoundError,
  type EvidencePassage,
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
 * page. The summary, the references and any quote come from the
 * ALREADY-LOADED analysis, so navigation stays instant.
 *
 * THE SOURCE TEXT IS FETCHED LAZILY, ONCE
 * ---------------------------------------
 * The chunk a reference points at is not part of the stored analysis. It
 * is read from the server the first time a reader opens ANY evidence
 * reference for this paper — never on page load — and then cached, so
 * opening further references, or the same one again, costs nothing. That
 * read is an exact lookup of the cited chunks: no model call, and no AI
 * allowance spent. It is never retried automatically.
 *
 * The cache is per ANALYSIS VERSION, not just per paper: the analysis's
 * `generated_at` is in the key. Once a reader has asked for a paper's
 * source text, a different analysis of it — however it arrived — is read
 * once in its turn, and is never shown the previous version's result.
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
 * Four different things, never presented as one another:
 *
 *   1. The summary is the model's reading of the paper, labelled so.
 *   2. A quote, when one was persisted, is a verbatim passage the
 *      validator checked against the chunk it came from.
 *   3. The extracted source text is the chunk a reference points at, as
 *      the PDF extractor produced it. It was not chosen or written by a
 *      model, and it is NOT a quotation — it is labelled as extracted.
 *   4. A reference whose chunk cannot be resolved shows its LOCATION
 *      only. Nothing is substituted for it.
 *
 * A quote is highlighted inside the extracted text only when it occurs
 * there character for character, in the chunk with the SAME page and
 * chunk id — never in a neighbour. The extracted string is never edited:
 * CSS decides how its whitespace looks, and the text that is matched
 * against is the text that arrived.
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

/** What the inspector currently knows about this paper's source passages. */
interface PassageState {
  loading: boolean;
  failed: boolean;
  /** The read finished. `byReference` is still null when the paper has
   *  no stored analysis for the server to resolve. */
  loaded: boolean;
  /** `${page}:${chunk_id}` -> the server's answer for that reference. */
  byReference: Map<string, EvidencePassage> | null;
  reindexed: boolean;
  retry: () => void;
}

/** The pair that IS a reference. chunk_id alone repeats on every page. */
function referenceKey(page: number, chunkId: number): string {
  return `${page}:${chunkId}`;
}

function hasQuote(item: IntelligenceEvidence): item is IntelligenceEvidence & { quote: string } {
  return typeof item.quote === "string" && item.quote.trim().length > 0;
}

/**
 * Split a passage around the quotes that occur in it EXACTLY.
 *
 * Nothing is normalised on either side: `indexOf` is a character-for-
 * character test, the same one the server applied when it accepted the
 * quote. A quote that is not found produces no highlight. Joining the
 * returned segments always gives back `text` unchanged.
 */
function segmentPassage(
  text: string,
  quotes: string[],
): { segments: { text: string; quoted: boolean }[]; matched: number } {
  const ranges: [number, number][] = [];
  for (const quote of quotes) {
    const at = text.indexOf(quote);
    if (at !== -1) ranges.push([at, at + quote.length]);
  }
  const matched = ranges.length;
  ranges.sort((a, b) => a[0] - b[0]);

  // Two quotes from one chunk can overlap; one highlight covers both.
  const merged: [number, number][] = [];
  for (const [start, end] of ranges) {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }

  const segments: { text: string; quoted: boolean }[] = [];
  let cursor = 0;
  for (const [start, end] of merged) {
    if (start > cursor) segments.push({ text: text.slice(cursor, start), quoted: false });
    segments.push({ text: text.slice(start, end), quoted: true });
    cursor = end;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), quoted: false });

  return { segments, matched };
}

/** One reference's chunk, or a plain statement that it is unavailable. */
function SourcePassage({
  page,
  quotes,
  passage,
}: {
  page: number;
  /** Only the quotes that cite THIS (page, chunk_id). */
  quotes: string[];
  passage: EvidencePassage | undefined;
}) {
  const text =
    passage && passage.status === "found" && typeof passage.text === "string"
      ? passage.text
      : "";

  if (!text) {
    // Unresolved, or never returned. No neighbouring chunk stands in.
    return (
      <p className="text-[12px] leading-[1.6] text-muted-foreground">
        The source text for this reference is unavailable. The page reference
        above still applies.
      </p>
    );
  }

  const { segments, matched } = segmentPassage(text, quotes);

  return (
    <div role="group" aria-label={`Extracted source text from page ${page}`}>
      <p className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
        Extracted source text
      </p>
      {/* The string is rendered exactly as it arrived. Only CSS touches
          its whitespace, so PDF line breaks read as ordinary spacing. */}
      <p
        data-source-text=""
        className="mt-1.5 min-w-0 whitespace-normal break-words rounded-md bg-muted/40 px-2.5 py-2 text-[13px] leading-[1.7] text-foreground/85 [overflow-wrap:anywhere]"
      >
        {segments.map((segment, index) =>
          segment.quoted ? (
            <mark
              key={index}
              className="rounded-sm bg-primary/15 px-0.5 text-foreground"
            >
              {segment.text}
            </mark>
          ) : (
            <span key={index}>{segment.text}</span>
          ),
        )}
      </p>
      {matched > 0 && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          Highlighted: the quoted passage above, found word for word in this text.
        </p>
      )}
      {quotes.length > matched && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          {matched > 0 ? "Another quoted passage above" : "The quoted passage above"}{" "}
          was not found word for word in this text.
        </p>
      )}
    </div>
  );
}

/** The source text for every chunk cited on one page. */
function SourcePassages({
  page,
  references,
  passages,
  anyShown,
}: {
  page: number;
  references: { chunkId: number; quotes: string[] }[];
  passages: PassageState;
  /** At least one of these references resolved to text that is displayed. */
  anyShown: boolean;
}) {
  if (passages.loading) {
    return (
      <p
        role="status"
        className="mt-3 flex items-center gap-2 text-[12px] text-muted-foreground"
      >
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
        Loading source text…
      </p>
    );
  }

  if (passages.failed) {
    // The reference itself came with the analysis and is untouched.
    return (
      <div className="mt-3 text-[12px] leading-[1.6] text-muted-foreground">
        <p>
          The source text could not be loaded right now. The page reference
          above is unchanged.
        </p>
        <button
          type="button"
          onClick={passages.retry}
          className="mt-1 underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          Try loading the source text again
        </button>
      </div>
    );
  }

  if (!passages.loaded) return null;

  return (
    <div className="mt-3 space-y-3 border-t border-border pt-3">
      {anyShown && (
        <p className="text-[11px] leading-[1.6] text-muted-foreground">
          Below is the text extracted from page {page} that this reference
          points to. It is not a quotation, and its line breaks and symbols may
          differ from the PDF.
        </p>
      )}
      {passages.reindexed && (
        <p className="text-[11px] leading-[1.6] text-muted-foreground">
          This paper was re-indexed or changed after this analysis was
          generated, so its source text may not be the text the analysis read.
          Regenerating the analysis refreshes it.
        </p>
      )}
      {references.map((reference) => (
        <SourcePassage
          key={reference.chunkId}
          page={page}
          quotes={reference.quotes}
          passage={passages.byReference?.get(referenceKey(page, reference.chunkId))}
        />
      ))}
    </div>
  );
}

function EvidenceDetail({
  page,
  items,
  passages,
}: {
  page: number;
  items: IntelligenceEvidence[];
  passages: PassageState;
}) {
  const quoted = items.filter(hasQuote);

  // The distinct chunks cited on this page, in first-appearance order,
  // each carrying ONLY the quotes that cite that exact chunk. A quote is
  // never offered to a different chunk's passage.
  const references: { chunkId: number; quotes: string[] }[] = [];
  for (const item of items) {
    const chunkId = Number(item.chunk_id);
    let reference = references.find((entry) => entry.chunkId === chunkId);
    if (!reference) {
      reference = { chunkId, quotes: [] };
      references.push(reference);
    }
    if (hasQuote(item)) reference.quotes.push(item.quote);
  }

  const sourceTextShown =
    passages.loaded &&
    !passages.loading &&
    !passages.failed &&
    references.some((reference) => {
      const passage = passages.byReference?.get(referenceKey(page, reference.chunkId));
      return passage?.status === "found" && !!passage.text;
    });

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
      ) : sourceTextShown ? (
        // No stored quote, but the chunk itself resolved and is shown
        // below — so pointing the reader away to the PDF would be wrong.
        <p className="mt-2 text-[13px] leading-[1.7] text-muted-foreground">
          Evidence location: Page {page}. No quoted passage was stored for
          this reference — the text extracted from this page is shown below.
        </p>
      ) : (
        // No stored passage. Say so plainly — never synthesise one, and
        // never reuse the model's summary as though the paper said it.
        <p className="mt-2 text-[13px] leading-[1.7] text-muted-foreground">
          Evidence location: Page {page}. No passage was stored for this
          reference — open the page to read it in the paper.
        </p>
      )}

      <SourcePassages
        page={page}
        references={references}
        passages={passages}
        anyShown={sourceTextShown}
      />

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
  onInspect,
  passages,
}: {
  name: IntelligenceSectionName;
  section: IntelligenceSection;
  onEvidenceClick: (page: number) => void;
  /** A reader opened an evidence reference: the source text is now wanted. */
  onInspect: () => void;
  passages: PassageState;
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
    // 3. Ask for the source text — only when a reference is being OPENED,
    //    never when one is closed. Idempotent: the first open for this
    //    paper starts the one read, and every later open changes nothing.
    if (openPage !== page) onInspect();
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
        <EvidenceDetail page={openPage} items={openItems} passages={passages} />
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

  // The passages belong to ONE analysis: they answer "what do this
  // analysis's references point at". So the stamp of the analysis on
  // screen is part of their key. However the displayed analysis comes to
  // change — regenerated here, regenerated in another tab, or saved by a
  // request this browser saw fail — it has a different stamp, therefore a
  // different key, and can never be handed the previous analysis's
  // result. Nothing has to remember to invalidate.
  const evidenceKey = queryKeys.paperEvidence(userId, paperId, data?.generated_at);

  // The source passages are wanted only once a reader opens an evidence
  // reference, and the wish is remembered PER user and paper: this panel
  // instance outlives a change of paper, and a flag that stayed set would
  // start the read on the next paper's page load.
  const inspectionScope = `${userId ?? ""}:${paperId}`;
  const [inspectedScope, setInspectedScope] = useState<string | null>(null);
  const passagesRequested = inspectedScope === inspectionScope;

  const passageQuery = useQuery({
    queryKey: evidenceKey,
    queryFn: () => getPaperEvidence(paperId),
    // No analysis, no references to resolve — and never before a reader
    // has asked. A signed-out render never reaches the API.
    enabled: !!userId && !!paperId && !!data && passagesRequested,
    // Never stale WITHIN one analysis version: the key above carries the
    // version, so a different analysis is a different query rather than a
    // stale one, and for a given version the cited chunks change only if
    // the paper is re-indexed. One read therefore serves every reference
    // opened for that version.
    staleTime: Infinity,
    // Never retried on its own, on any trigger. A failed read stays
    // failed until the reader asks again.
    retry: false,
    retryOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const passageResult = passageQuery.data;
  const passagesByReference = useMemo(() => {
    if (!passageResult) return null;
    const byReference = new Map<string, EvidencePassage>();
    for (const item of passageResult.items) {
      byReference.set(referenceKey(Number(item.page), Number(item.chunk_id)), item);
    }
    return byReference;
  }, [passageResult]);

  const passages: PassageState = {
    loading: passagesRequested && passageQuery.isFetching,
    failed: passagesRequested && passageQuery.isError && !passageQuery.isFetching,
    loaded: passageQuery.isSuccess,
    byReference: passagesByReference,
    reindexed: passageResult?.reindexed_since_analysis === true,
    retry: () => {
      void passageQuery.refetch();
    },
  };

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
      // The source passages need nothing done here. They are cached under
      // the analysis's own stamp, so a new analysis has a new key and
      // starts with no passages: if the reader has opened evidence for
      // this paper they are read once for the new version, and if not,
      // nothing is fetched until they do. Invalidating explicitly would be
      // wrong as well as redundant — at this point the panel still
      // observes the PREVIOUS version's key, so the refetch it triggers
      // would file the new analysis's passages under the old one. And a
      // run the server discarded returns the analysis already on screen,
      // whose passages are still exactly its passages.
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
                onInspect={() => setInspectedScope(inspectionScope)}
                passages={passages}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
