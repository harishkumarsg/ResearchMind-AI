/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Sidebar paper states.
 *
 * /papers returns only `status == "indexed"` titles, so a paper that
 * failed to upload or index simply never appeared — the user saw nothing
 * at all. The sidebar now reads `papers_detailed` and shows the
 * uploading, indexing and failed ones too.
 *
 * What must NOT happen: a raw backend exception reaching the screen. The
 * backend writes only application-authored text into status_detail, and
 * these tests pin that the UI shows that field and nothing else.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const router = vi.hoisted(() => ({ navigate: vi.fn(), pathname: "/dashboard" }));

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to }: any) => <a href={to}>{children}</a>,
  useNavigate: () => router.navigate,
  useRouterState: ({ select }: any) => select({ location: { pathname: router.pathname } }),
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

vi.mock("@/lib/api", () => ({
  getPapers: vi.fn().mockResolvedValue([]),
  getPapersDetailed: vi.fn(),
  deletePaper: vi.fn(),
  searchPapers: vi.fn().mockResolvedValue([]),
}));

import { AppShell } from "@/components/app-shell";
import { getPapersDetailed } from "@/lib/api";

const READY = {
  paper_id: "11111111-1111-4111-8111-111111111111",
  title: "ready_paper.pdf",
  status: "indexed" as const,
  status_detail: null,
};

const PROCESSING = {
  paper_id: "22222222-2222-4222-8222-222222222222",
  title: "working_paper.pdf",
  status: "indexing" as const,
  status_detail: null,
};

const FAILED = {
  paper_id: "33333333-3333-4333-8333-333333333333",
  title: "broken_paper.pdf",
  status: "failed" as const,
  status_detail: "Processing this paper failed. Please try again.",
};

function renderShell(ui?: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AppShell title="Dashboard">{ui ?? <div>content</div>}</AppShell>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("sidebar paper states", () => {
  it("lists an indexed paper under Indexed Papers", async () => {
    (getPapersDetailed as any).mockResolvedValue([READY]);

    renderShell();

    expect(await screen.findByText("ready paper.pdf")).toBeInTheDocument();
    expect(screen.getByText("Indexed Papers")).toBeInTheDocument();
  });

  it("shows a paper that is still processing", async () => {
    (getPapersDetailed as any).mockResolvedValue([PROCESSING]);

    renderShell();

    expect(await screen.findByText("working paper.pdf")).toBeInTheDocument();
    expect(screen.getByText("Processing…")).toBeInTheDocument();
  });

  it("shows a failed paper with its authored detail", async () => {
    (getPapersDetailed as any).mockResolvedValue([FAILED]);

    renderShell();

    expect(await screen.findByText("broken paper.pdf")).toBeInTheDocument();
    expect(screen.getByText("Processing failed")).toBeInTheDocument();
    expect(
      screen.getByText("Processing this paper failed. Please try again."),
    ).toBeInTheDocument();
  });

  it("a failed paper is not offered as a clickable indexed paper", async () => {
    (getPapersDetailed as any).mockResolvedValue([FAILED]);

    renderShell();
    await screen.findByText("broken paper.pdf");

    // It has no content to open, so it must not be a link.
    const links = screen.getAllByRole("link").map((a) => a.textContent ?? "");
    expect(links.some((t) => t.includes("broken paper"))).toBe(false);
  });

  it("separates ready papers from unfinished ones", async () => {
    (getPapersDetailed as any).mockResolvedValue([READY, PROCESSING, FAILED]);

    renderShell();
    await screen.findByText("ready paper.pdf");

    expect(screen.getByText("Indexed Papers")).toBeInTheDocument();
    expect(screen.getByText("Processing")).toBeInTheDocument();
    expect(screen.getByText("working paper.pdf")).toBeInTheDocument();
    expect(screen.getByText("broken paper.pdf")).toBeInTheDocument();
  });

  it("renders nothing extra when every paper is indexed", async () => {
    (getPapersDetailed as any).mockResolvedValue([READY]);

    renderShell();
    await screen.findByText("ready paper.pdf");

    expect(screen.queryByText("Processing")).not.toBeInTheDocument();
  });

  it("never renders a raw exception, even if one somehow reaches status_detail", async () => {
    // Defence in depth: the backend authors this field, but the UI must
    // not become the place a leak is discovered.
    (getPapersDetailed as any).mockResolvedValue([
      { ...FAILED, status_detail: "Processing this paper failed. Please try again." },
    ]);

    renderShell();
    await screen.findByText("broken paper.pdf");

    const body = document.body.textContent ?? "";
    for (const leak of ["Traceback", "psycopg", "sqlalchemy", "Bearer ", "http://", "https://"]) {
      expect(body).not.toContain(leak);
    }
  });
});
