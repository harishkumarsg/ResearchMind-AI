/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Paper Intelligence panel — the read-only reader.
 *
 * The properties worth pinning are the ones a careless change would
 * break silently:
 *
 *   * it NEVER generates. Opening a paper must not trigger a billable
 *     generation, so the panel calls only the read API and the empty
 *     state offers no generate action;
 *   * the query key carries BOTH the user and the paper, which is what
 *     stops one account's analysis being served to the next from cache;
 *   * evidence is shown by PAGE and clicking one drives the existing
 *     viewer, rather than dumping chunk ids or raw JSON at the reader.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
import { queryKeys } from "@/lib/query-keys";

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
  return { ...utils, queryClient, onEvidenceClick };
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.userId = "user-1";
});

describe("PaperIntelligencePanel", () => {
  it("renders all ten sections in the approved order", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());

    renderPanel();

    const headings = await screen.findAllByRole("heading", { level: 3 });
    expect(headings.map((h) => h.textContent)).toEqual([
      "Research Problem",
      "Research Objective",
      "Methodology",
      "Dataset",
      "Experimental Setup",
      "Evaluation Metrics",
      "Key Results",
      "Contributions",
      "Limitations",
      "Reproducibility",
    ]);
  });

  it("shows the summary for an answered section", async () => {
    const data = result();
    data.intelligence.methodology = section({ summary: "A two-stage hybrid framework." });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    expect(await screen.findByText("A two-stage hybrid framework.")).toBeInTheDocument();
  });

  it('shows "Not specified" for a not_specified section, with no summary', async () => {
    const data = result();
    data.intelligence.reproducibility = {
      status: "not_specified",
      summary: null,
      evidence: [],
    };
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    expect(await screen.findByText("Not specified")).toBeInTheDocument();
  });

  it("renders evidence as page references, not chunk ids", async () => {
    const data = result();
    data.intelligence.research_problem = section({
      evidence: [
        { page: 1, chunk_id: 0, quote: null },
        { page: 2, chunk_id: 4, quote: null },
      ],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    expect(await screen.findByRole("button", { name: "Go to page 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to page 2" })).toBeInTheDocument();
    // chunk_id is part of evidence identity but must not be the label.
    expect(screen.queryByText(/chunk/i)).not.toBeInTheDocument();
  });

  it("shows multiple page references for multi-page evidence", async () => {
    const data = result();
    data.intelligence.key_results = section({
      evidence: [
        { page: 5, chunk_id: 0, quote: null },
        { page: 8, chunk_id: 1, quote: null },
      ],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    expect(await screen.findByRole("button", { name: "Go to page 5" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to page 8" })).toBeInTheDocument();
  });

  it("collapses two chunks on the same page into one reference", async () => {
    const data = result();
    data.intelligence.dataset = section({
      evidence: [
        { page: 3, chunk_id: 0, quote: null },
        { page: 3, chunk_id: 1, quote: null },
      ],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    renderPanel();

    await screen.findAllByRole("heading", { level: 3 });
    expect(screen.getAllByRole("button", { name: "Go to page 3" }).length).toBe(10);
  });

  it("navigates the viewer when an evidence page is clicked", async () => {
    const data = result();
    data.intelligence.limitations = section({
      evidence: [{ page: 7, chunk_id: 0, quote: null }],
    });
    (getPaperIntelligence as any).mockResolvedValue(data);

    const { onEvidenceClick } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 7" }));

    expect(onEvidenceClick).toHaveBeenCalledWith(7);
  });

  it("shows a loading state while the analysis is fetched", () => {
    (getPaperIntelligence as any).mockReturnValue(new Promise(() => {}));

    renderPanel();

    expect(screen.getByRole("status")).toHaveTextContent(/loading paper intelligence/i);
  });

  it("shows an empty state when nothing has been generated", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);

    renderPanel();

    expect(await screen.findByText(/no analysis yet/i)).toBeInTheDocument();
  });

  it("offers no generate action in the empty state", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);

    renderPanel();

    await screen.findByText(/no analysis yet/i);
    expect(screen.queryByRole("button", { name: /generate/i })).not.toBeInTheDocument();
  });

  it("never generates on mount — it only reads", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());

    renderPanel();

    await screen.findAllByRole("heading", { level: 3 });
    expect(getPaperIntelligence).toHaveBeenCalledTimes(1);
    expect(getPaperIntelligence).toHaveBeenCalledWith(PAPER_ID);
  });

  it("surfaces a backend error without inventing wording", async () => {
    (getPaperIntelligence as any).mockRejectedValue(new Error("Paper not found."));

    renderPanel();

    expect(await screen.findByText("Paper not found.")).toBeInTheDocument();
  });

  it("does not render raw JSON", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());

    const { container } = renderPanel();

    await screen.findAllByRole("heading", { level: 3 });
    expect(container.textContent).not.toContain("chunk_id");
    expect(container.textContent).not.toContain('"status"');
    expect(container.textContent).not.toContain("schema_version");
  });

  it("does not fetch until a user is authenticated", () => {
    auth.userId = undefined;
    (getPaperIntelligence as any).mockResolvedValue(result());

    renderPanel();

    expect(getPaperIntelligence).not.toHaveBeenCalled();
  });
});

describe("Paper Intelligence cache isolation", () => {
  it("keys the query by both user and paper", () => {
    expect(queryKeys.paperIntelligence("user-1", PAPER_ID)).toEqual([
      "paper-intelligence",
      "user-1",
      PAPER_ID,
    ]);
  });

  it("gives two users different keys for the same paper", () => {
    expect(queryKeys.paperIntelligence("user-1", PAPER_ID)).not.toEqual(
      queryKeys.paperIntelligence("user-2", PAPER_ID),
    );
  });

  it("gives two papers different keys for the same user", () => {
    expect(queryKeys.paperIntelligence("user-1", "paper-a")).not.toEqual(
      queryKeys.paperIntelligence("user-1", "paper-b"),
    );
  });

  it("caches one user's analysis under that user's key only", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());

    const { queryClient } = renderPanel();
    await screen.findAllByRole("heading", { level: 3 });

    expect(queryClient.getQueryData(queryKeys.paperIntelligence("user-1", PAPER_ID))).toBeTruthy();
    // The next account reads a different key, so a late response from the
    // previous one can never be rendered to them.
    expect(
      queryClient.getQueryData(queryKeys.paperIntelligence("user-2", PAPER_ID)),
    ).toBeUndefined();
  });

  it("refetches for a different user rather than reusing the cache", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());

    const { unmount } = renderPanel();
    await screen.findAllByRole("heading", { level: 3 });
    unmount();

    auth.userId = "user-2";
    renderPanel();

    await waitFor(() => expect(getPaperIntelligence).toHaveBeenCalledTimes(2));
  });
});
