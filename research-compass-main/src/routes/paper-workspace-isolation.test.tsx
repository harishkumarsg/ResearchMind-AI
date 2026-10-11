/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Paper workspace — everything on the page belongs to ONE paper.
 *
 * The page shows AI output about a specific paper: a summary, answers,
 * an analysis, the evidence behind it, and the PDF at a particular page.
 * Every one of those is only true of the paper it was produced for. If
 * any of it is still on screen after the reader moves to another paper,
 * the page is asserting something false about that paper — which is the
 * one thing an evidence tool must not do.
 *
 * WHY THIS NEEDS A TEST AT ALL
 * ----------------------------
 * Moving from /paper/A to /paper/B does not, by itself, give the page a
 * fresh start. The installed router identifies a route's element by its
 * route id and keys the route component only when a `remountDeps` option
 * is configured — and this app configures none. So the SAME component
 * instance is re-rendered with new params, and any state it holds
 * survives the move unless the page itself ties that state to the paper.
 *
 * A "navigation" below is therefore exactly what the router does: change
 * what the route hooks return, and re-render the same element.
 *
 * Offline. The API module is mocked; nothing here reaches a network, a
 * provider or a database, and no real generation is ever requested.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const nav = vi.hoisted(() => ({
  paperName: "Paper_A.pdf",
  search: {} as { page?: number },
}));

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (options: any) => ({
    options,
    useParams: () => ({ paperName: nav.paperName }),
    useSearch: () => nav.search,
  }),
  Link: ({ children, to }: any) => <a href={to}>{children}</a>,
  useNavigate: () => vi.fn(),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    session: { access_token: "t" },
    user: { id: "user-1" },
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

// The real shell needs the whole sidebar; the page's own header actions
// (Delete lives there) are what matter here, so they are rendered.
vi.mock("@/components/app-shell", () => ({
  AppShell: ({ children, actions }: any) => (
    <div>
      <div>{actions}</div>
      {children}
    </div>
  ),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<any>("@/lib/api");
  return {
    ...actual,
    getPaperDetails: vi.fn(),
    summarizePaper: vi.fn(),
    deletePaper: vi.fn(),
    getPaperFileUrl: vi.fn(),
    getPaperIntelligence: vi.fn(),
    getPaperEvidence: vi.fn(),
    generatePaperIntelligence: vi.fn(),
    streamAskQuestion: vi.fn(),
  };
});

import { Route } from "@/routes/paper.$paperName";
import {
  deletePaper,
  generatePaperIntelligence,
  getPaperDetails,
  getPaperEvidence,
  getPaperFileUrl,
  getPaperIntelligence,
  INTELLIGENCE_SECTION_ORDER,
  streamAskQuestion,
  summarizePaper,
} from "@/lib/api";
import { queryKeys } from "@/lib/query-keys";

const PaperDetailsPage = (Route as any).options.component;

interface Paper {
  name: string;
  id: string;
}

const A: Paper = { name: "Paper_A.pdf", id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" };
const B: Paper = { name: "Paper_B.pdf", id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" };

const byName = (name: string) => (name === A.name ? A : B);
const byId = (id: string) => (id === A.id ? A : B);

/** What marks the page as showing a given paper's own details. */
const abstractOf = (paper: Paper) => `ABSTRACT-OF-${paper.name}`;

function details(paper: Paper) {
  return {
    status: "success",
    paper: paper.name,
    paper_id: paper.id,
    source: paper.name,
    authors: "",
    keywords: "",
    abstract: abstractOf(paper),
    total_chunks: 10,
    preview: "",
  };
}

/** A stored analysis whose Key Results section cites page 4, and only it. */
function analysis(paper: Paper, overrides: any = {}) {
  const intelligence: any = {};
  for (const name of INTELLIGENCE_SECTION_ORDER) {
    intelligence[name] = {
      status: "answered",
      summary: `A summary of ${name}.`,
      evidence: [{ page: 3, chunk_id: 0, quote: null }],
    };
  }
  intelligence.key_results = {
    status: "answered",
    summary: `KEY-RESULTS-OF-${paper.name}`,
    evidence: [{ page: 4, chunk_id: 0, quote: null }],
  };
  return {
    paper_id: paper.id,
    paper: paper.name,
    intelligence,
    generated_at: "2026-09-22T11:30:00+00:00",
    model: "test-model",
    schema_version: "2",
    superseded: false,
    ...overrides,
  };
}

/** A promise the test settles by hand. */
function deferred<T = any>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderWorkspace(start: Paper = A, search: { page?: number } = {}) {
  nav.paperName = start.name;
  nav.search = search;

  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = () =>
    (
      <QueryClientProvider client={queryClient}>
        <PaperDetailsPage />
      </QueryClientProvider>
    ) as ReactNode;
  const utils = render(tree());

  return {
    ...utils,
    queryClient,
    /** A link to another paper: same route component, new params. */
    goTo: (paper: Paper, nextSearch: { page?: number } = {}) => {
      nav.paperName = paper.name;
      nav.search = nextSearch;
      utils.rerender(tree());
    },
    /** An ordinary re-render: nothing about the route changed. */
    rerenderInPlace: (nextSearch?: { page?: number }) => {
      if (nextSearch) nav.search = nextSearch;
      utils.rerender(tree());
    },
  };
}

const showing = (paper: Paper) => screen.findByText(abstractOf(paper));
const viewer = () => screen.getByTitle("Paper document");
const viewerSrc = () => viewer().getAttribute("src");
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

async function viewerAt(paper: Paper, page: number) {
  await waitFor(() => expect(viewerSrc()).toBe(`blob:${paper.id}#page=${page}`));
}

function turnToPage(page: number) {
  fireEvent.change(screen.getByLabelText("Page number"), { target: { value: String(page) } });
}

function showAskTab() {
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Ask" }));
}

function ask(question = "what is the method?") {
  fireEvent.change(screen.getByLabelText(/ask about this paper/i), {
    target: { value: question },
  });
  fireEvent.click(screen.getByRole("button", { name: /^ask$/i }));
}

beforeEach(() => {
  vi.clearAllMocks();
  nav.paperName = A.name;
  nav.search = {};

  (URL as any).revokeObjectURL = vi.fn();
  vi.spyOn(window, "confirm").mockReturnValue(true);

  vi.mocked(getPaperDetails).mockReset();
  vi.mocked(getPaperDetails).mockImplementation(async (name: string) => details(byName(name)) as any);
  vi.mocked(getPaperFileUrl).mockReset();
  vi.mocked(getPaperFileUrl).mockImplementation(async (id: string) => `blob:${id}`);
  // By default no paper has an analysis; tests that need one say so.
  vi.mocked(getPaperIntelligence).mockReset();
  vi.mocked(getPaperIntelligence).mockResolvedValue(null);
  vi.mocked(getPaperEvidence).mockReset();
  vi.mocked(getPaperEvidence).mockResolvedValue({
    paper_id: A.id,
    generated_at: "2026-09-22T11:30:00+00:00",
    reindexed_since_analysis: false,
    items: [],
  } as any);
  vi.mocked(summarizePaper).mockReset();
  vi.mocked(deletePaper).mockReset();
  vi.mocked(generatePaperIntelligence).mockReset();
  vi.mocked(streamAskQuestion).mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ----------------------------------------------------------------------
// A link to another paper starts from a clean workspace
// ----------------------------------------------------------------------
describe("moving to another paper", () => {
  it("does not show the previous paper's AI summary", async () => {
    vi.mocked(summarizePaper).mockResolvedValue({ summary: "SUMMARY-OF-PAPER-A" } as any);

    const { goTo } = renderWorkspace(A);
    fireEvent.click(await screen.findByRole("button", { name: /generate ai summary/i }));
    await screen.findByText("SUMMARY-OF-PAPER-A");

    goTo(B);
    await showing(B);

    // The summary was written about paper A. It says nothing about B.
    expect(screen.queryByText("SUMMARY-OF-PAPER-A")).not.toBeInTheDocument();
    // And B must be able to get its own: the action is offered again.
    expect(screen.getByRole("button", { name: /generate ai summary/i })).toBeInTheDocument();
    expect(summarizePaper).toHaveBeenCalledTimes(1);
  });

  it("does not show the previous paper's summary error", async () => {
    vi.mocked(summarizePaper).mockRejectedValue(new Error("SUMMARY-FAILED-FOR-PAPER-A"));

    const { goTo } = renderWorkspace(A);
    fireEvent.click(await screen.findByRole("button", { name: /generate ai summary/i }));
    await screen.findByText("SUMMARY-FAILED-FOR-PAPER-A");

    goTo(B);
    await showing(B);

    expect(screen.queryByText("SUMMARY-FAILED-FOR-PAPER-A")).not.toBeInTheDocument();
  });

  it("does not show the previous paper's delete error", async () => {
    vi.mocked(deletePaper).mockRejectedValue(new Error("DELETE-FAILED-FOR-PAPER-A"));

    const { goTo } = renderWorkspace(A);
    await showing(A);
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    await screen.findByText("DELETE-FAILED-FOR-PAPER-A");

    goTo(B);
    await showing(B);

    // Nothing failed for B, and nothing was attempted on it.
    expect(screen.queryByText("DELETE-FAILED-FOR-PAPER-A")).not.toBeInTheDocument();
    expect(deletePaper).toHaveBeenCalledTimes(1);
    expect(deletePaper).toHaveBeenCalledWith(A.name);
  });

  it("opens the next paper at page 1, not at the previous paper's page", async () => {
    const { goTo } = renderWorkspace(A);
    await showing(A);
    await viewerAt(A, 1);

    turnToPage(7);
    await viewerAt(A, 7);

    goTo(B);
    await showing(B);

    // Page 7 was a place in paper A. Paper B opens from its beginning.
    await viewerAt(B, 1);
    expect(screen.getByLabelText("Page number")).toHaveValue(1);
  });

  it("opens the next paper at the page its link asks for", async () => {
    const { goTo } = renderWorkspace(A);
    await showing(A);
    turnToPage(7);
    await viewerAt(A, 7);

    goTo(B, { page: 3 });
    await showing(B);

    await viewerAt(B, 3);
  });

  it("does not carry a ?page from the previous paper's link to the next paper", async () => {
    const { goTo } = renderWorkspace(A, { page: 5 });
    await showing(A);
    await viewerAt(A, 5);

    goTo(B);
    await showing(B);

    await viewerAt(B, 1);
  });

  it("does not let a summary still in flight for the previous paper appear on the next", async () => {
    const slow = deferred<any>();
    vi.mocked(summarizePaper).mockReturnValue(slow.promise);

    const { goTo } = renderWorkspace(A);
    fireEvent.click(await screen.findByRole("button", { name: /generate ai summary/i }));
    await screen.findByText(/generating summary/i);

    goTo(B);
    await showing(B);

    // B is not waiting on anything: nobody asked for a summary of B.
    expect(screen.queryByText(/generating summary/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /generate ai summary/i })).toBeInTheDocument();

    // Paper A's summary arrives late — after the reader has moved on.
    await act(async () => {
      slow.resolve({ summary: "LATE-SUMMARY-OF-PAPER-A" });
      await slow.promise;
    });
    await settle();

    expect(screen.queryByText("LATE-SUMMARY-OF-PAPER-A")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /generate ai summary/i })).toBeInTheDocument();
    expect(summarizePaper).toHaveBeenCalledTimes(1);
    expect(summarizePaper).toHaveBeenCalledWith(A.name);
  });
});

// ----------------------------------------------------------------------
// The same, when the next paper's data is already cached
// ----------------------------------------------------------------------
describe("moving to a paper that is already cached", () => {
  // With the next paper's details cached there is no loading gap, so the
  // workspace panels stay mounted across the move. That is the case in
  // which their own state — an answer, an error, an open inspector — has
  // a chance to outlive the paper it belongs to. Every test here visits B
  // first so that it is cached, does something on A, and returns to B.

  async function visitBThenA() {
    const view = renderWorkspace(B);
    await showing(B);
    view.goTo(A);
    await showing(A);
    return view;
  }

  it("does not show the previous paper's Ask answer or citations", async () => {
    vi.mocked(streamAskQuestion).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "token", text: "ANSWER-ABOUT-PAPER-A" });
      onEvent({
        type: "done",
        citations: [{ paper: A.name, source: A.name, page: 4, cited: true }],
      });
    });

    const { goTo } = await visitBThenA();
    showAskTab();
    ask();
    await screen.findByText("ANSWER-ABOUT-PAPER-A");
    expect(screen.getByRole("button", { name: "Go to page 4" })).toBeInTheDocument();

    goTo(B);
    await showing(B);
    showAskTab();

    // "Answers are drawn from this paper only" — and this paper is B.
    expect(screen.queryByText("ANSWER-ABOUT-PAPER-A")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Go to page 4" })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/ask about this paper/i)).toHaveValue("");
    expect(screen.getByText(/ask a question to see an answer/i)).toBeInTheDocument();
    expect(streamAskQuestion).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamAskQuestion).mock.calls[0][3]).toBe(A.id);
  });

  it("does not show the previous paper's generation error", async () => {
    vi.mocked(generatePaperIntelligence).mockRejectedValue(
      new Error("GENERATION-FAILED-FOR-PAPER-A"),
    );

    const { goTo } = await visitBThenA();
    fireEvent.click(await screen.findByRole("button", { name: "Generate intelligence" }));
    await screen.findByText("GENERATION-FAILED-FOR-PAPER-A");
    expect(
      screen.getByText("Your existing analysis, if any, was not changed."),
    ).toBeInTheDocument();

    goTo(B);
    await showing(B);
    await screen.findByText(/no analysis yet/i);

    // Nothing was attempted for B, so nothing failed for B.
    expect(screen.queryByText("GENERATION-FAILED-FOR-PAPER-A")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Your existing analysis, if any, was not changed."),
    ).not.toBeInTheDocument();
    expect(generatePaperIntelligence).toHaveBeenCalledTimes(1);
    expect(generatePaperIntelligence).toHaveBeenCalledWith(A.id);
  });

  it("does not show the previous paper's 'newer analysis' notice", async () => {
    vi.mocked(getPaperIntelligence).mockImplementation(
      async (id: string) => analysis(byId(id)) as any,
    );
    vi.mocked(generatePaperIntelligence).mockResolvedValue(
      analysis(A, { superseded: true }) as any,
    );

    const { goTo } = await visitBThenA();
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));
    await screen.findByText("A newer analysis finished first and is shown.");

    goTo(B);
    await showing(B);
    await screen.findByText(`KEY-RESULTS-OF-${B.name}`);

    // That was a fact about a run on paper A.
    expect(
      screen.queryByText("A newer analysis finished first and is shown."),
    ).not.toBeInTheDocument();
  });

  it("does not leave the next paper busy with the previous paper's generation", async () => {
    vi.mocked(getPaperIntelligence).mockImplementation(
      async (id: string) => analysis(byId(id)) as any,
    );
    const slow = deferred<any>();
    vi.mocked(generatePaperIntelligence).mockReturnValue(slow.promise);

    const { goTo, queryClient } = await visitBThenA();
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));
    await screen.findByRole("button", { name: /analysing/i });

    goTo(B);
    await showing(B);
    await screen.findByText(`KEY-RESULTS-OF-${B.name}`);

    // B is not being analysed, and its own action is available.
    expect(screen.queryByText(/analysing this paper/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeEnabled();

    // Paper A's run finishes after the reader has left it.
    const finished = analysis(A, { generated_at: "2026-10-06T08:00:00+00:00" });
    finished.intelligence.key_results.summary = "REGENERATED-KEY-RESULTS-OF-PAPER-A";
    await act(async () => {
      slow.resolve(finished);
      await slow.promise;
    });
    await settle();

    // It changes nothing on B's page...
    expect(screen.getByText(`KEY-RESULTS-OF-${B.name}`)).toBeInTheDocument();
    expect(screen.queryByText("REGENERATED-KEY-RESULTS-OF-PAPER-A")).not.toBeInTheDocument();
    expect(
      screen.queryByText("A newer analysis finished first and is shown."),
    ).not.toBeInTheDocument();
    // ...and the result is not lost: it is A's stored analysis, under A's key.
    expect(queryClient.getQueryData(queryKeys.paperIntelligence("user-1", A.id))).toEqual(finished);
    expect(
      (queryClient.getQueryData(queryKeys.paperIntelligence("user-1", B.id)) as any).paper_id,
    ).toBe(B.id);
  });

  it("does not leave an evidence inspector open from the previous paper", async () => {
    // Both papers cite page 4 in the same section, so an "open page 4"
    // that outlived paper A would open B's evidence without a click.
    vi.mocked(getPaperIntelligence).mockImplementation(
      async (id: string) => analysis(byId(id)) as any,
    );

    const { goTo } = await visitBThenA();
    fireEvent.click(await screen.findByRole("button", { name: "Go to page 4" }));
    expect(screen.getByRole("region", { name: /evidence on page 4/i })).toBeInTheDocument();

    goTo(B);
    await showing(B);
    await screen.findByText(`KEY-RESULTS-OF-${B.name}`);

    expect(screen.queryByRole("region", { name: /evidence on page/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to page 4" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // The reader has not asked for B's source text, so none is read for it.
    await settle();
    for (const call of vi.mocked(getPaperEvidence).mock.calls) {
      expect(call[0]).toBe(A.id);
    }
  });
});

// ----------------------------------------------------------------------
// Staying on one paper keeps everything
// ----------------------------------------------------------------------
describe("staying on the same paper", () => {
  it("opens at ?page=N on first load", async () => {
    renderWorkspace(A, { page: 5 });
    await showing(A);

    await viewerAt(A, 5);
  });

  it("keeps the summary, the viewer page and an open inspector across re-renders", async () => {
    vi.mocked(getPaperIntelligence).mockImplementation(
      async (id: string) => analysis(byId(id)) as any,
    );
    vi.mocked(summarizePaper).mockResolvedValue({ summary: "SUMMARY-OF-PAPER-A" } as any);

    const { rerenderInPlace, queryClient } = renderWorkspace(A);
    await showing(A);

    fireEvent.click(await screen.findByRole("button", { name: "Go to page 4" }));
    turnToPage(7);
    await viewerAt(A, 7);
    fireEvent.click(screen.getByRole("button", { name: /generate ai summary/i }));
    await screen.findByText("SUMMARY-OF-PAPER-A");

    // Re-rendered by its parent, twice, and then by fresh data arriving.
    rerenderInPlace();
    rerenderInPlace();
    await act(async () => {
      await queryClient.invalidateQueries();
    });
    await settle();

    expect(screen.getByText("SUMMARY-OF-PAPER-A")).toBeInTheDocument();
    expect(viewerSrc()).toBe(`blob:${A.id}#page=7`);
    expect(screen.getByRole("region", { name: /evidence on page 4/i })).toBeInTheDocument();
    expect(summarizePaper).toHaveBeenCalledTimes(1);
  });

  it("keeps an Ask answer across re-renders", async () => {
    vi.mocked(streamAskQuestion).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "token", text: "ANSWER-ABOUT-PAPER-A" });
      onEvent({ type: "done", citations: [] });
    });

    const { rerenderInPlace } = renderWorkspace(A);
    await showing(A);
    showAskTab();
    ask();
    await screen.findByText("ANSWER-ABOUT-PAPER-A");

    rerenderInPlace();
    rerenderInPlace();

    expect(screen.getByText("ANSWER-ABOUT-PAPER-A")).toBeInTheDocument();
    expect(streamAskQuestion).toHaveBeenCalledTimes(1);
  });

  it("does not start over when only ?page changes", async () => {
    // The same paper with a different ?page is still the same paper: a
    // fresh start here would throw away a summary the reader paid for.
    vi.mocked(getPaperIntelligence).mockImplementation(
      async (id: string) => analysis(byId(id)) as any,
    );
    vi.mocked(summarizePaper).mockResolvedValue({ summary: "SUMMARY-OF-PAPER-A" } as any);

    const { rerenderInPlace } = renderWorkspace(A);
    await showing(A);
    fireEvent.click(await screen.findByRole("button", { name: "Go to page 4" }));
    fireEvent.click(screen.getByRole("button", { name: /generate ai summary/i }));
    await screen.findByText("SUMMARY-OF-PAPER-A");

    rerenderInPlace({ page: 9 });
    await settle();

    expect(screen.getByText("SUMMARY-OF-PAPER-A")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /evidence on page 4/i })).toBeInTheDocument();
    expect(summarizePaper).toHaveBeenCalledTimes(1);
  });
});
