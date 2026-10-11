/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Evidence Inspector — the chain from claim to page.
 *
 *   section -> evidence -> page -> PDF
 *
 * Two properties carry the weight here, and both are about trust rather
 * than mechanics:
 *
 *   * the AI's summary and the paper's own words are never presented as
 *     the same kind of thing. A section labels its summary as
 *     interpretation; a passage is labelled as coming from the paper;
 *   * a reference with no stored quote shows its LOCATION and nothing
 *     else. It must never fall back to replaying the summary as though
 *     the paper had said it.
 *
 * The third is cost. The summary, references and quotes arrive with the
 * analysis. The source text a reference points at is read separately —
 * once per paper, the first time a reader opens any reference, and never
 * again per click, per section, or on page load.
 *
 * The second describe block covers that source text: it is the paper's
 * extracted chunk, labelled as extracted, never as a quotation; a quote
 * is highlighted in it only where it occurs character for character, in
 * the chunk with the same page AND chunk id; and a chunk that cannot be
 * resolved leaves the location-only fallback exactly as it was.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
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
  return {
    ...actual,
    getPaperIntelligence: vi.fn(),
    getPaperEvidence: vi.fn(),
    generatePaperIntelligence: vi.fn(),
  };
});

import { PaperIntelligencePanel } from "@/components/paper-intelligence-panel";
import {
  generatePaperIntelligence,
  getPaperEvidence,
  getPaperIntelligence,
  INTELLIGENCE_SECTION_ORDER,
} from "@/lib/api";
import { queryKeys } from "@/lib/query-keys";

const PAPER_ID = "11111111-1111-1111-1111-111111111111";
const OTHER_PAPER_ID = "22222222-2222-2222-2222-222222222222";

/** The server's answer for a paper's cited references. */
function passages(items: any[] = [], overrides: any = {}) {
  return {
    paper_id: PAPER_ID,
    generated_at: "2026-09-22T11:30:00+00:00",
    reindexed_since_analysis: false,
    items,
    ...overrides,
  };
}

function found(page: number, chunk_id: number, text: string) {
  return { page, chunk_id, status: "found", text };
}

function unresolved(page: number, chunk_id: number, status: string) {
  return { page, chunk_id, status, text: null };
}

function section(overrides: any = {}) {
  return {
    status: "answered",
    summary: "A summary of this section.",
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
    generated_at: "2026-09-22T11:30:00+00:00",
    model: "openai/gpt-oss-120b",
    schema_version: "1",
    superseded: false,
    ...overrides,
  };
}

function renderPanel(onEvidenceClick = vi.fn(), paperId = PAPER_ID) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const tree = (id: string) =>
    (
      <QueryClientProvider client={queryClient}>
        <PaperIntelligencePanel paperId={id} onEvidenceClick={onEvidenceClick} />
      </QueryClientProvider>
    ) as ReactNode;
  const utils = render(tree(paperId));
  return {
    ...utils,
    onEvidenceClick,
    queryClient,
    showPaper: (id: string) => utils.rerender(tree(id)),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.userId = "user-1";
  // Reset, not just clear: several tests queue one-shot return values,
  // and a leftover one must never answer the next test's first read.
  vi.mocked(getPaperIntelligence).mockReset();
  // By default the server resolves nothing, so the tests that are not
  // about source text see exactly the location-only inspector.
  vi.mocked(getPaperEvidence).mockReset();
  vi.mocked(getPaperEvidence).mockResolvedValue(passages() as any);
  vi.mocked(generatePaperIntelligence).mockReset();
});

describe("Evidence inspector", () => {
  it("still navigates the PDF when a page reference is clicked", async () => {
    const data = result();
    data.intelligence.key_results = section({
      evidence: [{ page: 4, chunk_id: 0, quote: null }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    const { onEvidenceClick } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 4" }));

    // The pre-existing behaviour, preserved and fired on every click.
    expect(onEvidenceClick).toHaveBeenCalledWith(4);
  });

  it("opens the inspector for the clicked page", async () => {
    const data = result();
    data.intelligence.key_results = section({
      evidence: [{ page: 4, chunk_id: 0, quote: null }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    expect(screen.queryByRole("region", { name: /evidence on page 4/i })).toBeNull();

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 4" }));

    expect(screen.getByRole("region", { name: /evidence on page 4/i })).toBeInTheDocument();
  });

  it("displays a persisted quote as a passage from the paper", async () => {
    const data = result();
    data.intelligence.methodology = section({
      evidence: [
        { page: 3, chunk_id: 0, quote: "A two-stage hybrid framework is proposed." },
      ],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    const chips = await screen.findAllByRole("button", { name: "Go to page 3" });
    fireEvent.click(chips[2]); // methodology is the third section

    const region = screen.getByRole("region", { name: /evidence on page 3/i });
    expect(region).toHaveTextContent("Passage from the paper");
    expect(region).toHaveTextContent("A two-stage hybrid framework is proposed.");
  });

  it("shows a location-only state when no quote was stored, inventing nothing", async () => {
    const data = result();
    data.intelligence.key_results = section({
      summary: "AI-WRITTEN-SUMMARY-MARKER",
      evidence: [{ page: 6, chunk_id: 2, quote: null }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 6" }));

    const region = screen.getByRole("region", { name: /evidence on page 6/i });
    expect(region).toHaveTextContent(/evidence location: page 6/i);
    expect(region).not.toHaveTextContent("Passage from the paper");
    // The summary must never be replayed as though the paper said it.
    expect(region).not.toHaveTextContent("AI-WRITTEN-SUMMARY-MARKER");
  });

  it("treats a blank quote as no quote", async () => {
    const data = result();
    data.intelligence.dataset = section({
      evidence: [{ page: 8, chunk_id: 0, quote: "   " }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 8" }));

    const region = screen.getByRole("region", { name: /evidence on page 8/i });
    expect(region).toHaveTextContent(/evidence location: page 8/i);
    expect(region).not.toHaveTextContent("Passage from the paper");
  });

  it("distinguishes AI interpretation from source evidence", async () => {
    const data = result();
    data.intelligence.methodology = section({
      evidence: [{ page: 3, chunk_id: 0, quote: "Verbatim text from the paper." }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    const labels = await screen.findAllByText("AI interpretation");
    expect(labels.length).toBe(10);

    const chips = screen.getAllByRole("button", { name: "Go to page 3" });
    fireEvent.click(chips[2]);

    const region = screen.getByRole("region", { name: /evidence on page 3/i });
    expect(region).toHaveTextContent("Passage from the paper");
    expect(region).not.toHaveTextContent("AI interpretation");
  });

  it("does not label a not_specified section as AI interpretation", async () => {
    const data = result();
    data.intelligence.reproducibility = {
      status: "not_specified",
      summary: null,
      evidence: [],
    };
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    await screen.findByText("Not specified");
    expect(screen.getAllByText("AI interpretation").length).toBe(9);
  });

  it("opens the inspector independently for multiple evidence pages", async () => {
    const data = result();
    data.intelligence.contributions = section({
      evidence: [
        { page: 5, chunk_id: 0, quote: "Passage from page five." },
        { page: 9, chunk_id: 1, quote: null },
      ],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    const { onEvidenceClick } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 5" }));
    expect(screen.getByRole("region", { name: /evidence on page 5/i })).toHaveTextContent(
      "Passage from page five.",
    );

    fireEvent.click(screen.getByRole("button", { name: "Go to page 9" }));
    // Switching replaces the open inspector rather than stacking them.
    expect(screen.queryByRole("region", { name: /evidence on page 5/i })).toBeNull();
    expect(screen.getByRole("region", { name: /evidence on page 9/i })).toHaveTextContent(
      /evidence location: page 9/i,
    );

    expect(onEvidenceClick).toHaveBeenNthCalledWith(1, 5);
    expect(onEvidenceClick).toHaveBeenNthCalledWith(2, 9);
  });

  it("shows every stored passage when one page carries several chunks", async () => {
    const data = result();
    data.intelligence.dataset = section({
      evidence: [
        { page: 3, chunk_id: 0, quote: "First stored passage." },
        { page: 3, chunk_id: 1, quote: "Second stored passage." },
      ],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    const chips = await screen.findAllByRole("button", { name: "Go to page 3" });
    fireEvent.click(chips[3]); // dataset is the fourth section

    const region = screen.getByRole("region", { name: /evidence on page 3/i });
    expect(region).toHaveTextContent("First stored passage.");
    expect(region).toHaveTextContent("Second stored passage.");
  });

  it("reports expansion state to assistive technology", async () => {
    const data = result();
    data.intelligence.key_results = section({
      evidence: [{ page: 4, chunk_id: 0, quote: null }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    const chip = await screen.findByRole("button", { name: "Go to page 4" });
    expect(chip).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(chip);
    expect(chip).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(chip);
    expect(chip).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: /evidence on page 4/i })).toBeNull();
  });

  it("renders no raw JSON in the inspector", async () => {
    const data = result();
    data.intelligence.key_results = section({
      evidence: [{ page: 4, chunk_id: 2, quote: "A stored passage." }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    const { container } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 4" }));

    expect(container.textContent).not.toContain("chunk_id");
    expect(container.textContent).not.toContain("{");
    expect(container.textContent).not.toContain("[object");
  });

  it("reads the analysis once and the source text at most once, never per click", async () => {
    // Deliberately changed from "makes no extra request": the source text
    // is not part of the stored analysis, so the FIRST opened reference
    // costs one read. What must still hold is that no click after it does.
    const data = result();
    data.intelligence.key_results = section({
      evidence: [{ page: 4, chunk_id: 0, quote: null }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    const chip = await screen.findByRole("button", { name: "Go to page 4" });
    fireEvent.click(chip);
    fireEvent.click(chip);
    fireEvent.click(chip);

    await waitFor(() => expect(getPaperEvidence).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 20));

    // The summary, references and quotes came from the loaded analysis.
    expect(getPaperIntelligence).toHaveBeenCalledTimes(1);
    expect(getPaperEvidence).toHaveBeenCalledTimes(1);
    expect(getPaperEvidence).toHaveBeenCalledWith(PAPER_ID);
  });

  it("leaves the ten sections intact while the inspector is open", async () => {
    const data = result();
    data.intelligence.key_results = section({
      evidence: [{ page: 4, chunk_id: 0, quote: null }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 4" }));

    expect(screen.getAllByRole("heading", { level: 3 }).length).toBe(10);
  });

  it("opens no inspector for a not_specified section", async () => {
    const data = result();
    data.intelligence.reproducibility = {
      status: "not_specified",
      summary: null,
      evidence: [{ page: 4, chunk_id: 0, quote: "should never be reachable" }],
    };
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    await screen.findByText("Not specified");
    // A not_specified section exposes no page chips at all, so its
    // evidence — which the validator would have rejected anyway — has no
    // route to the screen.
    expect(screen.queryByText("should never be reachable")).toBeNull();
  });
});

// ----------------------------------------------------------------------
// Evidence Passage View — the extracted source text behind a reference
// ----------------------------------------------------------------------
describe("Evidence passage view", () => {
  const QUOTE = "A two-stage hybrid framework is proposed.";
  // As the indexer stores it: hard line breaks and a ligature untouched.
  const METHOD_TEXT =
    "METHODOLOGY.\nA two-stage hybrid framework is proposed.\nIt uses a ﬁne-tuned detector.";
  const SUMMARY = "AI-WRITTEN-SUMMARY-MARKER";
  // `generated_at` identifies one stored analysis. V1 is the stamp every
  // fixture carries by default; V2 is a later, different analysis.
  const V1_AT = "2026-09-22T11:30:00+00:00";
  const V2_AT = "2026-10-06T08:00:00+00:00";

  /** An analysis whose key_results section cites exactly `evidence`. */
  function analysis(evidence: any[], overrides: any = {}) {
    const data = result(overrides);
    data.intelligence.key_results = section({ summary: SUMMARY, evidence });
    return data;
  }

  async function open(page: number) {
    fireEvent.click(await screen.findByRole("button", { name: `Go to page ${page}` }));
    return inspector(page);
  }

  function inspector(page: number) {
    return screen.getByRole("region", { name: new RegExp(`evidence on page ${page}`, "i") });
  }

  function sourceGroups(region: HTMLElement) {
    return within(region).findAllByRole("group", { name: /^Extracted source text from page/ });
  }

  function sourceText(group: HTMLElement) {
    return group.querySelector("[data-source-text]") as HTMLElement;
  }

  const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

  // -- when the read happens -------------------------------------------
  it("does not read source text when the paper opens", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );

    renderPanel();

    await screen.findAllByRole("heading", { level: 3 });
    await settle();
    expect(getPaperEvidence).not.toHaveBeenCalled();
  });

  it("reads source text when the first reference is opened", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );

    renderPanel();
    await open(4);

    await waitFor(() => expect(getPaperEvidence).toHaveBeenCalledTimes(1));
    expect(getPaperEvidence).toHaveBeenCalledWith(PAPER_ID);
  });

  it("does not read source text when a reference is closed", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );

    renderPanel();
    await open(4);
    await waitFor(() => expect(getPaperEvidence).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Go to page 4" })); // close
    await settle();

    expect(getPaperEvidence).toHaveBeenCalledTimes(1);
  });

  it("serves every later reference from the one cached read", async () => {
    const data = analysis([
      { page: 4, chunk_id: 0, quote: null },
      { page: 6, chunk_id: 1, quote: null },
    ]);
    data.intelligence.limitations = section({
      evidence: [{ page: 7, chunk_id: 0, quote: null }],
    });
    vi.mocked(getPaperIntelligence).mockResolvedValue(data as any);
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([
        found(4, 0, "Text of page four."),
        found(6, 1, "Text of page six."),
        found(7, 0, "Text of page seven."),
      ]) as any,
    );

    renderPanel();

    const four = await open(4);
    expect(sourceText((await sourceGroups(four))[0]).textContent).toBe("Text of page four.");

    const six = await open(6);
    expect(sourceText((await sourceGroups(six))[0]).textContent).toBe("Text of page six.");

    // A different SECTION, and the first reference reopened.
    const seven = await open(7);
    expect(sourceText((await sourceGroups(seven))[0]).textContent).toBe("Text of page seven.");
    await open(4);

    await settle();
    expect(getPaperEvidence).toHaveBeenCalledTimes(1);
  });

  it("keys the read by user, paper and analysis version", () => {
    expect(queryKeys.paperEvidence("user-1", PAPER_ID, V1_AT)).toEqual([
      "paper-evidence",
      "user-1",
      PAPER_ID,
      V1_AT,
    ]);
    // Each of the three scopes changes the key on its own.
    const key = JSON.stringify(queryKeys.paperEvidence("user-1", PAPER_ID, V1_AT));
    expect(JSON.stringify(queryKeys.paperEvidence("user-2", PAPER_ID, V1_AT))).not.toBe(key);
    expect(JSON.stringify(queryKeys.paperEvidence("user-1", OTHER_PAPER_ID, V1_AT))).not.toBe(key);
    expect(JSON.stringify(queryKeys.paperEvidence("user-1", PAPER_ID, V2_AT))).not.toBe(key);
    // Without a version it is the prefix covering every version of one
    // user's paper — still scoped to that user and that paper.
    expect(queryKeys.paperEvidence("user-1", PAPER_ID)).toEqual([
      "paper-evidence",
      "user-1",
      PAPER_ID,
    ]);
  });

  it("caches the read under this user, this paper and this analysis version only", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    const served = passages([found(4, 0, "Cached text.")]);
    vi.mocked(getPaperEvidence).mockResolvedValue(served as any);

    const { queryClient } = renderPanel();
    await sourceGroups(await open(4));

    expect(
      queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID, V1_AT)),
    ).toEqual(served);
    expect(
      queryClient.getQueryData(queryKeys.paperEvidence("user-2", PAPER_ID, V1_AT)),
    ).toBeUndefined();
    expect(
      queryClient.getQueryData(queryKeys.paperEvidence("user-1", OTHER_PAPER_ID, V1_AT)),
    ).toBeUndefined();
    // Another analysis of the same paper is a different cache entry.
    expect(
      queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID, V2_AT)),
    ).toBeUndefined();
    // Nothing was stored without a version.
    expect(
      queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID)),
    ).toBeUndefined();
  });

  it("does not start a read for the next paper until a reference is opened there", async () => {
    const first = analysis([{ page: 4, chunk_id: 0, quote: null }]);
    const second = analysis([{ page: 6, chunk_id: 0, quote: null }]);
    second.intelligence.methodology = section({ summary: "SECOND-PAPER-SUMMARY" });
    vi.mocked(getPaperIntelligence).mockImplementation(async (id: string) =>
      id === PAPER_ID ? (first as any) : (second as any),
    );

    const { showPaper } = renderPanel();
    await open(4);
    await waitFor(() => expect(getPaperEvidence).toHaveBeenCalledTimes(1));

    showPaper(OTHER_PAPER_ID);
    await screen.findByText("SECOND-PAPER-SUMMARY");
    await settle();

    // The reader asked for the first paper's text, not this one's.
    expect(getPaperEvidence).toHaveBeenCalledTimes(1);

    await open(6);
    await waitFor(() => expect(getPaperEvidence).toHaveBeenCalledTimes(2));
    expect(getPaperEvidence).toHaveBeenLastCalledWith(OTHER_PAPER_ID);
  });

  // -- what is shown ---------------------------------------------------
  it("shows a loading state and keeps the reference while the text is read", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 2, quote: QUOTE }]) as any,
    );
    vi.mocked(getPaperEvidence).mockReturnValue(new Promise(() => {}) as any);

    renderPanel();
    const region = await open(4);

    expect(await within(region).findByRole("status")).toHaveTextContent(/loading source text/i);
    // Nothing the analysis already supplied is withheld meanwhile.
    expect(within(region).getByText(/Evidence · Page 4/)).toBeInTheDocument();
    expect(within(region).getByText(QUOTE)).toBeInTheDocument();
    expect(within(region).getByText(/page 4 · chunk 2/)).toBeInTheDocument();
  });

  it("shows the cited chunk, labelled as extracted source text", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "The measured accuracy was 91 percent.")]) as any,
    );

    renderPanel();
    const region = await open(4);
    const [group] = await sourceGroups(region);

    expect(group.getAttribute("aria-label")).toBe("Extracted source text from page 4");
    expect(within(group).getByText("Extracted source text")).toBeInTheDocument();
    expect(sourceText(group).textContent).toBe("The measured accuracy was 91 percent.");
    expect(region).toHaveTextContent(/it is not a quotation/i);
    expect(within(region).queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps AI interpretation, quote and extracted text as three different things", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: QUOTE }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(passages([found(4, 0, METHOD_TEXT)]) as any);

    const { container } = renderPanel();
    const region = await open(4);
    const [group] = await sourceGroups(region);

    // 1. The AI's reading stays outside the inspector, labelled as such.
    expect(region.textContent).not.toContain(SUMMARY);
    expect(region).not.toHaveTextContent("AI interpretation");
    expect(screen.getAllByText("AI interpretation").length).toBe(10);

    // 2. The quote keeps its own label and its own element.
    expect(within(region).getByText("Passage from the paper")).toBeInTheDocument();
    const quotes = Array.from(container.querySelectorAll("blockquote"));
    expect(quotes.length).toBe(1);
    expect(quotes[0].textContent).toBe(QUOTE);

    // 3. The extracted text is neither: not a blockquote, and it does not
    //    borrow the quote's label.
    expect(group.querySelector("blockquote")).toBeNull();
    expect(group).not.toHaveTextContent("Passage from the paper");
    expect(group).toHaveTextContent("Extracted source text");
  });

  it("does not present extracted text as verified, checked or AI-written", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "Plain extracted text.")]) as any,
    );

    renderPanel();
    const region = await open(4);
    await sourceGroups(region);

    const shown = region.textContent?.toLowerCase() ?? "";
    for (const claim of ["verified", "fact-check", "confirmed", "ai-generated", "ai interpretation"]) {
      expect(shown).not.toContain(claim);
    }
  });

  it("renders the passage string exactly as it was returned", async () => {
    const original = "  Line one\nLine  two – ﬁnal value.\n\n3 Results ";
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(passages([found(4, 0, original)]) as any);

    renderPanel();
    const [group] = await sourceGroups(await open(4));

    // Not trimmed, not collapsed, not normalised: the DOM holds the string.
    expect(sourceText(group).textContent).toBe(original);
  });

  it("shows the source text even when no quote was stored", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 6, chunk_id: 2, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(6, 2, "Text with no stored quote.")]) as any,
    );

    const { container } = renderPanel();
    const region = await open(6);
    const [group] = await sourceGroups(region);

    expect(sourceText(group).textContent).toBe("Text with no stored quote.");
    expect(region).toHaveTextContent(/evidence location: page 6/i);
    expect(region).toHaveTextContent(/no quoted passage was stored/i);
    // No quote exists, so nothing is presented as one.
    expect(container.querySelectorAll("blockquote").length).toBe(0);
    expect(region.querySelectorAll("mark").length).toBe(0);
    expect(region).not.toHaveTextContent("Passage from the paper");
  });

  it("gives each chunk on a page its own passage, in order", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([
        { page: 4, chunk_id: 0, quote: null },
        { page: 4, chunk_id: 1, quote: null },
      ]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 1, "Second chunk."), found(4, 0, "First chunk.")]) as any,
    );

    renderPanel();
    const groups = await sourceGroups(await open(4));

    expect(groups.map((g) => sourceText(g).textContent)).toEqual([
      "First chunk.",
      "Second chunk.",
    ]);
  });

  it("shows only the passages cited on the opened page", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([
        { page: 4, chunk_id: 0, quote: null },
        { page: 9, chunk_id: 0, quote: null },
      ]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "PAGE-FOUR-TEXT"), found(9, 0, "PAGE-NINE-TEXT")]) as any,
    );

    renderPanel();
    const region = await open(4);
    await sourceGroups(region);

    expect(region).toHaveTextContent("PAGE-FOUR-TEXT");
    expect(region).not.toHaveTextContent("PAGE-NINE-TEXT");
  });

  // -- highlighting ----------------------------------------------------
  it("highlights the quote exactly where it occurs in its own chunk", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: QUOTE }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(passages([found(4, 0, METHOD_TEXT)]) as any);

    renderPanel();
    const [group] = await sourceGroups(await open(4));

    const marks = Array.from(group.querySelectorAll("mark"));
    expect(marks.length).toBe(1);
    expect(marks[0].textContent).toBe(QUOTE);
    // Highlighting split the string; it did not change a character of it.
    expect(sourceText(group).textContent).toBe(METHOD_TEXT);
    expect(group).toHaveTextContent(/highlighted: the quoted passage above/i);
    expect(group).not.toHaveTextContent(/was not found word for word/i);
  });

  it.each([
    ["differs by a line break", "A two-stage hybrid\nframework is proposed."],
    ["differs by spacing", "A two-stage  hybrid framework is proposed."],
    ["differs by case", "a two-stage hybrid framework is proposed."],
    ["differs by a ligature", "A two-stage hybrid framework is proposedﬁ."],
    ["is a paraphrase", "The authors propose a hybrid framework in two stages."],
  ])("does not highlight a quote when the passage %s", async (_label, passage) => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: QUOTE }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(passages([found(4, 0, passage)]) as any);

    renderPanel();
    const [group] = await sourceGroups(await open(4));

    // No normalisation on either side: close is not a match.
    expect(group.querySelectorAll("mark").length).toBe(0);
    expect(group).not.toHaveTextContent(/highlighted/i);
    expect(group).toHaveTextContent("The quoted passage above was not found word for word in this text.");
    expect(sourceText(group).textContent).toBe(passage);
  });

  it("never highlights a quote in a different chunk on the same page", async () => {
    // The quote cites chunk 0, but the text only occurs in chunk 1.
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([
        { page: 4, chunk_id: 0, quote: QUOTE },
        { page: 4, chunk_id: 1, quote: null },
      ]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "An unrelated passage."), found(4, 1, METHOD_TEXT)]) as any,
    );

    renderPanel();
    const region = await open(4);
    const groups = await sourceGroups(region);

    expect(groups.length).toBe(2);
    expect(region.querySelectorAll("mark").length).toBe(0);
    expect(sourceText(groups[1]).textContent).toBe(METHOD_TEXT);
    expect(groups[1]).not.toHaveTextContent(/highlighted/i);
  });

  it("never highlights a quote in the same chunk id on a different page", async () => {
    // (4, 0) carries the quote; (9, 0) shares the chunk id and the text.
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([
        { page: 4, chunk_id: 0, quote: QUOTE },
        { page: 9, chunk_id: 0, quote: null },
      ]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "An unrelated passage."), found(9, 0, METHOD_TEXT)]) as any,
    );

    renderPanel();
    const region = await open(9);
    const [group] = await sourceGroups(region);

    expect(sourceText(group).textContent).toBe(METHOD_TEXT);
    expect(region.querySelectorAll("mark").length).toBe(0);
  });

  it("highlights each quote that cites the same chunk", async () => {
    const second = "It uses a ﬁne-tuned detector.";
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([
        { page: 4, chunk_id: 0, quote: QUOTE },
        { page: 4, chunk_id: 0, quote: second },
      ]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(passages([found(4, 0, METHOD_TEXT)]) as any);

    renderPanel();
    const groups = await sourceGroups(await open(4));

    // One chunk, shown once, with both exact spans marked.
    expect(groups.length).toBe(1);
    expect(Array.from(groups[0].querySelectorAll("mark")).map((m) => m.textContent)).toEqual([
      QUOTE,
      second,
    ]);
    expect(sourceText(groups[0]).textContent).toBe(METHOD_TEXT);
  });

  // -- unresolved, missing and failed ----------------------------------
  it.each(["missing_chunk", "duplicate_chunk_identity", "malformed_payload", "over_budget"])(
    "keeps the location-only fallback when the chunk is %s",
    async (status) => {
      vi.mocked(getPaperIntelligence).mockResolvedValue(
        analysis([{ page: 6, chunk_id: 2, quote: null }]) as any,
      );
      vi.mocked(getPaperEvidence).mockResolvedValue(
        passages([unresolved(6, 2, status)]) as any,
      );

      renderPanel();
      const region = await open(6);

      expect(
        await within(region).findByText(/the source text for this reference is unavailable/i),
      ).toBeInTheDocument();
      // The existing fallback, word for word, and the reference itself.
      expect(region).toHaveTextContent(/evidence location: page 6/i);
      expect(within(region).getByText(/No passage was stored for this reference/)).toBeInTheDocument();
      expect(within(region).getByText(/page 6 · chunk 2/)).toBeInTheDocument();
      expect(within(region).queryByRole("group")).not.toBeInTheDocument();
      // Nothing announces text that is not there.
      expect(region).not.toHaveTextContent(/below is the text extracted/i);
      expect(region).not.toHaveTextContent("Extracted source text");
      expect(region.textContent).not.toContain(SUMMARY);
    },
  );

  it("never renders text that arrived with an unresolved status", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 6, chunk_id: 2, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([
        { page: 6, chunk_id: 2, status: "missing_chunk", text: "MUST-NOT-RENDER" },
      ]) as any,
    );

    renderPanel();
    const region = await open(6);

    await within(region).findByText(/the source text for this reference is unavailable/i);
    expect(region).not.toHaveTextContent("MUST-NOT-RENDER");
  });

  it("does not borrow a neighbouring chunk for a reference the server left out", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 6, chunk_id: 2, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(6, 1, "NEIGHBOUR-ON-THE-SAME-PAGE"), found(7, 2, "SAME-CHUNK-ID-ELSEWHERE")]) as any,
    );

    renderPanel();
    const region = await open(6);

    await within(region).findByText(/the source text for this reference is unavailable/i);
    expect(region).not.toHaveTextContent("NEIGHBOUR-ON-THE-SAME-PAGE");
    expect(region).not.toHaveTextContent("SAME-CHUNK-ID-ELSEWHERE");
  });

  it("treats a paper with no stored analysis on the server as unavailable, not as an error", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 6, chunk_id: 2, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(null);

    renderPanel();
    const region = await open(6);

    await within(region).findByText(/the source text for this reference is unavailable/i);
    expect(region).not.toHaveTextContent(/could not be loaded/i);
    expect(region).toHaveTextContent(/evidence location: page 6/i);
  });

  it("keeps the inspector content and shows a neutral error when the read fails", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 2, quote: QUOTE }]) as any,
    );
    vi.mocked(getPaperEvidence).mockRejectedValue(
      new Error("ECONNREFUSED 10.0.0.7:6333 qdrant-internal"),
    );

    const { container } = renderPanel();
    const region = await open(4);

    expect(
      await within(region).findByText(/the source text could not be loaded right now/i),
    ).toBeInTheDocument();
    // Whatever went wrong is not the reader's to see.
    expect(container.textContent).not.toContain("ECONNREFUSED");
    expect(container.textContent).not.toContain("qdrant-internal");
    // The evidence the analysis supplied is all still there.
    expect(within(region).getByText(/Evidence · Page 4/)).toBeInTheDocument();
    expect(within(region).getByText("Passage from the paper")).toBeInTheDocument();
    expect(within(region).getByText(QUOTE)).toBeInTheDocument();
    expect(within(region).getByText(/page 4 · chunk 2/)).toBeInTheDocument();
    expect(within(region).queryByRole("group")).not.toBeInTheDocument();
  });

  it("does not retry a failed read on its own, and retries once when asked", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence)
      .mockRejectedValueOnce(new Error("temporary"))
      .mockResolvedValueOnce(passages([found(4, 0, "Recovered text.")]) as any);

    renderPanel();
    const region = await open(4);
    await within(region).findByText(/the source text could not be loaded right now/i);

    // Closing, reopening and waiting do not trigger another attempt.
    fireEvent.click(screen.getByRole("button", { name: "Go to page 4" }));
    await open(4);
    await settle();
    expect(getPaperEvidence).toHaveBeenCalledTimes(1);

    fireEvent.click(
      within(inspector(4)).getByRole("button", { name: /try loading the source text again/i }),
    );

    const [group] = await sourceGroups(inspector(4));
    expect(sourceText(group).textContent).toBe("Recovered text.");
    expect(getPaperEvidence).toHaveBeenCalledTimes(2);
    await settle();
    // The retry repeated the READ and nothing else: no generation, and no
    // second read of the analysis.
    expect(getPaperEvidence).toHaveBeenCalledTimes(2);
    expect(getPaperEvidence).toHaveBeenLastCalledWith(PAPER_ID);
    expect(generatePaperIntelligence).not.toHaveBeenCalled();
    expect(getPaperIntelligence).toHaveBeenCalledTimes(1);
  });

  // -- re-index caution ------------------------------------------------
  it("cautions that the paper changed since the analysis, without hiding the text", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "Text after a re-index.")], {
        reindexed_since_analysis: true,
      }) as any,
    );

    renderPanel();
    const region = await open(4);
    const [group] = await sourceGroups(region);

    expect(region).toHaveTextContent(/re-indexed or changed after this analysis was generated/i);
    // A caution about the paper, not a verdict on this passage.
    expect(region).toHaveTextContent(/may not be the text the analysis read/i);
    expect(sourceText(group).textContent).toBe("Text after a re-index.");
  });

  it("shows no caution when the paper has not changed since the analysis", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "Current text.")]) as any,
    );

    renderPanel();
    const region = await open(4);
    await sourceGroups(region);

    expect(region).not.toHaveTextContent(/re-indexed/i);
  });

  // -- layout ----------------------------------------------------------
  it("wraps long text instead of overflowing, truncating or clamping it", async () => {
    const long = "https://example.org/" + "x".repeat(600) + " " + "W".repeat(400);
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(passages([found(4, 0, long)]) as any);

    renderPanel();
    const [group] = await sourceGroups(await open(4));
    const text = sourceText(group);

    // The whole string is present...
    expect(text.textContent).toBe(long);
    // ...it may break anywhere, so one long token cannot push the column wide...
    expect(text.className).toMatch(/break-words/);
    expect(text.className).toMatch(/\[overflow-wrap:anywhere\]/);
    expect(text.className).toMatch(/min-w-0/);
    // ...and nothing hides or cuts it.
    expect(text.className).not.toMatch(
      /truncate|line-clamp|overflow-hidden|overflow-x|whitespace-nowrap|whitespace-pre(?!-)/,
    );
  });

  // -- nothing else happens --------------------------------------------
  it("still drives the PDF viewer when source text is shown", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(passages([found(4, 0, "Text.")]) as any);

    const { onEvidenceClick } = renderPanel();
    await sourceGroups(await open(4));

    expect(onEvidenceClick).toHaveBeenCalledTimes(1);
    expect(onEvidenceClick).toHaveBeenCalledWith(4);
    expect(screen.getAllByRole("heading", { level: 3 }).length).toBe(10);
  });

  it("never generates an analysis as part of inspecting evidence", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([
        { page: 4, chunk_id: 0, quote: null },
        { page: 6, chunk_id: 0, quote: null },
      ]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "Four."), found(6, 0, "Six.")]) as any,
    );

    renderPanel();
    await sourceGroups(await open(4));
    await sourceGroups(await open(6));
    await settle();

    expect(generatePaperIntelligence).not.toHaveBeenCalled();
    expect(getPaperIntelligence).toHaveBeenCalledTimes(1);
  });

  // -- regenerate ------------------------------------------------------
  it("re-reads the source text once after a successful Regenerate", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence)
      .mockResolvedValueOnce(passages([found(4, 0, "Text for the old analysis.")]) as any)
      .mockResolvedValueOnce(
        passages([found(4, 0, "Text for the new analysis.")], { generated_at: V2_AT }) as any,
      );
    // A regeneration is a new stored analysis, with its own stamp.
    const regenerated = analysis([{ page: 4, chunk_id: 0, quote: null }], {
      generated_at: V2_AT,
    });
    regenerated.intelligence.methodology = section({ summary: "REGENERATED-SUMMARY" });
    vi.mocked(generatePaperIntelligence).mockResolvedValue(regenerated as any);

    const { queryClient } = renderPanel();
    const [before] = await sourceGroups(await open(4));
    expect(sourceText(before).textContent).toBe("Text for the old analysis.");

    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await screen.findByText("REGENERATED-SUMMARY");

    await waitFor(() =>
      expect(inspector(4)).toHaveTextContent("Text for the new analysis."),
    );
    await settle();
    // Exactly one read for the new version — not one for it and another
    // for the version it replaced.
    expect(getPaperEvidence).toHaveBeenCalledTimes(2);
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
    expect(inspector(4)).not.toHaveTextContent("Text for the old analysis.");
    // Each version keeps its own entry; neither overwrote the other.
    expect(
      (queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID, V1_AT)) as any)
        .items[0].text,
    ).toBe("Text for the old analysis.");
    expect(
      (queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID, V2_AT)) as any)
        .items[0].text,
    ).toBe("Text for the new analysis.");
  });

  it("does not re-read when a Regenerate returns the analysis already shown", async () => {
    // The server kept a newer run and discarded this one: the object that
    // comes back is the one on screen, with the same stamp.
    const shown = analysis([{ page: 4, chunk_id: 0, quote: null }]);
    vi.mocked(getPaperIntelligence).mockResolvedValue(shown as any);
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "Text for the analysis shown.")]) as any,
    );
    vi.mocked(generatePaperIntelligence).mockResolvedValue({ ...shown, superseded: true } as any);

    renderPanel();
    await sourceGroups(await open(4));

    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await screen.findByText("A newer analysis finished first and is shown.");
    await settle();

    // Same analysis version, so its passages are still its passages.
    expect(getPaperEvidence).toHaveBeenCalledTimes(1);
    expect(inspector(4)).toHaveTextContent("Text for the analysis shown.");
  });

  it("reads no source text after a Regenerate when no reference was ever opened", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    const regenerated = analysis([{ page: 4, chunk_id: 0, quote: null }], {
      generated_at: V2_AT,
    });
    regenerated.intelligence.methodology = section({ summary: "REGENERATED-SUMMARY" });
    vi.mocked(generatePaperIntelligence).mockResolvedValue(regenerated as any);

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));
    await screen.findByText("REGENERATED-SUMMARY");
    await settle();

    expect(getPaperEvidence).not.toHaveBeenCalled();
  });

  it("keeps the cached source text when a Regenerate fails and nothing was saved", async () => {
    vi.mocked(getPaperIntelligence).mockResolvedValue(
      analysis([{ page: 4, chunk_id: 0, quote: null }]) as any,
    );
    vi.mocked(getPaperEvidence).mockResolvedValue(
      passages([found(4, 0, "Text for the stored analysis.")]) as any,
    );
    vi.mocked(generatePaperIntelligence).mockRejectedValue(
      new Error("The paper analysis could not be completed. Please try again."),
    );

    renderPanel();
    await sourceGroups(await open(4));

    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await screen.findByText("The paper analysis could not be completed. Please try again.");
    await settle();

    // The free re-read found the same analysis, so its passages stand.
    expect(getPaperIntelligence).toHaveBeenCalledTimes(2);
    expect(getPaperEvidence).toHaveBeenCalledTimes(1);
    expect(inspector(4)).toHaveTextContent("Text for the stored analysis.");
  });

  // -- the cache belongs to one analysis version -----------------------
  //
  // A stored analysis is identified by `generated_at`. The displayed
  // analysis can change WITHOUT a successful Regenerate in this tab, and
  // when it does, the passages read for the previous analysis describe a
  // different set of references. Reusing them made the inspector say
  // "unavailable" about references it had simply never asked for.

  /** v1 cites one chunk; v2 is a later analysis that cites a second one. */
  function twoVersions(marker: string) {
    const v1 = analysis([{ page: 4, chunk_id: 0, quote: null }]);
    const v2 = analysis(
      [
        { page: 4, chunk_id: 0, quote: null },
        { page: 6, chunk_id: 1, quote: null },
      ],
      { generated_at: V2_AT },
    );
    v2.intelligence.methodology = section({ summary: marker });
    const v1Passages = passages([found(4, 0, "V1 text of page four.")]);
    const v2Passages = passages(
      [found(4, 0, "V2 text of page four."), found(6, 1, "V2 text of page six.")],
      { generated_at: V2_AT },
    );
    return { v1, v2, v1Passages, v2Passages };
  }

  async function expectSecondVersionPassages() {
    // The open inspector moves to the new version's text...
    await waitFor(() => expect(inspector(4)).toHaveTextContent("V2 text of page four."));
    expect(inspector(4)).not.toHaveTextContent("V1 text of page four.");

    // ...and a reference only the NEW analysis cites has its text, not a
    // false "unavailable" from the old version's result.
    const six = await open(6);
    const [group] = await sourceGroups(six);
    expect(sourceText(group).textContent).toBe("V2 text of page six.");
    expect(six).not.toHaveTextContent(/the source text for this reference is unavailable/i);
  }

  it("reads the new version's source text when a failed request had saved a new analysis", async () => {
    // The browser saw the generation fail — a dropped connection, a proxy
    // timeout — but the server finished and stored it.
    const { v1, v2, v1Passages, v2Passages } = twoVersions("SAVED-ON-THE-SERVER-SUMMARY");
    vi.mocked(getPaperIntelligence)
      .mockResolvedValueOnce(v1 as any)
      .mockResolvedValueOnce(v2 as any);
    vi.mocked(getPaperEvidence)
      .mockResolvedValueOnce(v1Passages as any)
      .mockResolvedValueOnce(v2Passages as any);
    vi.mocked(generatePaperIntelligence).mockRejectedValue(
      new Error("The paper analysis could not be completed."),
    );

    const { queryClient } = renderPanel();
    const [before] = await sourceGroups(await open(4));
    expect(sourceText(before).textContent).toBe("V1 text of page four.");

    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await screen.findByText("The paper analysis could not be completed.");
    // The free re-read of the analysis finds what the server saved.
    await screen.findByText("SAVED-ON-THE-SERVER-SUMMARY");

    await expectSecondVersionPassages();
    await settle();

    // One read per analysis version; the old result was not reused.
    expect(getPaperEvidence).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID, V1_AT))).toEqual(
      v1Passages,
    );
    expect(queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID, V2_AT))).toEqual(
      v2Passages,
    );
    // Recovering from the failure generated nothing further.
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
    expect(getPaperIntelligence).toHaveBeenCalledTimes(2);
  });

  it("reads the new version's source text when the analysis was regenerated elsewhere", async () => {
    // Another tab or device regenerated the analysis. This tab learns of it
    // only when its analysis query refreshes — as it does on window focus
    // and on returning to the paper. No Regenerate happens here at all.
    const { v1, v2, v1Passages, v2Passages } = twoVersions("REGENERATED-ELSEWHERE-SUMMARY");
    vi.mocked(getPaperIntelligence)
      .mockResolvedValueOnce(v1 as any)
      .mockResolvedValueOnce(v2 as any);
    vi.mocked(getPaperEvidence)
      .mockResolvedValueOnce(v1Passages as any)
      .mockResolvedValueOnce(v2Passages as any);

    const { queryClient } = renderPanel();
    const [before] = await sourceGroups(await open(4));
    expect(sourceText(before).textContent).toBe("V1 text of page four.");

    await act(async () => {
      await queryClient.refetchQueries({
        queryKey: queryKeys.paperIntelligence("user-1", PAPER_ID),
        exact: true,
      });
    });
    await screen.findByText("REGENERATED-ELSEWHERE-SUMMARY");

    await expectSecondVersionPassages();
    await settle();

    expect(getPaperEvidence).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID, V1_AT))).toEqual(
      v1Passages,
    );
    expect(queryClient.getQueryData(queryKeys.paperEvidence("user-1", PAPER_ID, V2_AT))).toEqual(
      v2Passages,
    );
    expect(generatePaperIntelligence).not.toHaveBeenCalled();
  });

  it("reads nothing for a new analysis version until a reference has been opened", async () => {
    // Versioning the cache must not turn a refreshed analysis into a
    // passage read on its own: the reader has not asked for any.
    const { v1, v2 } = twoVersions("REGENERATED-ELSEWHERE-SUMMARY");
    vi.mocked(getPaperIntelligence)
      .mockResolvedValueOnce(v1 as any)
      .mockResolvedValueOnce(v2 as any);

    const { queryClient } = renderPanel();
    await screen.findAllByRole("heading", { level: 3 });

    await act(async () => {
      await queryClient.refetchQueries({
        queryKey: queryKeys.paperIntelligence("user-1", PAPER_ID),
        exact: true,
      });
    });
    await screen.findByText("REGENERATED-ELSEWHERE-SUMMARY");
    await settle();

    expect(getPaperEvidence).not.toHaveBeenCalled();

    // The first opened reference then reads the version on screen, once.
    await open(6);
    await waitFor(() => expect(getPaperEvidence).toHaveBeenCalledTimes(1));
    await settle();
    expect(getPaperEvidence).toHaveBeenCalledTimes(1);
  });
});
