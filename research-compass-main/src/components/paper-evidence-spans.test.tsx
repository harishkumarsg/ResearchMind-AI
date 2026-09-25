/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Phase 3.3 — verified verbatim spans in the Evidence Inspector.
 *
 * The inspector was built in Phase 2B step 5 already able to render a
 * `quote`, but production never produced one: the generation prompt
 * forbade the field, so every stored reference was location-only. Phase
 * 3.2 changed that, which means the span path is now REACHABLE in
 * production for the first time and its behaviour has to be pinned.
 *
 * This suite covers the cases the shipped Phase 2B suite could not
 * meaningfully exercise while `quote` was always null:
 *
 *   * a span renders as a passage, visually separated from the summary;
 *   * rendering depends on the EVIDENCE OBJECT, never on schema_version —
 *     a "1" row carrying a span must show it, and a "2" row without one
 *     must fall back to location-only;
 *   * a non-string `quote` (the malformed-payload case) degrades to
 *     location-only rather than crashing the workspace or fabricating
 *     text;
 *   * one reference's span never appears under another's.
 *
 * Offline: the API module is mocked, so no network and no provider.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const auth = vi.hoisted(() => ({ userId: "user-1" as string | undefined }));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    user: auth.userId ? { id: auth.userId } : null,
    session: auth.userId ? { access_token: "t" } : null,
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<any>("@/lib/api");
  return { ...actual, getPaperIntelligence: vi.fn() };
});

import { PaperIntelligencePanel } from "@/components/paper-intelligence-panel";
import { getPaperIntelligence, INTELLIGENCE_SECTION_ORDER } from "@/lib/api";

const PAPER_ID = "11111111-1111-1111-1111-111111111111";

const SUMMARY = "The model's reading of this section.";
const SPAN_P3 = "A two-stage hybrid framework is proposed for defect detection.";
const SPAN_P7 = "The approach depends on prompt quality and careful tuning.";

/** One section. Every other section is left location-only so a test's
 *  assertions cannot be satisfied by a different section's evidence. */
function section(overrides: any = {}) {
  return {
    status: "answered",
    summary: SUMMARY,
    evidence: [{ page: 3, chunk_id: 0, quote: null }],
    ...overrides,
  };
}

function result(overrides: any = {}) {
  const intelligence: any = {};
  for (const name of INTELLIGENCE_SECTION_ORDER) intelligence[name] = section();
  return {
    paper_id: PAPER_ID,
    paper: "ETASR_18859.pdf",
    intelligence,
    generated_at: "2026-09-25T10:15:17+00:00",
    model: "openai/gpt-oss-120b",
    schema_version: "2",
    superseded: false,
    ...overrides,
  };
}

/** A result whose FIRST section carries the given evidence array. */
function withFirstSectionEvidence(evidence: any[], resultOverrides: any = {}) {
  const base = result(resultOverrides);
  base.intelligence[INTELLIGENCE_SECTION_ORDER[0]] = section({ evidence });
  return base;
}

function renderPanel(onEvidenceClick = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const utils = render(
    (
      <QueryClientProvider client={queryClient}>
        <PaperIntelligencePanel paperId={PAPER_ID} onEvidenceClick={onEvidenceClick} />
      </QueryClientProvider>
    ) as ReactNode,
  );
  return { ...utils, onEvidenceClick };
}

/** Open the inspector for a page inside the first section. */
async function openPage(page: number) {
  const buttons = await screen.findAllByRole("button", { name: `Go to page ${page}` });
  fireEvent.click(buttons[0]);
  return buttons[0];
}

function inspector(page: number) {
  return screen.getByRole("region", { name: `Evidence on page ${page}` });
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.userId = "user-1";
});

// ----------------------------------------------------------------------
// 1, 2, 3 — a verified span renders, distinctly, without losing the page
// ----------------------------------------------------------------------
describe("a verified span renders", () => {
  it("shows the span as a passage from the paper", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: SPAN_P3 }]) as any,
    );
    renderPanel();
    await openPage(3);

    const region = inspector(3);
    expect(within(region).getByText(SPAN_P3)).toBeInTheDocument();
    expect(within(region).getByText("Passage from the paper")).toBeInTheDocument();
  });

  it("renders the span in a blockquote, which the summary is not", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: SPAN_P3 }]) as any,
    );
    const { container } = renderPanel();
    await openPage(3);

    const quotes = Array.from(container.querySelectorAll("blockquote"));
    expect(quotes.length).toBe(1);
    expect(quotes[0].textContent).toBe(SPAN_P3);
    // The summary is emphatically NOT quoted: it is the model's reading.
    expect(quotes[0].textContent).not.toContain(SUMMARY);
  });

  it("keeps the summary labelled as AI interpretation alongside the span", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: SPAN_P3 }]) as any,
    );
    renderPanel();
    await openPage(3);

    expect(screen.getAllByText("AI interpretation").length).toBeGreaterThan(0);
    expect(screen.getAllByText(SUMMARY).length).toBeGreaterThan(0);
  });

  it("still shows the page reference and still drives the PDF", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: SPAN_P3 }]) as any,
    );
    const { onEvidenceClick } = renderPanel();
    await openPage(3);

    // The existing citation -> viewer path is unchanged.
    expect(onEvidenceClick).toHaveBeenCalledWith(3);
    const region = inspector(3);
    expect(within(region).getByText(/Evidence · Page 3/)).toBeInTheDocument();
    expect(within(region).getByText(/page 3 · chunk 0/)).toBeInTheDocument();
  });

  it("wraps a long span rather than truncating or clamping it", async () => {
    const long = "L".repeat(200);
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: long }]) as any,
    );
    const { container } = renderPanel();
    await openPage(3);

    const quote = container.querySelector("blockquote")!;
    // Whole span present — no ellipsis, no line-clamp, no truncate.
    expect(quote.textContent).toBe(long);
    expect(quote.className).not.toMatch(/line-clamp|truncate|overflow-hidden/);
  });
});

// ----------------------------------------------------------------------
// 4, 7, 8 — the evidence object decides, never schema_version
// ----------------------------------------------------------------------
describe("rendering depends on the evidence object, not schema_version", () => {
  it("renders a span carried by a schema_version 1 row", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence(
        [{ page: 3, chunk_id: 0, quote: SPAN_P3 }],
        { schema_version: "1" },
      ) as any,
    );
    renderPanel();
    await openPage(3);

    expect(within(inspector(3)).getByText(SPAN_P3)).toBeInTheDocument();
  });

  it("falls back to location-only on a schema_version 2 row with no span", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence(
        [{ page: 3, chunk_id: 0, quote: null }],
        { schema_version: "2" },
      ) as any,
    );
    renderPanel();
    await openPage(3);

    const region = inspector(3);
    expect(within(region).getByText(/No passage was stored for this reference/)).toBeInTheDocument();
    expect(within(region).queryByText(SPAN_P3)).not.toBeInTheDocument();
  });

  it("renders a whole schema_version 1 analysis with no spans at all", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      result({ schema_version: "1" }) as any,
    );
    renderPanel();

    // Backward compatibility: the ten sections render as before.
    expect(await screen.findByText("Research Problem")).toBeInTheDocument();
    expect(screen.getByText("Reproducibility")).toBeInTheDocument();
    await openPage(3);
    expect(within(inspector(3)).getByText(/No passage was stored/)).toBeInTheDocument();
  });

  it("never renders the schema_version itself as a trust signal", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: SPAN_P3 }]) as any,
    );
    const { container } = renderPanel();
    await openPage(3);

    expect(container.textContent).not.toMatch(/schema[_ ]version/i);
  });
});

// ----------------------------------------------------------------------
// 9 — malformed payloads fail closed
// ----------------------------------------------------------------------
describe("a malformed quote fails closed", () => {
  const MALFORMED: Array<[string, unknown]> = [
    ["a number", 42],
    ["an object", { text: "fabricated" }],
    ["an array", ["fabricated"]],
    ["a boolean", true],
    ["whitespace only", "   "],
    ["an empty string", ""],
  ];

  for (const [label, value] of MALFORMED) {
    it(`renders location-only when the quote is ${label}`, async () => {
      vi.mocked(getPaperIntelligence).mockResolvedValue(
        withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: value }]) as any,
      );
      const { container } = renderPanel();
      await openPage(3);

      const region = inspector(3);
      // Degrades to the safe state…
      expect(within(region).getByText(/No passage was stored for this reference/)).toBeInTheDocument();
      // …renders no blockquote at all…
      expect(container.querySelectorAll("blockquote").length).toBe(0);
      // …and never leaks a serialised object or a fabricated string.
      expect(region.textContent).not.toContain("fabricated");
      expect(region.textContent).not.toContain("[object Object]");
    });
  }

  it("does not crash the workspace when the quote is malformed", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: { a: 1 } }]) as any,
    );
    renderPanel();
    await openPage(3);

    // The panel and the other sections are still there.
    expect(screen.getByText("Paper Intelligence")).toBeInTheDocument();
    expect(screen.getByText("Reproducibility")).toBeInTheDocument();
  });

  it("never substitutes the summary for a missing span", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: null }]) as any,
    );
    const { container } = renderPanel();
    await openPage(3);

    const region = inspector(3);
    expect(region.textContent).not.toContain(SUMMARY);
    expect(container.querySelectorAll("blockquote").length).toBe(0);
  });
});

// ----------------------------------------------------------------------
// 10, 11 — spans stay with their own reference
// ----------------------------------------------------------------------
describe("span isolation between references", () => {
  it("shows only the span belonging to the opened page", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([
        { page: 3, chunk_id: 0, quote: SPAN_P3 },
        { page: 7, chunk_id: 1, quote: SPAN_P7 },
      ]) as any,
    );
    renderPanel();

    await openPage(3);
    const onThree = inspector(3);
    expect(within(onThree).getByText(SPAN_P3)).toBeInTheDocument();
    expect(within(onThree).queryByText(SPAN_P7)).not.toBeInTheDocument();
  });

  it("shows the other page's span only when that page is opened", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([
        { page: 3, chunk_id: 0, quote: SPAN_P3 },
        { page: 7, chunk_id: 1, quote: SPAN_P7 },
      ]) as any,
    );
    renderPanel();

    await openPage(7);
    const onSeven = inspector(7);
    expect(within(onSeven).getByText(SPAN_P7)).toBeInTheDocument();
    expect(within(onSeven).queryByText(SPAN_P3)).not.toBeInTheDocument();
  });

  it("renders each span independently when one page holds two chunks", async () => {
    const second = "Stage two performs zero-shot classification.";
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([
        { page: 3, chunk_id: 0, quote: SPAN_P3 },
        { page: 3, chunk_id: 1, quote: second },
      ]) as any,
    );
    const { container } = renderPanel();
    await openPage(3);

    const region = inspector(3);
    expect(within(region).getByText(SPAN_P3)).toBeInTheDocument();
    expect(within(region).getByText(second)).toBeInTheDocument();
    expect(container.querySelectorAll("blockquote").length).toBe(2);
  });

  it("keeps a span out of a sibling reference that has none", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([
        { page: 3, chunk_id: 0, quote: SPAN_P3 },
        { page: 7, chunk_id: 0, quote: null },
      ]) as any,
    );
    renderPanel();

    await openPage(7);
    const onSeven = inspector(7);
    expect(within(onSeven).getByText(/No passage was stored/)).toBeInTheDocument();
    expect(within(onSeven).queryByText(SPAN_P3)).not.toBeInTheDocument();
  });
});

// ----------------------------------------------------------------------
// Trust wording — the span must not claim more than it is
// ----------------------------------------------------------------------
describe("trust wording", () => {
  it("does not claim human, external or fact-check verification", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: SPAN_P3 }]) as any,
    );
    const { container } = renderPanel();
    await openPage(3);

    for (const claim of [
      "verified by humans",
      "human verified",
      "fact-checked",
      "fact checked",
      "independently verified",
      "externally verified",
      "peer reviewed",
    ]) {
      expect(container.textContent?.toLowerCase()).not.toContain(claim);
    }
  });

  it("does not describe the span as AI-generated", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      withFirstSectionEvidence([{ page: 3, chunk_id: 0, quote: SPAN_P3 }]) as any,
    );
    renderPanel();
    await openPage(3);

    const region = inspector(3);
    // "AI interpretation" belongs to the summary, outside this region.
    expect(within(region).queryByText("AI interpretation")).not.toBeInTheDocument();
    expect(within(region).getByText("Passage from the paper")).toBeInTheDocument();
  });
});
