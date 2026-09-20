/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Reports page — restoring a stored report on mount.
 *
 * The report used to live only in component state, so Ctrl+R returned the
 * page to an empty generation form while the row sat in Postgres. These
 * tests pin the restore, and pin the thing that must NOT happen to
 * achieve it: generateReport must never be called on mount, or every
 * refresh would bill a generation and insert a duplicate row.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@tanstack/react-router", () => ({
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

vi.mock("@/components/app-shell", () => ({
  AppShell: ({ children, actions }: any) => (
    <div>
      <div data-testid="actions">{actions}</div>
      {children}
    </div>
  ),
}));

vi.mock("@/lib/api", () => ({
  generateReport: vi.fn(),
  exportReport: vi.fn().mockResolvedValue(undefined),
  getLatestReport: vi.fn(),
}));

import { Route } from "@/routes/reports";
import { generateReport, exportReport, getLatestReport } from "@/lib/api";

const ReportsPage = (Route as any).options.component;

const SAVED = {
  status: "success",
  query: "what is the unique novelty",
  report: "### Findings\n\nThe stored report body.",
  citations: [{ paper: "a.pdf", source: "a.pdf", page: 3 }],
  sources_used: 1,
  chunks_used: null,
};

const FRESH = {
  status: "success",
  query: "a brand new topic",
  report: "### Findings\n\nA freshly generated body.",
  citations: [{ paper: "b.pdf", source: "b.pdf", page: 1 }],
  sources_used: 1,
  chunks_used: 7,
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReportsPage />
    </QueryClientProvider> as ReactNode,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Reports page restoration", () => {
  it("renders a stored report on mount", async () => {
    (getLatestReport as any).mockResolvedValue(SAVED);

    renderPage();

    expect(await screen.findByText("what is the unique novelty")).toBeInTheDocument();
    expect(await screen.findByText(/The stored report body/)).toBeInTheDocument();
  });

  it("keeps the empty generation form when nothing is stored", async () => {
    (getLatestReport as any).mockResolvedValue(null);

    renderPage();

    await waitFor(() => expect(getLatestReport).toHaveBeenCalled());

    expect(
      screen.getByPlaceholderText("Enter your research topic or question…"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/stored report body/)).not.toBeInTheDocument();
  });

  it("never generates on mount", async () => {
    (getLatestReport as any).mockResolvedValue(SAVED);

    renderPage();

    await screen.findByText("what is the unique novelty");

    // The whole point of the separate read endpoint: restoring a page
    // must not bill a generation or insert a duplicate row.
    expect(generateReport).not.toHaveBeenCalled();
  });

  it("offers Export PDF for a restored report", async () => {
    (getLatestReport as any).mockResolvedValue(SAVED);

    renderPage();

    const button = await screen.findByRole("button", { name: /export pdf/i });
    fireEvent.click(button);

    await waitFor(() => expect(exportReport).toHaveBeenCalledTimes(1));
  });

  it("omits the chunk count for a restored report but keeps the source count", async () => {
    (getLatestReport as any).mockResolvedValue(SAVED);

    renderPage();

    await screen.findByText("what is the unique novelty");

    expect(screen.getByText(/1 sources/)).toBeInTheDocument();
    expect(screen.queryByText(/chunks/)).not.toBeInTheDocument();
  });
});

describe("Reports page generation still works", () => {
  it("generates a report and shows it", async () => {
    (getLatestReport as any).mockResolvedValue(null);
    (generateReport as any).mockResolvedValue(FRESH);

    renderPage();

    await waitFor(() => expect(getLatestReport).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText("Enter your research topic or question…"), {
      target: { value: "a brand new topic" },
    });
    fireEvent.click(screen.getByRole("button", { name: /generate report/i }));

    expect(await screen.findByText(/A freshly generated body/)).toBeInTheDocument();
    expect(generateReport).toHaveBeenCalledWith("a brand new topic");
    expect(screen.getByText(/7 chunks/)).toBeInTheDocument();
  });

  it("a freshly generated report replaces the restored one", async () => {
    (getLatestReport as any).mockResolvedValue(SAVED);
    (generateReport as any).mockResolvedValue(FRESH);

    renderPage();
    await screen.findByText("what is the unique novelty");

    fireEvent.change(screen.getByPlaceholderText("Enter your research topic or question…"), {
      target: { value: "a brand new topic" },
    });
    fireEvent.click(screen.getByRole("button", { name: /generate report/i }));

    expect(await screen.findByText(/A freshly generated body/)).toBeInTheDocument();
    expect(screen.queryByText(/The stored report body/)).not.toBeInTheDocument();
  });

  it("surfaces a generation error without losing the form", async () => {
    (getLatestReport as any).mockResolvedValue(null);
    (generateReport as any).mockRejectedValue(new Error("Report generation failed."));

    renderPage();
    await waitFor(() => expect(getLatestReport).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText("Enter your research topic or question…"), {
      target: { value: "a topic" },
    });
    fireEvent.click(screen.getByRole("button", { name: /generate report/i }));

    expect(await screen.findByText("Report generation failed.")).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText("Enter your research topic or question…"),
    ).toBeInTheDocument();
  });
});
