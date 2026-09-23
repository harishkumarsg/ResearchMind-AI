/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Comparison matrix — two papers' PERSISTED intelligence, side by side.
 *
 * The property that matters most is attribution: Paper A's content must
 * stay under Paper A and Paper B's under Paper B, through selection,
 * through swapping, and through one paper having no analysis at all. A
 * matrix that mixes two papers' findings is worse than no matrix, because
 * it looks authoritative while being wrong.
 *
 * The second is restraint: this component generates nothing. It calls no
 * model, ranks nothing, and invents no content for a paper that has no
 * stored analysis.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const auth = vi.hoisted(() => ({ userId: "user-1" as string | undefined }));
const links = vi.hoisted(() => [] as any[]);

/**
 * A minimal but REACTIVE stand-in for the router's search params.
 *
 * It has to notify subscribers, not just hold a value: the component
 * reads its selection from the URL now, so a change that did not trigger
 * a re-render would make every interaction test silently assert against
 * a stale screen.
 */
const url = vi.hoisted(() => {
  let value: Record<string, unknown> = {};
  const listeners = new Set<() => void>();
  return {
    get: () => value,
    set: (next: Record<string, unknown>) => {
      value = next;
      listeners.forEach((l) => l());
    },
    subscribe: (l: () => void) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
});

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    user: auth.userId ? { id: auth.userId } : null,
    session: auth.userId ? { access_token: "t" } : null,
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

vi.mock("@tanstack/react-router", async () => {
  const React = await vi.importActual<any>("react");
  return {
  useSearch: () => React.useSyncExternalStore(url.subscribe, url.get, url.get),
  useNavigate: () => (opts: any) => {
    const next =
      typeof opts.search === "function" ? opts.search(url.get()) : opts.search;
    url.set(next ?? {});
  },
  Link: ({ children, to, params, search, ...rest }: any) => {
    links.push({ to, params, search });
    return (
      <a
        href={`${to}?page=${search?.page}`}
        data-paper={params?.paperName}
        data-page={search?.page}
        {...rest}
      >
        {children}
      </a>
    );
  },
  };
});

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<any>("@/lib/api");
  return {
    ...actual,
    getPaperIntelligence: vi.fn(),
    getPapersDetailed: vi.fn(),
  };
});

import { PaperComparisonMatrix } from "@/components/paper-comparison-matrix";
import {
  getPaperIntelligence,
  getPapersDetailed,
  INTELLIGENCE_SECTION_ORDER,
} from "@/lib/api";
import { queryKeys } from "@/lib/query-keys";

const PAPER_A = "aaaaaaaa-0000-4000-8000-000000000001";
const PAPER_B = "bbbbbbbb-0000-4000-8000-000000000002";

const PAPERS = [
  { paper_id: PAPER_A, title: "Paper Alpha.pdf", status: "indexed", status_detail: null },
  { paper_id: PAPER_B, title: "Paper Beta.pdf", status: "indexed", status_detail: null },
  { paper_id: "ccc", title: "Still Indexing.pdf", status: "indexing", status_detail: null },
];

function section(overrides: any = {}) {
  return {
    status: "answered",
    summary: "A summary.",
    evidence: [{ page: 3, chunk_id: 0, quote: null }],
    ...overrides,
  };
}

function intelligenceFor(marker: string, overrides: any = {}) {
  const intelligence: any = {};
  for (const name of INTELLIGENCE_SECTION_ORDER) {
    intelligence[name] = section({ summary: `${marker} ${name}` });
  }
  Object.assign(intelligence, overrides);
  return {
    paper_id: "x",
    paper: marker,
    intelligence,
    generated_at: "2026-09-22T11:30:00+00:00",
    model: "openai/gpt-oss-120b",
    schema_version: "1",
    superseded: false,
  };
}

function renderMatrix() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const utils = render(
    (
      <QueryClientProvider client={queryClient}>
        <PaperComparisonMatrix />
      </QueryClientProvider>
    ) as ReactNode,
  );
  return { ...utils, queryClient };
}

async function selectBoth() {
  const selectA = await screen.findByLabelText("Paper A");
  const selectB = screen.getByLabelText("Paper B");
  fireEvent.change(selectA, { target: { value: PAPER_A } });
  fireEvent.change(selectB, { target: { value: PAPER_B } });
}

/** The row for one section, as a DOM element. */
function sectionRow(label: string) {
  const heading = screen.getByText(label);
  return heading.closest("div")!.parentElement!;
}

beforeEach(() => {
  vi.clearAllMocks();
  links.length = 0;
  url.set({});
  auth.userId = "user-1";
  (getPapersDetailed as any).mockResolvedValue(PAPERS);
  (getPaperIntelligence as any).mockImplementation(async (id: string) => {
    if (id === PAPER_A) return intelligenceFor("ALPHA");
    if (id === PAPER_B) return intelligenceFor("BETA");
    return null;
  });
});

describe("Paper selection", () => {
  it("offers only the user's indexed papers", async () => {
    renderMatrix();

    const selectA = await screen.findByLabelText("Paper A");
    const options = within(selectA).getAllByRole("option").map((o) => o.textContent);

    expect(options).toContain("Paper Alpha.pdf");
    expect(options).toContain("Paper Beta.pdf");
    // Not yet indexed, so it cannot carry an analysis.
    expect(options).not.toContain("Still Indexing.pdf");
  });

  it("lets two papers be selected", async () => {
    renderMatrix();
    await selectBoth();

    // The title appears as a select <option> AND as a column header, so
    // both must be present rather than unique.
    expect(await screen.findAllByText("Paper Alpha.pdf")).toHaveLength(3);
    expect(screen.getAllByText("Paper Beta.pdf")).toHaveLength(3);
  });

  it("refuses the same paper twice", async () => {
    renderMatrix();

    const selectA = await screen.findByLabelText("Paper A");
    const selectB = screen.getByLabelText("Paper B");
    fireEvent.change(selectA, { target: { value: PAPER_A } });
    fireEvent.change(selectB, { target: { value: PAPER_A } });

    expect(await screen.findByText(/choose two different papers/i)).toBeInTheDocument();
    // No matrix is rendered for a paper against itself...
    expect(screen.queryByText("ALPHA methodology")).toBeNull();
    // ...and only the one distinct paper was ever fetched.
    const fetched = new Set((getPaperIntelligence as any).mock.calls.map((c: any[]) => c[0]));
    expect([...fetched]).toEqual([PAPER_A]);
  });

  it("prompts before two papers are chosen", async () => {
    renderMatrix();
    await screen.findByLabelText("Paper A");

    expect(screen.getByText(/select two papers/i)).toBeInTheDocument();
    expect(getPaperIntelligence).not.toHaveBeenCalled();
  });

  it("explains when the account has fewer than two indexed papers", async () => {
    (getPapersDetailed as any).mockResolvedValue([PAPERS[0]]);

    renderMatrix();

    expect(await screen.findByText(/two indexed papers are needed/i)).toBeInTheDocument();
  });
});

describe("Matrix content and attribution", () => {
  it("renders all ten sections", async () => {
    renderMatrix();
    await selectBoth();

    await screen.findByText("ALPHA research_problem");
    for (const label of [
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
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("keeps Paper A content under Paper A and Paper B content under Paper B", async () => {
    renderMatrix();
    await selectBoth();

    await screen.findByText("ALPHA methodology");

    const row = sectionRow("Methodology");
    const cells = within(row).getAllByText(/^(ALPHA|BETA) methodology$/);
    expect(cells).toHaveLength(2);
    // Column order is A then B, and the content follows it.
    expect(cells[0].textContent).toBe("ALPHA methodology");
    expect(cells[1].textContent).toBe("BETA methodology");
  });

  it("labels each column with its own paper", async () => {
    renderMatrix();
    await selectBoth();

    await screen.findByText("ALPHA research_problem");
    // "Paper A" appears as the selector label, the column header, and a
    // per-cell label for the stacked mobile layout. What matters is that
    // the HEADER pairs each column label with the right paper title.
    // Restricted to DIVs: the selector's <label> shares a parent with
    // the <select>, whose options contain BOTH titles, so it would match
    // either paper and prove nothing.
    const headerFor = (label: string, title: string) =>
      screen.getAllByText(label).find(
        (el) => el.tagName === "DIV" && el.parentElement?.textContent?.includes(title),
      );
    expect(headerFor("Paper A", "Paper Alpha.pdf")).toBeTruthy();
    expect(headerFor("Paper B", "Paper Beta.pdf")).toBeTruthy();
    // ...and never with the other paper's title.
    expect(headerFor("Paper A", "Paper Beta.pdf")).toBeUndefined();
    expect(headerFor("Paper B", "Paper Alpha.pdf")).toBeUndefined();
  });

  it("swapping A and B swaps the content without mixing it", async () => {
    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA key_results");

    // Swap the selections.
    fireEvent.change(screen.getByLabelText("Paper A"), { target: { value: PAPER_B } });
    fireEvent.change(screen.getByLabelText("Paper B"), { target: { value: PAPER_A } });

    await waitFor(() => {
      const row = sectionRow("Key Results");
      const cells = within(row).getAllByText(/^(ALPHA|BETA) key_results$/);
      expect(cells[0].textContent).toBe("BETA key_results");
      expect(cells[1].textContent).toBe("ALPHA key_results");
    });
  });

  it("swapping costs no extra request", async () => {
    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA key_results");

    const before = (getPaperIntelligence as any).mock.calls.length;

    fireEvent.change(screen.getByLabelText("Paper A"), { target: { value: PAPER_B } });
    fireEvent.change(screen.getByLabelText("Paper B"), { target: { value: PAPER_A } });

    await waitFor(() => expect(screen.getByText("BETA key_results")).toBeInTheDocument());
    // Per-paper cache keys: each paper was already fetched.
    expect((getPaperIntelligence as any).mock.calls.length).toBe(before);
  });

  it("shows a not_specified section as unavailable for that paper only", async () => {
    (getPaperIntelligence as any).mockImplementation(async (id: string) => {
      if (id === PAPER_A) {
        return intelligenceFor("ALPHA", {
          reproducibility: { status: "not_specified", summary: null, evidence: [] },
        });
      }
      return intelligenceFor("BETA");
    });

    renderMatrix();
    await selectBoth();

    expect(await screen.findByText("Not specified in Paper A.")).toBeInTheDocument();
    expect(screen.getByText("BETA reproducibility")).toBeInTheDocument();
  });

  it("labels summaries as AI interpretation", async () => {
    renderMatrix();
    await selectBoth();

    const labels = await screen.findAllByText("AI interpretation");
    expect(labels.length).toBe(20); // ten sections, two papers
  });

  it("renders no raw JSON", async () => {
    const { container } = renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA research_problem");

    expect(container.textContent).not.toContain("chunk_id");
    expect(container.textContent).not.toContain("schema_version");
    expect(container.textContent).not.toContain("{");
  });

  it("ranks nothing and declares no winner", async () => {
    const { container } = renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA research_problem");

    const text = container.textContent ?? "";
    for (const banned of ["better", "superior", "wins", "winner", "more reliable", "score"]) {
      expect(text.toLowerCase()).not.toContain(banned);
    }
  });
});

describe("Missing intelligence", () => {
  it('shows "not available" for the paper without an analysis', async () => {
    (getPaperIntelligence as any).mockImplementation(async (id: string) =>
      id === PAPER_A ? intelligenceFor("ALPHA") : null,
    );

    renderMatrix();
    await selectBoth();

    expect(
      await screen.findAllByText(/paper intelligence is not available for this paper/i),
    ).toHaveLength(10);
    // The paper that DOES have an analysis still renders normally.
    expect(screen.getByText("ALPHA methodology")).toBeInTheDocument();
  });

  it("invents no content for the missing paper", async () => {
    (getPaperIntelligence as any).mockImplementation(async (id: string) =>
      id === PAPER_A ? intelligenceFor("ALPHA") : null,
    );

    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA methodology");

    expect(screen.queryByText(/^BETA/)).toBeNull();
  });

  it("surfaces a backend error for one column without breaking the other", async () => {
    (getPaperIntelligence as any).mockImplementation(async (id: string) => {
      if (id === PAPER_A) return intelligenceFor("ALPHA");
      throw new Error("Paper not found.");
    });

    renderMatrix();
    await selectBoth();

    expect(await screen.findAllByText("Paper not found.")).toHaveLength(10);
    expect(screen.getByText("ALPHA methodology")).toBeInTheDocument();
  });
});

describe("Evidence attribution", () => {
  it("links a source page to its own paper", async () => {
    (getPaperIntelligence as any).mockImplementation(async (id: string) => {
      if (id === PAPER_A) {
        return intelligenceFor("ALPHA", {
          methodology: section({ summary: "ALPHA methodology", evidence: [{ page: 2, chunk_id: 0, quote: null }] }),
        });
      }
      return intelligenceFor("BETA", {
        methodology: section({ summary: "BETA methodology", evidence: [{ page: 7, chunk_id: 0, quote: null }] }),
      });
    });

    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA methodology");

    const a = screen.getByRole("link", { name: "Open Paper A at page 2" });
    expect(a).toHaveAttribute("data-paper", "Paper Alpha.pdf");
    expect(a).toHaveAttribute("data-page", "2");

    const b = screen.getByRole("link", { name: "Open Paper B at page 7" });
    expect(b).toHaveAttribute("data-paper", "Paper Beta.pdf");
    expect(b).toHaveAttribute("data-page", "7");
  });

  it("never points one paper's evidence at the other paper", async () => {
    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA research_problem");

    for (const link of links) {
      const expected = link.to === "/paper/$paperName" ? link.params.paperName : "";
      expect(["Paper Alpha.pdf", "Paper Beta.pdf"]).toContain(expected);
    }
    // Every link carries a real page.
    expect(links.every((l) => typeof l.search.page === "number" && l.search.page >= 1)).toBe(true);
  });

  it("shows multiple source pages for one section", async () => {
    (getPaperIntelligence as any).mockImplementation(async (id: string) =>
      id === PAPER_A
        ? intelligenceFor("ALPHA", {
            dataset: section({
              summary: "ALPHA dataset",
              evidence: [
                { page: 2, chunk_id: 0, quote: null },
                { page: 5, chunk_id: 1, quote: null },
              ],
            }),
          })
        : intelligenceFor("BETA"),
    );

    renderMatrix();
    await selectBoth();

    expect(await screen.findByRole("link", { name: "Open Paper A at page 2" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Paper A at page 5" })).toBeInTheDocument();
  });
});

describe("Tenancy and safety", () => {
  it("scopes each paper's query to the authenticated user", async () => {
    const { queryClient } = renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA research_problem");

    expect(queryClient.getQueryData(queryKeys.paperIntelligence("user-1", PAPER_A))).toBeTruthy();
    // Another account reads a different key entirely.
    expect(queryClient.getQueryData(queryKeys.paperIntelligence("user-2", PAPER_A))).toBeUndefined();
  });

  it("gives two users different keys for the same paper", () => {
    expect(queryKeys.paperIntelligence("user-1", PAPER_A)).not.toEqual(
      queryKeys.paperIntelligence("user-2", PAPER_A),
    );
  });

  it("fetches nothing while signed out", async () => {
    auth.userId = undefined;

    renderMatrix();

    await waitFor(() => expect(getPapersDetailed).not.toHaveBeenCalled());
    expect(getPaperIntelligence).not.toHaveBeenCalled();
  });

  it("sends no owner id — the server derives it from the JWT", async () => {
    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA research_problem");

    for (const call of (getPaperIntelligence as any).mock.calls) {
      expect(call).toHaveLength(1); // paperId only
      expect(typeof call[0]).toBe("string");
    }
  });

  it("makes exactly two intelligence requests and never generates", async () => {
    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA research_problem");

    expect((getPaperIntelligence as any).mock.calls.length).toBe(2);
    expect((getPaperIntelligence as any).mock.calls.map((c: any[]) => c[0]).sort()).toEqual(
      [PAPER_A, PAPER_B].sort(),
    );
  });
});

describe("Selection persists across refresh", () => {
  it("records Paper A in the URL when selected", async () => {
    renderMatrix();

    fireEvent.change(await screen.findByLabelText("Paper A"), {
      target: { value: PAPER_A },
    });

    expect(url.get()).toEqual({ a: PAPER_A });
  });

  it("records Paper B in the URL when selected", async () => {
    renderMatrix();
    await selectBoth();

    expect(url.get()).toEqual({ a: PAPER_A, b: PAPER_B });
  });

  it("restores both selections when the page loads with them", async () => {
    // What a refresh actually looks like: fresh mount, URL already set.
    url.set({ a: PAPER_A, b: PAPER_B });

    renderMatrix();

    expect(await screen.findByText("ALPHA research_problem")).toBeInTheDocument();
    expect(screen.getByText("BETA research_problem")).toBeInTheDocument();
    expect((await screen.findByLabelText("Paper A")) as HTMLSelectElement).toHaveValue(
      PAPER_A,
    );
    expect(screen.getByLabelText("Paper B") as HTMLSelectElement).toHaveValue(PAPER_B);
  });

  it("survives an unmount and remount with the URL unchanged", async () => {
    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA research_problem");

    const persisted = url.get();
    cleanup();

    renderMatrix();

    expect(url.get()).toEqual(persisted);
    expect(await screen.findByText("ALPHA research_problem")).toBeInTheDocument();
    expect(screen.getByText("BETA research_problem")).toBeInTheDocument();
  });

  it("updates the URL when A and B are swapped", async () => {
    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA key_results");

    fireEvent.change(screen.getByLabelText("Paper A"), { target: { value: PAPER_B } });
    fireEvent.change(screen.getByLabelText("Paper B"), { target: { value: PAPER_A } });

    expect(url.get()).toEqual({ a: PAPER_B, b: PAPER_A });
  });

  it("removes a parameter when its selection is cleared", async () => {
    renderMatrix();
    await selectBoth();

    fireEvent.change(screen.getByLabelText("Paper B"), { target: { value: "" } });

    // Removed, not left behind as an empty value.
    expect(url.get()).toEqual({ a: PAPER_A });
    expect("b" in url.get()).toBe(false);
  });

  it("leaves unrelated search parameters alone", async () => {
    url.set({ tab: "notes" });

    renderMatrix();
    fireEvent.change(await screen.findByLabelText("Paper A"), {
      target: { value: PAPER_A },
    });

    expect(url.get()).toEqual({ tab: "notes", a: PAPER_A });
  });

  it("renders the comparison exactly as before when restored from the URL", async () => {
    url.set({ a: PAPER_A, b: PAPER_B });

    renderMatrix();
    await screen.findByText("ALPHA methodology");

    const row = sectionRow("Methodology");
    const cells = within(row).getAllByText(/^(ALPHA|BETA) methodology$/);
    expect(cells[0].textContent).toBe("ALPHA methodology");
    expect(cells[1].textContent).toBe("BETA methodology");
    expect(screen.getAllByText("AI interpretation")).toHaveLength(20);
  });
});

describe("URL parameters are not authorization", () => {
  it("ignores a paper id the account does not own", async () => {
    const FOREIGN = "ffffffff-0000-4000-8000-00000000000f";
    url.set({ a: FOREIGN, b: PAPER_B });

    renderMatrix();
    await screen.findByLabelText("Paper A");

    // Not in this account's paper list, so it is not a selection at all.
    expect((screen.getByLabelText("Paper A") as HTMLSelectElement).value).toBe("");
    expect(screen.getByText(/select two papers/i)).toBeInTheDocument();
    // And it is never even requested.
    const asked = (getPaperIntelligence as any).mock.calls.map((c: any[]) => c[0]);
    expect(asked).not.toContain(FOREIGN);
  });

  it("ignores an id for a paper that is not indexed", async () => {
    url.set({ a: "ccc", b: PAPER_B });

    renderMatrix();
    await screen.findByLabelText("Paper A");

    expect((screen.getByLabelText("Paper A") as HTMLSelectElement).value).toBe("");
    const asked = (getPaperIntelligence as any).mock.calls.map((c: any[]) => c[0]);
    expect(asked).not.toContain("ccc");
  });

  it("fetches nothing from the URL while signed out", async () => {
    auth.userId = undefined;
    url.set({ a: PAPER_A, b: PAPER_B });

    renderMatrix();

    await waitFor(() => expect(getPapersDetailed).not.toHaveBeenCalled());
    expect(getPaperIntelligence).not.toHaveBeenCalled();
  });

  it("puts no intelligence content, owner id or token in the URL", async () => {
    renderMatrix();
    await selectBoth();
    await screen.findByText("ALPHA research_problem");

    const encoded = JSON.stringify(url.get());
    expect(encoded).not.toContain("ALPHA");
    expect(encoded).not.toContain("user-1");
    expect(encoded).not.toContain("token");
    expect(encoded).not.toContain("owner");
    // Ids only.
    expect(Object.values(url.get()).sort()).toEqual([PAPER_A, PAPER_B].sort());
  });

  it("still makes exactly two reads and never generates", async () => {
    url.set({ a: PAPER_A, b: PAPER_B });

    renderMatrix();
    await screen.findByText("ALPHA research_problem");

    expect((getPaperIntelligence as any).mock.calls.length).toBe(2);
    // The read API is the only one this component reaches for.
    for (const call of (getPaperIntelligence as any).mock.calls) {
      expect(call).toHaveLength(1);
    }
  });
});
