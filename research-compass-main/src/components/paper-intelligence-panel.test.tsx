/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Paper Intelligence panel — the reader, and the explicit generate action.
 *
 * The properties worth pinning are the ones a careless change would
 * break silently:
 *
 *   * it never generates ON ITS OWN. Opening a paper must not trigger a
 *     billable generation: the panel reads on load, and generates only
 *     when the user clicks, exactly once per click, never retried;
 *   * the query key carries BOTH the user and the paper, which is what
 *     stops one account's analysis being served to the next from cache;
 *   * evidence is shown by PAGE and clicking one drives the existing
 *     viewer, rather than dumping chunk ids or raw JSON at the reader.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
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
    generatePaperIntelligence: vi.fn(),
  };
});

import { PaperIntelligencePanel } from "@/components/paper-intelligence-panel";
import {
  generatePaperIntelligence,
  getPaperIntelligence,
  INTELLIGENCE_SECTION_ORDER,
  PaperIntelligenceNotFoundError,
  RateLimitError,
} from "@/lib/api";
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

  it("offers a generate action in the empty state", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);

    renderPanel();

    await screen.findByText(/no analysis yet/i);
    expect(
      screen.getByRole("button", { name: "Generate intelligence" }),
    ).toBeInTheDocument();
    // Offered, not started.
    expect(generatePaperIntelligence).not.toHaveBeenCalled();
  });

  it("never generates on mount — it only reads", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());

    renderPanel();

    await screen.findAllByRole("heading", { level: 3 });
    expect(getPaperIntelligence).toHaveBeenCalledTimes(1);
    expect(getPaperIntelligence).toHaveBeenCalledWith(PAPER_ID);
    expect(generatePaperIntelligence).not.toHaveBeenCalled();
  });

  it("never generates on mount for an unanalysed paper either", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);

    renderPanel();

    await screen.findByText(/no analysis yet/i);
    // Give any stray effect a chance to fire before asserting.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(generatePaperIntelligence).not.toHaveBeenCalled();
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

describe("Generate / Regenerate Paper Intelligence", () => {
  const RELATIONSHIP_KEY = queryKeys.paperRelationship("user-1", PAPER_ID, "other-paper");

  /** A result whose methodology summary marks it as the NEW analysis. */
  function fresh(overrides: any = {}) {
    const data = result(overrides);
    data.intelligence.methodology = section({ summary: "Freshly generated methodology." });
    return data;
  }

  /** A POST that stays pending until the test releases it. */
  function pendingGeneration() {
    let release: (value: any) => void = () => {};
    let fail: (error: Error) => void = () => {};
    (generatePaperIntelligence as any).mockImplementation(
      () =>
        new Promise((resolve, reject) => {
          release = resolve;
          fail = reject;
        }),
    );
    return { release: (value: any) => release(value), fail: (error: Error) => fail(error) };
  }

  it("calls the POST exactly once, for this paper, when Generate is clicked", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);
    (generatePaperIntelligence as any).mockResolvedValue(fresh());

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));

    await screen.findByText("Freshly generated methodology.");
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
    expect(generatePaperIntelligence).toHaveBeenCalledWith(PAPER_ID);
  });

  it("fires exactly one POST for clicks dispatched in the SAME React batch", async () => {
    // Batched inside one act(), every click runs before any re-render, so
    // `disabled` is not applied yet and only the ref latch can stop the
    // second POST — each of which would spend a unit.
    (getPaperIntelligence as any).mockResolvedValue(null);
    const generation = pendingGeneration();

    renderPanel();
    const button = await screen.findByRole("button", { name: "Generate intelligence" });

    act(() => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);

    generation.release(fresh());
    await screen.findByText("Freshly generated methodology.");
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
  });

  it("disables the action, marks it busy and announces progress while running", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);
    const generation = pendingGeneration();

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));

    const busy = await screen.findByRole("button", { name: /analysing/i });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status")).toHaveTextContent(/up to about a minute/i);

    generation.release(fresh());
    await screen.findByText("Freshly generated methodology.");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders the POST result directly, with no follow-up GET", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);
    (generatePaperIntelligence as any).mockResolvedValue(fresh());

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));

    await screen.findByText("Freshly generated methodology.");
    expect(getPaperIntelligence).toHaveBeenCalledTimes(1);
  });

  it("writes the result to the shared intelligence key the comparison matrix reads", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);
    const generated = fresh();
    (generatePaperIntelligence as any).mockResolvedValue(generated);

    const { queryClient } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));

    await screen.findByText("Freshly generated methodology.");
    expect(queryClient.getQueryData(queryKeys.paperIntelligence("user-1", PAPER_ID))).toEqual(
      generated,
    );
  });

  it("invalidates this user's cached relationships, and only this user's", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);
    (generatePaperIntelligence as any).mockResolvedValue(fresh());

    const { queryClient } = renderPanel();
    const otherUsersKey = queryKeys.paperRelationship("user-2", PAPER_ID, "other-paper");
    queryClient.setQueryData(RELATIONSHIP_KEY, { marker: "cached" });
    queryClient.setQueryData(otherUsersKey, { marker: "cached" });

    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));
    await screen.findByText("Freshly generated methodology.");

    expect(queryClient.getQueryState(RELATIONSHIP_KEY)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(otherUsersKey)?.isInvalidated).toBe(false);
  });

  it("offers Regenerate, not Generate, when an analysis already exists", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());

    renderPanel();

    expect(await screen.findByRole("button", { name: "Regenerate" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Generate intelligence" }),
    ).not.toBeInTheDocument();
    expect(generatePaperIntelligence).not.toHaveBeenCalled();
  });

  it("keeps the existing analysis visible while a regeneration runs", async () => {
    const existing = result();
    existing.intelligence.methodology = section({ summary: "The existing methodology." });
    (getPaperIntelligence as any).mockResolvedValue(existing);
    const generation = pendingGeneration();

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));

    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.getByText("The existing methodology.")).toBeInTheDocument();

    generation.release(fresh());
    await screen.findByText("Freshly generated methodology.");
    expect(screen.queryByText("The existing methodology.")).not.toBeInTheDocument();
  });

  it("shows the backend's message on failure and says nothing was changed", async () => {
    const existing = result();
    existing.intelligence.methodology = section({ summary: "The existing methodology." });
    (getPaperIntelligence as any).mockResolvedValue(existing);
    (generatePaperIntelligence as any).mockRejectedValue(
      new Error("The paper analysis could not be completed. Please try again."),
    );

    const { container } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));

    expect(
      await screen.findByText("The paper analysis could not be completed. Please try again."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Your existing analysis, if any, was not changed."),
    ).toBeInTheDocument();
    expect(screen.getByText("The existing methodology.")).toBeInTheDocument();
    expect(container.textContent).not.toContain('"status"');
    // Re-enabled for a deliberate second attempt; never retried on its own.
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeEnabled();
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
  });

  it("re-reads the stored analysis exactly once after a failure", async () => {
    // A client-side failure can hide a server-side success; the GET is free.
    (getPaperIntelligence as any).mockResolvedValue(null);
    (generatePaperIntelligence as any).mockRejectedValue(new Error("Request failed."));

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));

    await screen.findByText("Request failed.");
    await waitFor(() => expect(getPaperIntelligence).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(getPaperIntelligence).toHaveBeenCalledTimes(2);
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
  });

  it("shows a server-side success that a failed request had hidden", async () => {
    (getPaperIntelligence as any)
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce(fresh());
    (generatePaperIntelligence as any).mockRejectedValue(
      new Error("The paper analysis could not be completed."),
    );

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));

    expect(await screen.findByText("Freshly generated methodology.")).toBeInTheDocument();
  });

  it("shows the authored daily-quota message unchanged", async () => {
    const quotaMessage =
      "You've reached today's limit for AI answers (50). It resets at 00:00 UTC.";
    (getPaperIntelligence as any).mockResolvedValue(null);
    (generatePaperIntelligence as any).mockRejectedValue(new Error(quotaMessage));

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));

    expect(await screen.findByText(quotaMessage)).toBeInTheDocument();
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
  });

  it("shows the existing rate-limit wording, not a provider's", async () => {
    (getPaperIntelligence as any).mockResolvedValue(null);
    (generatePaperIntelligence as any).mockRejectedValue(new RateLimitError());

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));

    expect(await screen.findByText(/busy right now/i)).toBeInTheDocument();
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
  });

  it("offers no generate action for a paper that is not found", async () => {
    (getPaperIntelligence as any).mockRejectedValue(new PaperIntelligenceNotFoundError());

    renderPanel();

    expect(await screen.findByText("Paper not found.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /generate/i })).not.toBeInTheDocument();
  });

  it("offers Generate when the stored analysis could not be read", async () => {
    const unreadable =
      "The stored analysis for this paper could not be read. " +
      "Generating it again will replace it.";
    (getPaperIntelligence as any).mockRejectedValue(new Error(unreadable));
    (generatePaperIntelligence as any).mockResolvedValue(fresh());

    renderPanel();

    expect(await screen.findByText(unreadable)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Generate intelligence" }));

    expect(await screen.findByText("Freshly generated methodology.")).toBeInTheDocument();
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
  });

  it("renders the returned current object and a quiet note when superseded", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());
    (generatePaperIntelligence as any).mockResolvedValue(fresh({ superseded: true }));

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));

    expect(await screen.findByText("Freshly generated methodology.")).toBeInTheDocument();
    expect(
      screen.getByText("A newer analysis finished first and is shown."),
    ).toBeInTheDocument();
  });

  it("shows no superseded note for an ordinary result", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());
    (generatePaperIntelligence as any).mockResolvedValue(fresh());

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));

    await screen.findByText("Freshly generated methodology.");
    expect(screen.queryByText(/a newer analysis finished first/i)).not.toBeInTheDocument();
  });

  it("shows when the analysis was generated", async () => {
    (getPaperIntelligence as any).mockResolvedValue(result());

    renderPanel();

    expect(await screen.findByText(/^Generated /)).toBeInTheDocument();
  });

  it("offers nothing and generates nothing without an authenticated user", async () => {
    auth.userId = undefined;
    (getPaperIntelligence as any).mockResolvedValue(null);

    renderPanel();

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("button", { name: /generate|regenerate/i })).not.toBeInTheDocument();
    expect(getPaperIntelligence).not.toHaveBeenCalled();
    expect(generatePaperIntelligence).not.toHaveBeenCalled();
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
