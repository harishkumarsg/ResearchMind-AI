/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Research Relationship panel.
 *
 * The panel states a fact about stored data — which sections each paper
 * grounds — so the tests that matter are the ones stopping it from
 * saying more than that:
 *
 *   * "Supported by both" must never be presented as agreement. The
 *     panel says so in words, and a test pins the wording, because the
 *     whole phase exists to avoid inventing relationships;
 *   * a_only and b_only must stay distinct, and a missing side must
 *     never borrow the other's claim;
 *   * a verdict must never appear before the data does — an
 *     "Insufficient evidence" flash during loading is a false statement
 *     about the papers, not a cosmetic glitch.
 *
 * Presentational and offline: the panel takes already-loaded data as
 * props, so no query, no provider and no network is involved at all.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const links = vi.hoisted(() => [] as any[]);
const auth = vi.hoisted(() => ({ userId: "user-1" as string | undefined }));
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

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<any>("@/lib/api");
  return { ...actual, getPaperIntelligence: vi.fn(), getPapersDetailed: vi.fn() };
});

vi.mock("@tanstack/react-router", async () => {
  const React = await vi.importActual<any>("react");
  return {
  useSearch: () => React.useSyncExternalStore(url.subscribe, url.get, url.get),
  Link: ({ children, to, params, search, ...rest }: any) => {
    links.push({ to, params, search });
    return (
      <a href={`${to}?page=${search?.page}`} data-paper={params?.paperName} {...rest}>
        {children}
      </a>
    );
  },
  };
});

import {
  PaperRelationshipPanel,
  PaperRelationshipSection,
  type RelationshipColumn,
} from "@/components/paper-relationship-panel";
import { queryKeys } from "@/lib/query-keys";
import {
  getPaperIntelligence,
  getPapersDetailed,
  INTELLIGENCE_SECTION_ORDER,
  type IntelligenceSection,
  type PaperIntelligenceSections,
} from "@/lib/api";

function answered(marker: string, page = 3): IntelligenceSection {
  return {
    status: "answered",
    summary: `${marker} summary`,
    evidence: [{ page, chunk_id: 0, quote: null }],
  };
}

function notSpecified(): IntelligenceSection {
  return { status: "not_specified", summary: null, evidence: [] };
}

function paper(marker: string): PaperIntelligenceSections {
  const sections = {} as PaperIntelligenceSections;
  for (const name of INTELLIGENCE_SECTION_ORDER) {
    sections[name] = answered(`${marker} ${name}`);
  }
  return sections;
}

function column(
  label: string,
  title: string,
  overrides: Partial<RelationshipColumn> = {},
): RelationshipColumn {
  return {
    label,
    title,
    intelligence: null,
    isLoading: false,
    error: null,
    ...overrides,
  };
}

function renderPanel(a: RelationshipColumn, b: RelationshipColumn) {
  return render((<PaperRelationshipPanel a={a} b={b} />) as ReactNode);
}

/** The row element for one section label. */
function row(label: string) {
  // h3 -> its flex header div -> the row div. One more level up would
  // return the whole list, which silently widens every within() query.
  return screen.getByText(label).closest("div")!.parentElement!;
}

const A = (o: Partial<RelationshipColumn> = {}) => column("Paper A", "Alpha.pdf", o);
const B = (o: Partial<RelationshipColumn> = {}) => column("Paper B", "Beta.pdf", o);

beforeEach(() => {
  vi.clearAllMocks();
  links.length = 0;
  url.set({});
  auth.userId = "user-1";
});

describe("Structure and ordering", () => {
  it("renders the ten sections in canonical order", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: paper("BETA") }));

    const headings = screen.getAllByRole("heading", { level: 3 });
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

  it("does not introduce a second ordering", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: paper("BETA") }));

    const headings = screen.getAllByRole("heading", { level: 3 });
    expect(headings).toHaveLength(INTELLIGENCE_SECTION_ORDER.length);
  });
});

describe("Verdict labels", () => {
  it("comparable renders as Supported by both", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: paper("BETA") }));

    expect(screen.getAllByText("Supported by both")).toHaveLength(10);
  });

  it("a_only renders as Paper A only", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: null }));

    expect(screen.getAllByText("Paper A only")).toHaveLength(10);
    expect(screen.queryByText("Paper B only")).toBeNull();
  });

  it("b_only renders as Paper B only", () => {
    renderPanel(A({ intelligence: null }), B({ intelligence: paper("BETA") }));

    expect(screen.getAllByText("Paper B only")).toHaveLength(10);
    expect(screen.queryByText("Paper A only")).toBeNull();
  });

  it("neither renders as Insufficient evidence", () => {
    renderPanel(A({ intelligence: null }), B({ intelligence: null }));

    expect(screen.getAllByText("Insufficient evidence")).toHaveLength(10);
  });

  it("keeps a_only and b_only distinct in a mixed comparison", () => {
    const a = paper("ALPHA");
    const b = paper("BETA");
    a.dataset = notSpecified(); // -> Paper B only
    b.limitations = notSpecified(); // -> Paper A only
    a.reproducibility = notSpecified();
    b.reproducibility = notSpecified(); // -> Insufficient evidence

    renderPanel(A({ intelligence: a }), B({ intelligence: b }));

    expect(within(row("Dataset")).getByText("Paper B only")).toBeInTheDocument();
    expect(within(row("Limitations")).getByText("Paper A only")).toBeInTheDocument();
    expect(
      within(row("Reproducibility")).getByText("Insufficient evidence"),
    ).toBeInTheDocument();
    expect(within(row("Methodology")).getByText("Supported by both")).toBeInTheDocument();
  });

  it("treats not_specified as ungrounded, not as a claim", () => {
    const a = paper("ALPHA");
    a.dataset = notSpecified();

    renderPanel(A({ intelligence: a }), B({ intelligence: paper("BETA") }));

    const dataset = row("Dataset");
    expect(within(dataset).getByText("Paper B only")).toBeInTheDocument();
    expect(within(dataset).queryByText(/ALPHA dataset/)).toBeNull();
  });
});

describe("Claims and attribution", () => {
  it("renders Paper A's claim only when grounded", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: null }));

    expect(screen.getByText("ALPHA methodology summary")).toBeInTheDocument();
    expect(screen.getAllByText(/no grounded claim for this section/)).toHaveLength(10);
  });

  it("renders Paper B's claim only when grounded", () => {
    renderPanel(A({ intelligence: null }), B({ intelligence: paper("BETA") }));

    expect(screen.getByText("BETA methodology summary")).toBeInTheDocument();
  });

  it("never puts one paper's claim on the other's side", () => {
    const a = paper("ALPHA");
    const b = paper("BETA");

    renderPanel(A({ intelligence: a }), B({ intelligence: b }));

    const methodology = row("Methodology");
    const alpha = within(methodology).getByText("ALPHA methodology summary");
    const beta = within(methodology).getByText("BETA methodology summary");

    // Each summary sits under its own column label.
    expect(alpha.parentElement?.textContent).toContain("Paper A");
    expect(alpha.parentElement?.textContent).not.toContain("BETA");
    expect(beta.parentElement?.textContent).toContain("Paper B");
    expect(beta.parentElement?.textContent).not.toContain("ALPHA");
  });

  it("invents no claim for a missing side", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: null }));

    expect(screen.queryByText(/BETA/)).toBeNull();
    // A's content is never replayed as B's.
    const methodology = row("Methodology");
    expect(within(methodology).getAllByText(/ALPHA methodology summary/)).toHaveLength(1);
  });

  it("labels each summary as AI interpretation", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: paper("BETA") }));

    expect(screen.getAllByText("AI interpretation")).toHaveLength(20);
  });

  it("labels the verdict as directly supported", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: paper("BETA") }));

    expect(screen.getAllByText("Directly supported")).toHaveLength(10);
  });

  it("links evidence pages to the owning paper", () => {
    const a = paper("ALPHA");
    const b = paper("BETA");
    a.methodology = answered("ALPHA methodology", 2);
    b.methodology = answered("BETA methodology", 7);

    renderPanel(A({ intelligence: a }), B({ intelligence: b }));

    expect(
      screen.getByRole("link", { name: "Open Paper A at page 2" }),
    ).toHaveAttribute("data-paper", "Alpha.pdf");
    expect(
      screen.getByRole("link", { name: "Open Paper B at page 7" }),
    ).toHaveAttribute("data-paper", "Beta.pdf");
  });
});

describe("Language stays neutral", () => {
  it("states that supported-by-both is not agreement", () => {
    const { container } = renderPanel(
      A({ intelligence: paper("ALPHA") }),
      B({ intelligence: paper("BETA") }),
    );

    expect(container.textContent).toContain("it does not mean the papers agree");
  });

  it("uses no ranking or quality language anywhere", () => {
    const a = paper("ALPHA");
    const b = paper("BETA");
    b.dataset = notSpecified();

    const { container } = renderPanel(A({ intelligence: a }), B({ intelligence: b }));

    const text = (container.textContent ?? "").toLowerCase();
    for (const banned of [
      "winner",
      "better",
      "stronger",
      "weaker",
      "score",
      "ranking",
      "recommendation",
      "superior",
      "wins",
    ]) {
      expect(text).not.toContain(banned);
    }
  });

  it("uses no semantic agreement language", () => {
    const { container } = renderPanel(
      A({ intelligence: paper("ALPHA") }),
      B({ intelligence: paper("BETA") }),
    );

    const text = (container.textContent ?? "").toLowerCase();
    for (const banned of ["contradict", "disagree", "consensus", "conflict"]) {
      expect(text).not.toContain(banned);
    }
  });

  it("renders no raw JSON", () => {
    const { container } = renderPanel(
      A({ intelligence: paper("ALPHA") }),
      B({ intelligence: paper("BETA") }),
    );

    expect(container.textContent).not.toContain("chunk_id");
    expect(container.textContent).not.toContain("{");
    expect(container.textContent).not.toContain("comparable");
  });
});

describe("Loading, missing and error states", () => {
  it("shows a calm loading state", () => {
    renderPanel(A({ isLoading: true }), B({ intelligence: paper("BETA") }));

    expect(screen.getByRole("status")).toHaveTextContent(/loading research relationship/i);
  });

  it("shows no verdict at all while loading", () => {
    renderPanel(A({ isLoading: true }), B({ intelligence: paper("BETA") }));

    // A verdict before the data arrives would be a false statement.
    for (const label of [
      "Insufficient evidence",
      "Supported by both",
      "Paper A only",
      "Paper B only",
    ]) {
      expect(screen.queryByText(label)).toBeNull();
    }
  });

  it("waits for the slower column before showing verdicts", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ isLoading: true }));

    expect(screen.queryByText("Paper A only")).toBeNull();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("explains when one paper has no structured analysis", () => {
    renderPanel(A({ intelligence: paper("ALPHA") }), B({ intelligence: null }));

    expect(screen.getByText(/Paper B \(Beta\.pdf\) has no structured analysis yet/)).
      toBeInTheDocument();
  });

  it("explains when neither paper has a structured analysis", () => {
    renderPanel(A({ intelligence: null }), B({ intelligence: null }));

    expect(screen.getByText(/neither paper has a structured analysis yet/i)).
      toBeInTheDocument();
  });

  it("offers no generate action in any empty state", () => {
    renderPanel(A({ intelligence: null }), B({ intelligence: null }));

    expect(screen.queryByRole("button", { name: /generate|analy/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /generate/i })).toBeNull();
  });

  it("shows a safe error without exposing internals", () => {
    renderPanel(
      A({ error: new Error("Paper not found.") }),
      B({ intelligence: paper("BETA") }),
    );

    expect(screen.getByText("Paper not found.")).toBeInTheDocument();
    // No verdicts alongside an error.
    expect(screen.queryByText("Supported by both")).toBeNull();
  });

  it("surfaces an error from either column", () => {
    renderPanel(
      A({ intelligence: paper("ALPHA") }),
      B({ error: new Error("Something went wrong on our side. Please try again.") }),
    );

    expect(
      screen.getByText("Something went wrong on our side. Please try again."),
    ).toBeInTheDocument();
  });
});

describe("Production shape — one paper analysed, one not", () => {
  it("renders today's production state correctly", () => {
    renderPanel(
      A({ intelligence: paper("ETASR"), title: "ETASR_18859.pdf" }),
      B({ intelligence: null, title: "2503.14162v2.pdf" }),
    );

    expect(screen.getAllByText("Paper A only")).toHaveLength(10);
    expect(screen.getByText(/has no structured analysis yet/)).toBeInTheDocument();
    expect(screen.getByText("ETASR methodology summary")).toBeInTheDocument();
    expect(screen.queryByText("Supported by both")).toBeNull();
  });
});

describe("Route wiring (PaperRelationshipSection)", () => {
  const PAPER_A = "aaaaaaaa-0000-4000-8000-000000000001";
  const PAPER_B = "bbbbbbbb-0000-4000-8000-000000000002";
  const PAPERS = [
    { paper_id: PAPER_A, title: "Alpha.pdf", status: "indexed", status_detail: null },
    { paper_id: PAPER_B, title: "Beta.pdf", status: "indexed", status_detail: null },
  ];

  function renderSection() {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const utils = render(
      (
        <QueryClientProvider client={queryClient}>
          <PaperRelationshipSection />
        </QueryClientProvider>
      ) as ReactNode,
    );
    return { ...utils, queryClient };
  }

  beforeEach(() => {
    url.set({ a: PAPER_A, b: PAPER_B });
    auth.userId = "user-1";
    (getPapersDetailed as any).mockResolvedValue(PAPERS);
    (getPaperIntelligence as any).mockImplementation(async (id: string) => {
      if (id === PAPER_A) return { intelligence: paper("ALPHA") };
      if (id === PAPER_B) return { intelligence: paper("BETA") };
      return null;
    });
  });

  it("renders the relationship from the URL selection", async () => {
    renderSection();

    expect(await screen.findByText("ALPHA methodology summary")).toBeInTheDocument();
    expect(screen.getByText("BETA methodology summary")).toBeInTheDocument();
    expect(screen.getAllByText("Supported by both")).toHaveLength(10);
  });

  it("uses the user-scoped per-paper query key", async () => {
    const { queryClient } = renderSection();
    await screen.findByText("ALPHA methodology summary");

    expect(
      queryClient.getQueryData(queryKeys.paperIntelligence("user-1", PAPER_A)),
    ).toBeTruthy();
    // A different account reads a different key entirely.
    expect(
      queryClient.getQueryData(queryKeys.paperIntelligence("user-2", PAPER_A)),
    ).toBeUndefined();
  });

  it("reads each paper at most once and never generates", async () => {
    renderSection();
    await screen.findByText("ALPHA methodology summary");

    expect((getPaperIntelligence as any).mock.calls.length).toBe(2);
    for (const call of (getPaperIntelligence as any).mock.calls) {
      // paperId only — no owner id is ever sent from the client.
      expect(call).toHaveLength(1);
    }
  });

  it("renders nothing until two distinct papers are selected", async () => {
    url.set({ a: PAPER_A });

    const { container } = renderSection();

    await waitFor(() => expect(getPapersDetailed).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  it("renders nothing when the same paper is chosen twice", async () => {
    url.set({ a: PAPER_A, b: PAPER_A });

    const { container } = renderSection();

    await waitFor(() => expect(getPapersDetailed).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  it("ignores a paper id the account does not own", async () => {
    url.set({ a: "ffffffff-0000-4000-8000-00000000000f", b: PAPER_B });

    const { container } = renderSection();

    await waitFor(() => expect(getPapersDetailed).toHaveBeenCalled());
    expect(container.textContent).toBe("");
    const asked = (getPaperIntelligence as any).mock.calls.map((c: any[]) => c[0]);
    expect(asked).not.toContain("ffffffff-0000-4000-8000-00000000000f");
  });

  it("fetches nothing while signed out", async () => {
    auth.userId = undefined;

    renderSection();

    await waitFor(() => expect(getPapersDetailed).not.toHaveBeenCalled());
    expect(getPaperIntelligence).not.toHaveBeenCalled();
  });

  it("shows the neutral state when one paper has no analysis", async () => {
    (getPaperIntelligence as any).mockImplementation(async (id: string) =>
      id === PAPER_A ? { intelligence: paper("ALPHA") } : null,
    );

    renderSection();

    expect(await screen.findByText(/has no structured analysis yet/)).toBeInTheDocument();
    expect(screen.getAllByText("Paper A only")).toHaveLength(10);
    expect(screen.queryByText(/BETA/)).toBeNull();
  });
});
