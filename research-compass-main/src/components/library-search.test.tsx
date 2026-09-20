/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ComponentType } from "react";

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to }: any) => <a href={to}>{children}</a>,
  useNavigate: () => vi.fn(),
  useRouterState: ({ select }: any) => select({ location: { pathname: "/search" } }),
  createFileRoute: () => (options: any) => ({ options }),
}));

// Signed in, so the search query is enabled.
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    session: {},
    user: { id: "user-a", email: "a@example.com" },
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

// The real module (so RateLimitError is the real class), with every network
// call replaced.
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  getPapers: vi.fn().mockResolvedValue([]),
  getPapersDetailed: vi.fn().mockResolvedValue([]),
  deletePaper: vi.fn(),
  searchPapers: vi.fn(),
}));

import { LIBRARY_SEARCH_INPUT_ID } from "@/components/app-shell";
import { RateLimitError, searchPapers } from "@/lib/api";
import { Route as SearchRoute } from "@/routes/search";

const search = searchPapers as unknown as ReturnType<typeof vi.fn>;
const DEBOUNCE_MS = 450;

function renderSearchPage() {
  // Library defaults (retry: 3), so the page's own retry rule is what runs.
  const queryClient = new QueryClient();
  const SearchPage = SearchRoute.options.component as ComponentType;
  render(
    <QueryClientProvider client={queryClient}>
      <SearchPage />
    </QueryClientProvider>,
  );
}

const input = () => document.getElementById(LIBRARY_SEARCH_INPUT_ID) as HTMLInputElement;
const type = (value: string) => fireEvent.change(input(), { target: { value } });
const advance = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const result = (paper: string) => ({
  paper,
  source: `${paper}.pdf`,
  page: 1,
  authors: "",
  abstract: `${paper} abstract`,
  text: "",
});

beforeEach(() => {
  vi.useFakeTimers();
  search.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Library search — debounced semantic search", () => {
  it("sends only the final query, once typing has paused for the debounce window", async () => {
    search.mockResolvedValue([]);
    renderSearchPage();

    for (const value of ["tra", "tran", "trans", "transf", "transformer"]) {
      type(value);
      await advance(100);
    }

    // The input and its label update immediately; only the request waits.
    expect(input().value).toBe("transformer");
    expect(screen.getByText('Searching for "transformer"')).toBeInTheDocument();

    await advance(DEBOUNCE_MS - 101);
    expect(search).not.toHaveBeenCalled();

    await advance(1);
    expect(search).toHaveBeenCalledTimes(1);
    expect(search).toHaveBeenCalledWith("transformer", expect.any(AbortSignal));
  });

  it("never searches for a query shorter than 3 characters", async () => {
    search.mockResolvedValue([]);
    renderSearchPage();

    type("a");
    await advance(DEBOUNCE_MS * 2);
    type("ab");
    await advance(DEBOUNCE_MS * 2);
    type("abc");
    await advance(100);
    type("ab"); // back below the minimum before the pause
    await advance(DEBOUNCE_MS * 2);

    expect(search).not.toHaveBeenCalled();
    expect(screen.queryByText(/result\(s\)/)).not.toBeInTheDocument();
  });

  it("a slow response for an older query never replaces the newer query's results", async () => {
    const older = deferred<any[]>();
    const newer = deferred<any[]>();
    const signals: Record<string, AbortSignal> = {};
    search.mockImplementation((query: string, signal: AbortSignal) => {
      signals[query] = signal;
      return query === "attention" ? older.promise : newer.promise;
    });
    renderSearchPage();

    type("attention");
    await advance(DEBOUNCE_MS);
    type("diffusion");
    await advance(DEBOUNCE_MS);

    expect(search).toHaveBeenCalledTimes(2);
    expect(signals.attention.aborted).toBe(true);

    newer.resolve([result("Diffusion Paper")]);
    await advance(10);
    older.resolve([result("Attention Paper")]); // arrives last
    await advance(10);

    expect(screen.getByText("Diffusion Paper")).toBeInTheDocument();
    expect(screen.queryByText("Attention Paper")).not.toBeInTheDocument();
    expect(screen.getByText("1 result(s)")).toBeInTheDocument();
  });

  it("handles a rate-limited search calmly and does not retry it", async () => {
    search.mockRejectedValue(new RateLimitError());
    renderSearchPage();

    type("attention");
    await advance(DEBOUNCE_MS);
    await advance(60_000); // well past every default retry delay

    expect(search).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/search is busy right now/i)).toBeInTheDocument();
    expect(screen.queryByText("Failed to search papers.")).not.toBeInTheDocument();
    expect(screen.queryByText("No papers found.")).not.toBeInTheDocument();
    expect(input()).toBeInTheDocument();
  });

  it("keeps the existing generic error message and retries for other failures", async () => {
    search.mockRejectedValue(new Error("Collection not found"));
    renderSearchPage();

    type("attention");
    await advance(DEBOUNCE_MS);
    await advance(60_000);

    expect(search).toHaveBeenCalledTimes(4); // first attempt + React Query's 3 retries
    expect(screen.getByText("Failed to search papers.")).toBeInTheDocument();
  });
});
