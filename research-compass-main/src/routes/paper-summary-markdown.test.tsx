/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Paper detail page — the AI Summary is Markdown, and must render as such.
 *
 * The backend returns "### Title", "- item", "1. item" and "**bold**".
 * The page previously put that straight into a <p> with
 * whitespace-pre-wrap, so production showed the syntax literally. These
 * tests pin the rendered structure — headings, bullets, numbers, bold —
 * and pin that the raw markers are gone, without asserting anything
 * about the summary's wording, which is the backend's business.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (options: any) => ({
    options,
    useParams: () => ({ paperName: "ETASR_18859.pdf" }),
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

vi.mock("@/components/app-shell", () => ({
  AppShell: ({ children }: any) => <div>{children}</div>,
}));

vi.mock("@/lib/api", () => ({
  getPaperDetails: vi.fn(),
  summarizePaper: vi.fn(),
  deletePaper: vi.fn(),
  retryUnlessRateLimited: () => false,
  // The page now also renders the Phase 2A workspace (viewer + ask
  // panel). This suite is about Summary markdown, so both are stubbed
  // to inert values rather than exercised here.
  getPaperFileUrl: vi.fn().mockRejectedValue(new Error("not used in this suite")),
  streamAskQuestion: vi.fn(),
  partitionCitations: (c: any[]) => ({
    cited: c.filter((x) => x.cited),
    alsoRetrieved: c.filter((x) => !x.cited),
  }),
}));

import { Route } from "@/routes/paper.$paperName";
import { getPaperDetails, summarizePaper } from "@/lib/api";

const PaperDetailsPage = (Route as any).options.component;

const DETAILS = {
  status: "success",
  paper: "ETASR_18859.pdf",
  paper_id: "11111111-1111-4111-8111-111111111111",
  source: "ETASR_18859.pdf",
  authors: "A. Author",
  keywords: "vision-language, AOI",
  abstract: "An abstract about VLM-AOI inspection.",
  total_chunks: 55,
  preview: "Preview text.",
};

//: Exercises every construct the summary prompt can emit.
const SUMMARY_MARKDOWN = [
  "### Title",
  "",
  "VLM-AOI: a two-stage hybrid framework.",
  "",
  "### Key Findings",
  "",
  "- Unsupervised localization",
  "- Zero-shot classification",
  "",
  "### Methodology",
  "",
  "1. Localize defects",
  "2. Classify with a VLM",
  "",
  "The framework is **effective** and *scalable*.",
].join("\n");

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <PaperDetailsPage />
    </QueryClientProvider> as ReactNode,
  );
}

async function generateSummary() {
  renderPage();

  const button = await screen.findByRole("button", { name: /generate ai summary/i });
  fireEvent.click(button);

  await screen.findByText("VLM-AOI: a two-stage hybrid framework.");
}

beforeEach(() => {
  vi.clearAllMocks();
  (getPaperDetails as any).mockResolvedValue(DETAILS);
  (summarizePaper as any).mockResolvedValue({ status: "success", summary: SUMMARY_MARKDOWN });
});

describe("AI Summary markdown rendering", () => {
  it("renders headings as headings, not as hashes", async () => {
    await generateSummary();

    expect(screen.getByRole("heading", { name: "Title" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Key Findings" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Methodology" })).toBeInTheDocument();
  });

  it("renders unordered list items as list items", async () => {
    await generateSummary();

    const bullet = screen.getByText("Unsupervised localization");
    expect(bullet.closest("li")).not.toBeNull();
    expect(bullet.closest("ul")).not.toBeNull();
  });

  it("renders ordered list items inside an ordered list", async () => {
    await generateSummary();

    const step = screen.getByText("Localize defects");
    expect(step.closest("li")).not.toBeNull();
    expect(step.closest("ol")).not.toBeNull();
  });

  it("renders bold and italic markup", async () => {
    await generateSummary();

    expect(screen.getByText("effective").tagName.toLowerCase()).toBe("strong");
    expect(screen.getByText("scalable").tagName.toLowerCase()).toBe("em");
  });

  it("shows no raw markdown syntax", async () => {
    await generateSummary();

    const body = document.body.textContent ?? "";
    expect(body).not.toContain("### ");
    expect(body).not.toContain("**effective**");
    expect(body).not.toContain("- Unsupervised localization");
  });

  it("preserves the summary content exactly", async () => {
    await generateSummary();

    // Same words the backend produced; only the presentation changed.
    for (const fragment of [
      "VLM-AOI: a two-stage hybrid framework.",
      "Unsupervised localization",
      "Zero-shot classification",
      "Localize defects",
      "Classify with a VLM",
    ]) {
      expect(screen.getByText(fragment, { exact: false })).toBeInTheDocument();
    }
  });

  it("still calls the summary endpoint exactly once per click", async () => {
    await generateSummary();

    expect(summarizePaper).toHaveBeenCalledTimes(1);
    expect(summarizePaper).toHaveBeenCalledWith("ETASR_18859.pdf");
  });

  it("surfaces a summary error as plain text, not markdown", async () => {
    (summarizePaper as any).mockRejectedValue(new Error("Summarization failed."));

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /generate ai summary/i }));

    expect(await screen.findByText("Summarization failed.")).toBeInTheDocument();
  });

  it("does not render a summary block before one is generated", async () => {
    renderPage();

    await screen.findByRole("button", { name: /generate ai summary/i });

    await waitFor(() => expect(getPaperDetails).toHaveBeenCalled());
    expect(screen.queryByRole("heading", { name: "Key Findings" })).not.toBeInTheDocument();
  });
});
