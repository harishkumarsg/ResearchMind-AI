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
 * The third is performance: everything the inspector shows already
 * arrived with the analysis, so opening it must cost no request.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
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

beforeEach(() => {
  vi.clearAllMocks();
  auth.userId = "user-1";
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

  it("makes no extra request when evidence is inspected", async () => {
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

    // Everything shown came from the already-loaded analysis.
    expect(getPaperIntelligence).toHaveBeenCalledTimes(1);
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
