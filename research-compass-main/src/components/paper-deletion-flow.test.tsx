/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Deleting a paper, end to end: from the click, through the real API
 * client and the HTTP response, to what the sidebar, the paper page and
 * the dashboard show afterwards.
 *
 * Only the network is replaced. `fetch` is answered by a small in-memory
 * backend that mirrors the shapes the FastAPI routes return — including
 * DELETE /papers/{paper_id}'s 502 with a JSON `detail` when a removal
 * fails partway — so deletePaper, getPapersDetailed, getPaperDetails and
 * getDashboardStats run unmodified. The real AppShell (sidebar), paper
 * page and dashboard share one QueryClient, and navigate() really changes
 * which route is rendered, so a refresh, a navigation or a stale cache
 * entry is observed exactly as a reader would see it.
 *
 * Offline. Nothing here reaches a network, a provider or a database, and
 * no generation is ever requested.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

// ----------------------------------------------------------------------
// A router that actually moves: navigate() changes the rendered route.
// ----------------------------------------------------------------------
const nav = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  const state = { path: "/dashboard" };
  return {
    state,
    navigate: vi.fn(),
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    go(path: string) {
      state.path = path;
      listeners.forEach((listener) => listener());
    },
  };
});

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (options: any) => ({
    options,
    useParams: () => ({
      paperName: decodeURIComponent(nav.state.path.replace(/^\/paper\//, "")),
    }),
    useSearch: () => ({}),
  }),
  Link: ({ children, to, params }: any) => (
    <a href={params?.paperName ? `/paper/${params.paperName}` : to}>{children}</a>
  ),
  useNavigate: () => nav.navigate,
  useRouterState: ({ select }: any) => select({ location: { pathname: nav.state.path } }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    session: { access_token: "user-1-token" },
    user: { id: "user-1" },
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

// The token authFetch attaches. Every request below must carry it.
vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: () => ({
    auth: {
      getSession: async () => ({ data: { session: { access_token: "user-1-token" } } }),
    },
  }),
}));

// Only the paper page's panels are stubbed — they are not part of
// deletion. Everything deletion touches is the real client.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<any>("@/lib/api");
  return {
    ...actual,
    getPaperFileUrl: vi.fn(),
    getPaperIntelligence: vi.fn(),
    getPaperEvidence: vi.fn(),
    generatePaperIntelligence: vi.fn(),
    streamAskQuestion: vi.fn(),
    summarizePaper: vi.fn(),
  };
});

import { Route as PaperRoute } from "@/routes/paper.$paperName";
import { Route as DashboardRoute } from "@/routes/dashboard";
import {
  API_BASE_URL,
  deletePaper,
  getPaperFileUrl,
  getPaperIntelligence,
  getPaperEvidence,
  type PaperDetail,
} from "@/lib/api";
import { queryKeys } from "@/lib/query-keys";

const PaperPage = (PaperRoute as any).options.component;
const DashboardPage = (DashboardRoute as any).options.component;

// ----------------------------------------------------------------------
// The backend, in memory
// ----------------------------------------------------------------------

// Verbatim from backend/app/api/delete_paper.py and
// backend/app/services/paper_lifecycle.py.
const DELETE_INCOMPLETE_DETAIL =
  "This paper could not be fully removed. It is still in your library, " +
  "so you can try deleting it again.";
const DELETION_INCOMPLETE_MARKER =
  "Removal did not finish. Delete this paper again to complete it.";

type Row = PaperDetail;

const server = {
  rows: [] as Row[],
  /** What DELETE /papers/{id} does: finish, or fail partway (502). */
  deleteOutcome: "deleted" as "deleted" | "incomplete",
  /** When set, DELETE answers with this instead (and changes nothing). */
  deleteReply: null as null | (() => Response),
  requests: [] as { method: string; path: string; authorization: string | null }[],
  unexpected: [] as string[],
};

const reply = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/** What marks the page as showing a given paper's own details. */
const abstractOf = (title: string) => `ABSTRACT-OF-${title}`;

async function backend(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input));
  const method = (init?.method ?? "GET").toUpperCase();
  server.requests.push({
    method,
    path: url.pathname + url.search,
    authorization: new Headers(init?.headers).get("Authorization"),
  });
  const indexed = server.rows.filter((row) => row.status === "indexed");

  if (method === "GET" && url.pathname === "/papers") {
    return reply(200, {
      status: "success",
      papers: indexed.map((row) => row.title),
      papers_detailed: server.rows.map((row) => ({ ...row })),
    });
  }

  if (method === "GET" && url.pathname === "/stats") {
    return reply(200, {
      status: "success",
      total_papers: indexed.length,
      total_chunks: indexed.length * 10,
      recent_papers: indexed.map((row) => row.title),
    });
  }

  if (method === "GET" && url.pathname === "/paper-details") {
    // Only an indexed paper is ever found (the retrieval guard).
    const title = url.searchParams.get("paper_name") ?? "";
    const row = indexed.find((candidate) => candidate.title === title);
    if (!row) return reply(200, { status: "error", message: `Paper not found: ${title}` });
    return reply(200, {
      status: "success",
      paper: row.title,
      paper_id: row.paper_id,
      source: row.title,
      authors: "",
      keywords: "",
      abstract: abstractOf(row.title),
      total_chunks: 10,
      preview: "",
    });
  }

  if (method === "DELETE" && url.pathname.startsWith("/papers/")) {
    if (server.deleteReply) return server.deleteReply();
    const id = decodeURIComponent(url.pathname.slice("/papers/".length));
    const row = server.rows.find((candidate) => candidate.paper_id === id);
    if (!row) return reply(404, { detail: "Paper not found" });
    if (server.deleteOutcome === "incomplete") {
      // The claim is released: the row stays, marked for another attempt.
      row.status = "deleting";
      row.status_detail = DELETION_INCOMPLETE_MARKER;
      return reply(502, { detail: DELETE_INCOMPLETE_DETAIL });
    }
    server.rows = server.rows.filter((candidate) => candidate !== row);
    return reply(200, {
      status: "success",
      message: "Paper deleted successfully.",
      paper_id: id,
      vectors_deleted: 10,
    });
  }

  server.unexpected.push(`${method} ${url.pathname}`);
  return reply(500, { detail: "unexpected request in test" });
}

/** The backend's library. Copies: a failed delete changes a row's status. */
function serve(...rows: Row[]) {
  server.rows = rows.map((row) => ({ ...row }));
}

const deletes = () => server.requests.filter((request) => request.method === "DELETE");
const detailsRequestsFor = (title: string) =>
  server.requests.filter(
    (request) => request.path === `/paper-details?paper_name=${encodeURIComponent(title)}`,
  );

// ----------------------------------------------------------------------
// Papers
// ----------------------------------------------------------------------

const indexedRow = (paper_id: string, title: string): Row => ({
  paper_id,
  title,
  status: "indexed",
  status_detail: null,
});

const A = indexedRow("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "Paper_A.pdf");
const B = indexedRow("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "Paper_B.pdf");
// Two DIFFERENT papers that happen to share a filename.
const COPY_ONE = indexedRow("c1c1c1c1-1111-4111-8111-111111111111", "paper.pdf");
const COPY_TWO = indexedRow("c2c2c2c2-2222-4222-8222-222222222222", "paper.pdf");

// ----------------------------------------------------------------------
// Rendering
// ----------------------------------------------------------------------

function CurrentRoute() {
  const path = useSyncExternalStore(nav.subscribe, () => nav.state.path);
  return path.startsWith("/paper/") ? <PaperPage /> : <DashboardPage />;
}

const clients: QueryClient[] = [];

function renderApp(path: string, queryClient = new QueryClient()) {
  clients.push(queryClient);
  nav.state.path = path;
  render(
    <QueryClientProvider client={queryClient}>
      <CurrentRoute />
    </QueryClientProvider>,
  );
  return queryClient;
}

/** A link or the browser's Back button: the route changes. */
const visit = (path: string) => act(() => nav.go(path));
const paperPath = (row: Row) => `/paper/${encodeURIComponent(row.title)}`;

/** The sidebar's Indexed Papers list. */
function indexedList() {
  const heading = screen.getByText("Indexed Papers");
  return within(heading.parentElement as HTMLElement);
}

const display = (title: string) => title.replace(/_/g, " ");

/** The sidebar's Processing list: papers not ready to query, including one being removed. */
function processingList() {
  const heading = screen.getByText("Processing");
  return within(heading.parentElement as HTMLElement);
}

/** The sidebar entry showing this text (a title or a short id), and its delete button. */
async function sidebarDeleteButton(text: string) {
  const label = await waitFor(() => indexedList().getByText(text));
  return within(label.closest("div") as HTMLElement).getByTitle("Delete paper");
}

/** The paper page's own Delete, once the library list has named the paper. */
async function pageDeleteButton() {
  const button = screen.getByRole("button", { name: /^delete$/i });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

const showing = (row: Row) => screen.findByText(abstractOf(row.title));
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

let confirmSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.clearAllMocks();
  serve();
  server.deleteOutcome = "deleted";
  server.deleteReply = null;
  server.requests = [];
  server.unexpected = [];
  nav.navigate.mockImplementation(({ to }: { to: string }) => nav.go(to));
  vi.spyOn(globalThis, "fetch").mockImplementation(backend as typeof fetch);
  confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
  (URL as any).revokeObjectURL = vi.fn();
  vi.mocked(getPaperFileUrl).mockImplementation(async (id: string) => `blob:${id}`);
  vi.mocked(getPaperIntelligence).mockResolvedValue(null);
  vi.mocked(getPaperEvidence).mockResolvedValue({ items: [] } as any);
});

afterEach(() => {
  // Tear down first, so a failed check below cannot leave this test's
  // tree mounted or its spies installed for the next test.
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
  // Every request was one this file's backend knows, and carried the token.
  expect(server.unexpected).toEqual([]);
  for (const request of server.requests) {
    expect(request.authorization).toBe("Bearer user-1-token");
  }
});

// ----------------------------------------------------------------------
// A. Deleting from the sidebar
// ----------------------------------------------------------------------
describe("deleting from the sidebar", () => {
  it("sends DELETE /papers/<paper_id> for the entry pressed — never the title route", async () => {
    serve(COPY_ONE, COPY_TWO);
    renderApp("/dashboard");

    fireEvent.click(await sidebarDeleteButton("c2c2c2c2"));

    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(deletes()[0].path).toBe(`/papers/${COPY_TWO.paper_id}`);
    expect(server.requests.some((request) => request.path.startsWith("/paper/"))).toBe(false);
  });

  it("keeps same-named papers distinguishable, and the confirmation names the one pressed", async () => {
    serve(COPY_ONE, COPY_TWO, B);
    renderApp("/dashboard");

    expect(await waitFor(() => indexedList().getByText("c1c1c1c1"))).toBeInTheDocument();
    expect(indexedList().getByText("c2c2c2c2")).toBeInTheDocument();
    expect(indexedList().getAllByText("paper.pdf")).toHaveLength(2);

    fireEvent.click(await sidebarDeleteButton("c1c1c1c1"));

    const prompt = String(confirmSpy.mock.calls[0][0]);
    expect(prompt).toContain('"paper.pdf" (c1c1c1c1)');
    expect(prompt).not.toContain("c2c2c2c2");
    expect(prompt).toContain("Only this one will be deleted");
    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(deletes()[0].path).toBe(`/papers/${COPY_ONE.paper_id}`);
  });

  it("after a successful delete, removes exactly that paper and keeps the rest", async () => {
    serve(COPY_ONE, COPY_TWO, B);
    renderApp("/dashboard");

    fireEvent.click(await sidebarDeleteButton("c2c2c2c2"));

    // The list is refreshed from the server, which no longer has COPY_TWO.
    await waitFor(() => expect(indexedList().queryByText("c2c2c2c2")).not.toBeInTheDocument());
    // Its same-named sibling is still there — now the only "paper.pdf",
    // so it no longer needs a short id — and the unrelated paper too.
    expect(indexedList().getAllByText("paper.pdf")).toHaveLength(1);
    expect(indexedList().queryByText("c1c1c1c1")).not.toBeInTheDocument();
    expect(indexedList().getByText(display(B.title))).toBeInTheDocument();
    expect(deletes()).toHaveLength(1);
    expect(nav.navigate).not.toHaveBeenCalled();
  });

  it("deletes nothing when the confirmation is declined", async () => {
    confirmSpy.mockReturnValue(false);
    serve(COPY_ONE, COPY_TWO);
    renderApp("/dashboard");

    fireEvent.click(await sidebarDeleteButton("c1c1c1c1"));
    await settle();

    expect(deletes()).toHaveLength(0);
    expect(indexedList().getByText("c1c1c1c1")).toBeInTheDocument();
  });
});

// ----------------------------------------------------------------------
// B. Deleting from the paper page
// ----------------------------------------------------------------------
describe("deleting from the paper page", () => {
  it("sends DELETE /papers/<paper_id> for the library's id", async () => {
    serve(A, B);
    renderApp(paperPath(A));
    await showing(A);

    fireEvent.click(await pageDeleteButton());

    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(deletes()[0].path).toBe(`/papers/${A.paper_id}`);
  });

  it("refuses a title shared by two papers, and the sidebar tells the two apart", async () => {
    serve(COPY_ONE, COPY_TWO, B);
    renderApp(paperPath(COPY_ONE));
    await showing(COPY_ONE);

    expect(
      await screen.findByText(/more than one paper in your library is named/i),
    ).toBeInTheDocument();
    const button = screen.getByRole("button", { name: /^delete$/i });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    await settle();

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(deletes()).toHaveLength(0);
    // Where the notice sends the reader: each copy has its own entry.
    expect(indexedList().getByText("c1c1c1c1")).toBeInTheDocument();
    expect(indexedList().getByText("c2c2c2c2")).toBeInTheDocument();
  });

  it("after a successful delete, goes to /dashboard and the library no longer lists the paper", async () => {
    serve(A, B);
    // Arrive the usual way: the dashboard first, so its lists are cached.
    renderApp("/dashboard");
    await waitFor(() => expect(indexedList().getByText(display(A.title))).toBeInTheDocument());
    visit(paperPath(A));
    await showing(A);

    fireEvent.click(await pageDeleteButton());

    await waitFor(() => expect(nav.navigate).toHaveBeenCalledWith({ to: "/dashboard" }));
    expect(nav.navigate).toHaveBeenCalledTimes(1);
    expect(nav.state.path).toBe("/dashboard");
    // The sidebar and the dashboard's own list are refreshed from the server.
    await waitFor(() =>
      expect(indexedList().queryByText(display(A.title))).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(screen.queryAllByText(display(A.title))).toHaveLength(0));
    await waitFor(() => expect(screen.queryAllByText(A.title)).toHaveLength(0));
    // Only the deleted paper went.
    expect(indexedList().getByText(display(B.title))).toBeInTheDocument();
  });

  it("does not show the deleted paper again from the cache when the reader goes back", async () => {
    serve(A, B);
    renderApp(paperPath(A));
    await showing(A);
    const detailsRequestsBefore = detailsRequestsFor(A.title).length;

    fireEvent.click(await pageDeleteButton());
    await waitFor(() => expect(nav.state.path).toBe("/dashboard"));

    // Back to /paper/Paper_A.pdf.
    visit(paperPath(A));

    // The paper is gone. What the page had cached about it must not be
    // rendered again as though it were still there.
    expect(screen.queryByText(abstractOf(A.title))).not.toBeInTheDocument();
    expect(screen.queryByTitle("Paper document")).not.toBeInTheDocument();
    // The page asks the server again instead.
    await waitFor(() =>
      expect(detailsRequestsFor(A.title).length).toBeGreaterThan(detailsRequestsBefore),
    );
    await settle();
    expect(screen.queryByText(abstractOf(A.title))).not.toBeInTheDocument();
  });
});

// ----------------------------------------------------------------------
// C. A deletion that fails: HTTP 502 with a JSON `detail`
// ----------------------------------------------------------------------
describe("a deletion that fails partway (HTTP 502)", () => {
  const JSON_PARSE_ERROR = /JSON|Unexpected token|SyntaxError/;

  it("the API client rejects with the backend's own detail, not a parsing error", async () => {
    serve(A);
    server.deleteOutcome = "incomplete";

    const failure = await deletePaper(A.paper_id).then(
      () => null,
      (error: unknown) => error,
    );

    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(SyntaxError);
    expect((failure as Error).message).toBe(DELETE_INCOMPLETE_DETAIL);
    expect(deletes()).toEqual([
      { method: "DELETE", path: `/papers/${A.paper_id}`, authorization: "Bearer user-1-token" },
    ]);
  });

  it("the API client resolves for the backend's success body", async () => {
    serve(A);

    await expect(deletePaper(A.paper_id)).resolves.toBeUndefined();
    expect(server.rows).toEqual([]);
  });

  it("from the paper page: shows the backend's message, stays, and keeps the paper", async () => {
    serve(A, B);
    server.deleteOutcome = "incomplete";
    renderApp(paperPath(A));
    await showing(A);

    fireEvent.click(await pageDeleteButton());

    expect(await screen.findByText(DELETE_INCOMPLETE_DETAIL)).toBeInTheDocument();
    // Nothing claims the paper is gone.
    expect(nav.navigate).not.toHaveBeenCalled();
    expect(nav.state.path).toBe(paperPath(A));
    expect(screen.getByText(abstractOf(A.title))).toBeInTheDocument();
    // The sidebar is reconciled with the server, which now has the paper
    // mid-removal: still listed, as being removed — not as ready.
    await waitFor(() => expect(processingList().getByText(display(A.title))).toBeInTheDocument());
    expect(processingList().getByText("Removing…")).toBeInTheDocument();
    expect(indexedList().queryByText(display(A.title))).not.toBeInTheDocument();
    // It can be tried again, as the message says, and the message stays.
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeEnabled();
    expect(screen.getByText(DELETE_INCOMPLETE_DETAIL)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(JSON_PARSE_ERROR);
  });

  it("from the sidebar: shows the backend's message for that paper, and keeps it listed", async () => {
    serve(COPY_ONE, COPY_TWO);
    server.deleteOutcome = "incomplete";
    renderApp("/dashboard");

    fireEvent.click(await sidebarDeleteButton("c2c2c2c2"));

    // The reader is told what happened, in the backend's words, and
    // which of the two same-named papers it happened to.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(DELETE_INCOMPLETE_DETAIL);
    expect(alert).toHaveTextContent("c2c2c2c2");
    expect(alert.textContent).not.toMatch(JSON_PARSE_ERROR);
    // Nothing claims the paper is gone. The sidebar is reconciled with the
    // server, which now has it mid-removal: listed as being removed — not
    // as ready — with Retry removal offered.
    await waitFor(() => expect(processingList().getByText("Removing…")).toBeInTheDocument());
    expect(processingList().getByText("paper.pdf")).toBeInTheDocument();
    expect(processingList().getByTitle("Retry removal")).toBeEnabled();
    expect(indexedList().queryByText("c2c2c2c2")).not.toBeInTheDocument();
    // Its same-named sibling is untouched and still ready.
    expect(indexedList().getByText("c1c1c1c1")).toBeInTheDocument();
    // The failure stays on screen after the refresh.
    expect(screen.getByRole("alert")).toHaveTextContent(DELETE_INCOMPLETE_DETAIL);
    expect(nav.navigate).not.toHaveBeenCalled();
    expect(deletes()).toHaveLength(1);
  });
});

// ----------------------------------------------------------------------
// D. Nothing else is removed from local state
// ----------------------------------------------------------------------
describe("deleting leaves other cached data alone", () => {
  it("another user's cached papers, and this user's other papers, are untouched", async () => {
    serve(A, B);
    const queryClient = new QueryClient();
    const otherUsers = {
      papersDetailed: [indexedRow("99999999-9999-4999-8999-999999999999", A.title)],
      papers: [A.title],
      details: { status: "success", paper: A.title, abstract: "USER-2-ABSTRACT" },
      stats: { status: "success", total_papers: 1, total_chunks: 5, recent_papers: [A.title] },
    };
    queryClient.setQueryData(queryKeys.papersDetailed("user-2"), otherUsers.papersDetailed);
    queryClient.setQueryData(queryKeys.papers("user-2"), otherUsers.papers);
    queryClient.setQueryData(queryKeys.paperDetails("user-2", A.title), otherUsers.details);
    queryClient.setQueryData(queryKeys.stats("user-2"), otherUsers.stats);
    // This user has also read paper B.
    const bDetails = { status: "success", paper: B.title, abstract: abstractOf(B.title) };
    queryClient.setQueryData(queryKeys.paperDetails("user-1", B.title), bDetails);

    renderApp(paperPath(A), queryClient);
    await showing(A);
    fireEvent.click(await pageDeleteButton());
    await waitFor(() => expect(nav.state.path).toBe("/dashboard"));
    await waitFor(() =>
      expect(indexedList().queryByText(display(A.title))).not.toBeInTheDocument(),
    );

    // The other user's entries keep their data and are not even marked stale.
    for (const [key, value] of [
      [queryKeys.papersDetailed("user-2"), otherUsers.papersDetailed],
      [queryKeys.papers("user-2"), otherUsers.papers],
      [queryKeys.paperDetails("user-2", A.title), otherUsers.details],
      [queryKeys.stats("user-2"), otherUsers.stats],
    ] as const) {
      expect(queryClient.getQueryData(key)).toEqual(value);
      expect(queryClient.getQueryState(key)?.isInvalidated).toBe(false);
    }
    // This user's other paper keeps its cached details, and stays listed.
    expect(queryClient.getQueryData(queryKeys.paperDetails("user-1", B.title))).toEqual(bDetails);
    expect(indexedList().getByText(display(B.title))).toBeInTheDocument();
    expect(deletes()).toHaveLength(1);
  });
});

// ----------------------------------------------------------------------
// E. Deleting, from the sidebar, the paper the page has open
// ----------------------------------------------------------------------
describe("deleting the open paper from the sidebar", () => {
  const viewerSrc = () => screen.getByTitle("Paper document").getAttribute("src");

  it("goes to /dashboard, and going back does not show the paper from the cache", async () => {
    serve(A, B);
    renderApp(paperPath(A));
    await showing(A);
    const detailsRequestsBefore = detailsRequestsFor(A.title).length;

    fireEvent.click(await sidebarDeleteButton(display(A.title)));

    await waitFor(() => expect(nav.navigate).toHaveBeenCalledWith({ to: "/dashboard" }));
    expect(nav.navigate).toHaveBeenCalledTimes(1);
    expect(nav.state.path).toBe("/dashboard");
    // The existing refresh still happens: only the deleted paper went.
    await waitFor(() =>
      expect(indexedList().queryByText(display(A.title))).not.toBeInTheDocument(),
    );
    expect(indexedList().getByText(display(B.title))).toBeInTheDocument();

    visit(paperPath(A));

    expect(screen.queryByText(abstractOf(A.title))).not.toBeInTheDocument();
    await waitFor(() =>
      expect(detailsRequestsFor(A.title).length).toBeGreaterThan(detailsRequestsBefore),
    );
  });

  it("stays when a different paper is deleted", async () => {
    serve(A, B);
    renderApp(paperPath(A));
    await showing(A);

    fireEvent.click(await sidebarDeleteButton(display(B.title)));

    await waitFor(() =>
      expect(indexedList().queryByText(display(B.title))).not.toBeInTheDocument(),
    );
    await settle();
    expect(nav.navigate).not.toHaveBeenCalled();
    expect(nav.state.path).toBe(paperPath(A));
    expect(screen.getByText(abstractOf(A.title))).toBeInTheDocument();
    expect(deletes().map((request) => request.path)).toEqual([`/papers/${B.paper_id}`]);
  });

  it("stays when the deletion fails", async () => {
    serve(A, B);
    server.deleteOutcome = "incomplete";
    renderApp(paperPath(A));
    await showing(A);

    fireEvent.click(await sidebarDeleteButton(display(A.title)));

    expect(await screen.findByRole("alert")).toHaveTextContent(DELETE_INCOMPLETE_DETAIL);
    await settle();
    expect(nav.navigate).not.toHaveBeenCalled();
    expect(nav.state.path).toBe(paperPath(A));
    expect(screen.getByText(abstractOf(A.title))).toBeInTheDocument();
  });

  it("stays on a shared title when one copy is deleted, and shows the copy that remains", async () => {
    serve(COPY_ONE, COPY_TWO, B);
    renderApp(paperPath(COPY_ONE));
    await showing(COPY_ONE);
    // The page shows whichever copy the title search found: here, copy one.
    await waitFor(() => expect(viewerSrc()).toBe(`blob:${COPY_ONE.paper_id}#page=1`));

    fireEvent.click(await sidebarDeleteButton("c1c1c1c1"));

    // The title still names a paper, so the page is still valid…
    await waitFor(() => expect(indexedList().queryByText("c1c1c1c1")).not.toBeInTheDocument());
    await settle();
    expect(nav.navigate).not.toHaveBeenCalled();
    expect(nav.state.path).toBe(paperPath(COPY_ONE));
    // …but what it showed was the deleted copy: it is fetched again, and
    // the copy that remains is shown instead.
    await waitFor(() => expect(viewerSrc()).toBe(`blob:${COPY_TWO.paper_id}#page=1`));
    expect(deletes().map((request) => request.path)).toEqual([`/papers/${COPY_ONE.paper_id}`]);
  });
});

// ----------------------------------------------------------------------
// F. After a failed deletion the paper stays visible and retryable
// ----------------------------------------------------------------------
describe("retrying a deletion that failed partway", () => {
  it("offers Retry removal for the paper, which then deletes it by id", async () => {
    serve(A, B);
    server.deleteOutcome = "incomplete";
    renderApp("/dashboard");

    fireEvent.click(await sidebarDeleteButton(display(A.title)));
    await waitFor(() => expect(processingList().getByTitle("Retry removal")).toBeEnabled());

    server.deleteOutcome = "deleted";
    fireEvent.click(processingList().getByTitle("Retry removal"));

    await waitFor(() => expect(screen.queryByText("Processing")).not.toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(indexedList().queryByText(display(A.title))).not.toBeInTheDocument();
    expect(indexedList().getByText(display(B.title))).toBeInTheDocument();
    expect(deletes().map((request) => request.path)).toEqual([
      `/papers/${A.paper_id}`,
      `/papers/${A.paper_id}`,
    ]);
  });
});

// ----------------------------------------------------------------------
// G. The dashboard's recent papers, when two share a filename
// ----------------------------------------------------------------------
describe("the dashboard's recent papers", () => {
  /** The key React holds for this element, read from its fiber. */
  function reactKey(element: Element) {
    const fiber = Object.keys(element).find((name) => name.startsWith("__reactFiber$"));
    return fiber ? (element as any)[fiber].key : undefined;
  }

  it("keys two same-named papers by their ids, with no duplicate keys", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    // The library lists the two copies in reverse id order. /papers sorts
    // by title only, so their relative order can differ between fetches;
    // the keys must not swap with it. A third, failed upload of the same
    // name is not a recent paper and must not lend its id to one.
    const failedCopy: Row = {
      ...indexedRow("c0c0c0c0-0000-4000-8000-000000000000", "paper.pdf"),
      status: "failed",
      status_detail: "Processing this paper failed. Please try again.",
    };
    serve(failedCopy, COPY_TWO, COPY_ONE, B);
    renderApp("/dashboard");

    const recent = () =>
      within(screen.getByText("Recent uploads").closest("section") as HTMLElement);
    const cards = () =>
      recent()
        .getAllByText(/\.pdf$/)
        .map((title) => title.parentElement as HTMLElement);

    // Display unchanged: one card per recent paper, in /stats order.
    await waitFor(() =>
      expect(cards().map((card) => card.textContent)).toEqual([
        "paper.pdf",
        "paper.pdf",
        "Paper_B.pdf",
      ]),
    );
    // Each card is keyed by its own paper's id.
    await waitFor(() =>
      expect(cards().map(reactKey)).toEqual([COPY_ONE.paper_id, COPY_TWO.paper_id, B.paper_id]),
    );
    const duplicateKeyWarnings = consoleError.mock.calls.filter((call) =>
      call.map(String).join(" ").includes("same key"),
    );
    expect(duplicateKeyWarnings).toEqual([]);
  });
});

// ----------------------------------------------------------------------
// H. An error body that is not the backend's JSON
// ----------------------------------------------------------------------
describe("deletePaper and error bodies that are not the backend's JSON", () => {
  const FALLBACK = "Delete failed. Please try again.";
  const JSON_PARSE_ERROR = /JSON|Unexpected token|SyntaxError|unexpected end/i;

  it.each([
    ["plain text", () => new Response("Bad Gateway", { status: 502 })],
    [
      "an HTML error page",
      () =>
        new Response("<!DOCTYPE html><html><body>502 Bad Gateway</body></html>", {
          status: 502,
          headers: { "Content-Type": "text/html" },
        }),
    ],
    ["an empty body", () => new Response(null, { status: 502 })],
    ["JSON without a detail", () => reply(500, { error: "internal" })],
    ["JSON whose detail is not text", () => reply(422, { detail: [{ msg: "bad id" }] })],
  ])("rejects with a readable message for %s, never a parsing error", async (_, body) => {
    serve(A);
    server.deleteReply = body;

    const failure = await deletePaper(A.paper_id).then(
      () => null,
      (error: unknown) => error,
    );

    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(SyntaxError);
    expect((failure as Error).message).toBe(FALLBACK);
  });

  it("still passes the backend's JSON detail through unchanged", async () => {
    serve(A);
    server.deleteReply = () => reply(409, { detail: "This paper is busy — try again shortly" });

    await expect(deletePaper(A.paper_id)).rejects.toThrow(
      /^This paper is busy — try again shortly$/,
    );
  });

  it("the sidebar shows the readable message for a plain-text error", async () => {
    serve(A, B);
    server.deleteReply = () => new Response("Bad Gateway", { status: 502 });
    renderApp("/dashboard");

    fireEvent.click(await sidebarDeleteButton(display(A.title)));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Could not delete “${display(A.title)}”. ${FALLBACK}`);
    expect(alert.textContent).not.toMatch(JSON_PARSE_ERROR);
    expect(alert.textContent).not.toContain("Bad Gateway");
  });

  it("the paper page shows the readable message for an empty error body", async () => {
    serve(A, B);
    server.deleteReply = () => new Response(null, { status: 502 });
    renderApp(paperPath(A));
    await showing(A);

    fireEvent.click(await pageDeleteButton());

    expect(await screen.findByText(FALLBACK)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(JSON_PARSE_ERROR);
    expect(nav.navigate).not.toHaveBeenCalled();
  });
});
