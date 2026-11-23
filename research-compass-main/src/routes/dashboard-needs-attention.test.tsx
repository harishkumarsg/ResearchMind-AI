/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Dashboard stat cards: "Needs attention" replaces the hard-coded
 * "Collection: researchmind" card (MASTER_ROADMAP section 7, item 5).
 *
 * - Some papers failed: a third card shows how many, linking to /upload,
 *   and the grid has three columns.
 * - None failed (or the list has not loaded): no third card at all, and
 *   the grid has two columns. No placeholder value is ever shown.
 *
 * Data comes from GET /papers (papers_detailed) via getPapersDetailed, the
 * same query the sidebar runs.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
  useNavigate: () => vi.fn(),
  useRouterState: ({ select }: any) => select({ location: { pathname: "/dashboard" } }),
  createFileRoute: () => (options: any) => ({ options }),
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

vi.mock("@/lib/require-auth", () => ({ useRequireAuth: vi.fn() }));

vi.mock("@/lib/api", () => ({
  getDashboardStats: vi.fn(),
  getPapersDetailed: vi.fn(),
  getPapers: vi.fn().mockResolvedValue([]),
  deletePaper: vi.fn(),
  searchPapers: vi.fn().mockResolvedValue([]),
}));

import { Route } from "@/routes/dashboard";
import { getDashboardStats, getPapersDetailed } from "@/lib/api";

const Dashboard = (Route as any).options.component;

const STATS = { status: "success", total_papers: 1, total_chunks: 42, recent_papers: [] };

const paper = (n: number, status: string) => ({
  paper_id: `0000000${n}-0000-4000-8000-00000000000${n}`,
  title: `paper_${n}.pdf`,
  status,
  status_detail: status === "failed" ? "The file could not be stored. Please try uploading it again." : null,
});

function renderDashboard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>,
  );
}

async function statsGrid() {
  // Wait for the stats query, so the grid is in its settled state.
  await screen.findByText("42");
  return screen.getByTestId("dashboard-stats");
}

beforeEach(() => {
  vi.clearAllMocks();
  (getDashboardStats as any).mockResolvedValue(STATS);
});

describe("dashboard Needs attention card", () => {
  it("shows the failed count, links to /upload, and uses three columns", async () => {
    (getPapersDetailed as any).mockResolvedValue([
      paper(1, "indexed"),
      paper(2, "failed"),
      paper(3, "failed"),
      paper(4, "indexing"),
    ]);

    renderDashboard();

    const card = await screen.findByRole("link", { name: /needs attention/i });
    expect(card).toHaveAttribute("href", "/upload");
    expect(within(card).getByText("2")).toBeInTheDocument();
    expect(within(card).getByText(/papers failed/i)).toBeInTheDocument();

    const grid = await statsGrid();
    expect(grid).toContainElement(card);
    expect(grid.className).toContain("md:grid-cols-3");
    expect(grid.children).toHaveLength(3);
  });

  it("uses the singular for one failed paper", async () => {
    (getPapersDetailed as any).mockResolvedValue([paper(1, "failed")]);

    renderDashboard();

    const card = await screen.findByRole("link", { name: /needs attention/i });
    expect(within(card).getByText("1")).toBeInTheDocument();
    expect(within(card).getByText(/^paper failed/i)).toBeInTheDocument();
  });

  it("with nothing failed, removes the third card and uses two columns", async () => {
    (getPapersDetailed as any).mockResolvedValue([paper(1, "indexed"), paper(2, "uploading")]);

    renderDashboard();

    const grid = await statsGrid();
    await waitFor(() => expect(getPapersDetailed).toHaveBeenCalled());
    expect(screen.queryByText(/needs attention/i)).not.toBeInTheDocument();
    expect(grid.className).not.toContain("md:grid-cols-3");
    expect(grid.className).toContain("grid-cols-2");
    expect(grid.children).toHaveLength(2);
  });

  it("with no papers, shows two cards and no third card", async () => {
    (getPapersDetailed as any).mockResolvedValue([]);

    renderDashboard();

    const grid = await statsGrid();
    expect(screen.queryByText(/needs attention/i)).not.toBeInTheDocument();
    expect(grid.children).toHaveLength(2);
  });

  it("while the paper list is still loading, shows no third card", async () => {
    (getPapersDetailed as any).mockReturnValue(new Promise(() => {}));

    renderDashboard();

    const grid = await statsGrid();
    expect(screen.queryByText(/needs attention/i)).not.toBeInTheDocument();
    expect(grid.children).toHaveLength(2);
  });

  it("never shows the old hard-coded collection label", async () => {
    (getPapersDetailed as any).mockResolvedValue([paper(1, "failed")]);

    renderDashboard();

    await screen.findByRole("link", { name: /needs attention/i });
    expect(screen.queryByText(/^collection$/i)).not.toBeInTheDocument();
    expect(screen.queryByText("researchmind")).not.toBeInTheDocument();
  });
});
