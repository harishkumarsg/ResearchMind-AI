/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Phase 2A — Paper Workspace.
 *
 * The workspace pairs the owner's own PDF with an AI panel scoped to
 * that one paper. Three things matter enough to pin:
 *
 *   * the PDF arrives as an in-memory blob URL from the authenticated
 *     API — never a Storage path, never a signed URL — and is revoked
 *     when the component goes away;
 *   * the ask call carries paper_id, so retrieval is scoped server-side;
 *   * citations are rendered from the backend's `cited` flag and their
 *     page numbers drive the viewer. Nothing here invents a page.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  getPaperFileUrl: vi.fn(),
  streamAskQuestion: vi.fn(),
}));

import { PaperViewer } from "@/components/paper-viewer";
import { PaperAskPanel } from "@/components/paper-ask-panel";
import { getPaperFileUrl, streamAskQuestion } from "@/lib/api";

const PAPER_ID = "11111111-1111-4111-8111-111111111111";
const BLOB_URL = "blob:http://localhost/fake-object-url";

const CITATIONS = [
  { paper: "p.pdf", source: "p.pdf", page: 7, cited: true },
  { paper: "p.pdf", source: "p.pdf", page: 12, cited: true },
  { paper: "p.pdf", source: "p.pdf", page: 3, cited: false },
];

let revokeSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  revokeSpy = vi.fn();
  (URL as any).createObjectURL = vi.fn(() => BLOB_URL);
  (URL as any).revokeObjectURL = revokeSpy;
  (getPaperFileUrl as any).mockResolvedValue(BLOB_URL);
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderViewer(page = 1, onPageChange = vi.fn()) {
  return {
    onPageChange,
    ...render(
      (<PaperViewer paperId={PAPER_ID} page={page} onPageChange={onPageChange} />) as ReactNode,
    ),
  };
}

// ======================================================================
// Viewer
// ======================================================================
describe("PaperViewer", () => {
  it("shows a loading state before the file arrives", () => {
    (getPaperFileUrl as any).mockReturnValue(new Promise(() => {}));

    renderViewer();

    expect(screen.getByRole("status")).toHaveTextContent(/loading paper/i);
  });

  it("renders the PDF from an authenticated blob URL, not a storage path", async () => {
    renderViewer();

    const frame = await screen.findByTitle("Paper document");
    expect(frame.getAttribute("src")).toContain("blob:");
    expect(frame.getAttribute("src")).not.toContain("supabase");
    expect(frame.getAttribute("src")).not.toContain("/storage/");
    expect(getPaperFileUrl).toHaveBeenCalledWith(PAPER_ID);
  });

  it("points the viewer at the requested page", async () => {
    renderViewer(7);

    const frame = await screen.findByTitle("Paper document");
    expect(frame.getAttribute("src")).toBe(`${BLOB_URL}#page=7`);
  });

  it("revokes the object URL on unmount so the document is not retained", async () => {
    const { unmount } = renderViewer();
    await screen.findByTitle("Paper document");

    unmount();

    expect(revokeSpy).toHaveBeenCalledWith(BLOB_URL);
  });

  it("degrades to a calm message when the file is unavailable", async () => {
    (getPaperFileUrl as any).mockRejectedValue(new Error("Paper not found."));

    renderViewer();

    expect(await screen.findByText("Paper not found.")).toBeInTheDocument();
    // The AI panel still works without the file — say so.
    expect(screen.getByText(/still ask questions/i)).toBeInTheDocument();
  });

  it("offers keyboard-accessible page controls", async () => {
    const { onPageChange } = renderViewer(5);
    await screen.findByTitle("Paper document");

    fireEvent.click(screen.getByRole("button", { name: /next page/i }));
    expect(onPageChange).toHaveBeenCalledWith(6);

    fireEvent.click(screen.getByRole("button", { name: /previous page/i }));
    expect(onPageChange).toHaveBeenCalledWith(4);
  });

  it("does not let the page go below one", async () => {
    const { onPageChange } = renderViewer(1);
    await screen.findByTitle("Paper document");

    expect(screen.getByRole("button", { name: /previous page/i })).toBeDisabled();
    expect(onPageChange).not.toHaveBeenCalled();
  });
});

// ======================================================================
// Ask panel
// ======================================================================
function renderPanel(onCitationClick = vi.fn()) {
  return {
    onCitationClick,
    ...render(
      (<PaperAskPanel paperId={PAPER_ID} onCitationClick={onCitationClick} />) as ReactNode,
    ),
  };
}

function ask(question = "what is the method?") {
  fireEvent.change(screen.getByLabelText(/ask about this paper/i), {
    target: { value: question },
  });
  fireEvent.click(screen.getByRole("button", { name: /^ask$/i }));
}

describe("PaperAskPanel", () => {
  it("scopes the question to this paper", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "token", text: "An answer." });
      onEvent({ type: "done", citations: CITATIONS });
    });

    renderPanel();
    ask();

    await waitFor(() => expect(streamAskQuestion).toHaveBeenCalled());
    // 4th argument is paperId — the server ANDs it onto its own owner filter.
    expect((streamAskQuestion as any).mock.calls[0][3]).toBe(PAPER_ID);
  });

  it("renders the answer and its cited pages", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "token", text: "The method is described." });
      onEvent({ type: "done", citations: CITATIONS });
    });

    renderPanel();
    ask();

    expect(await screen.findByText("The method is described.")).toBeInTheDocument();
    expect(screen.getByText("Page 7")).toBeInTheDocument();
    expect(screen.getByText("Page 12")).toBeInTheDocument();
  });

  it("separates cited evidence from what was merely retrieved", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "done", citations: CITATIONS });
    });

    renderPanel();
    ask();

    expect(await screen.findByText(/also retrieved, not cited/i)).toBeInTheDocument();
    expect(screen.getByText("Page 3")).toBeInTheDocument();
  });

  it("navigates the viewer to a citation's page", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "done", citations: CITATIONS });
    });

    const { onCitationClick } = renderPanel();
    ask();

    fireEvent.click(await screen.findByRole("button", { name: /go to page 7/i }));
    expect(onCitationClick).toHaveBeenCalledWith(7);
  });

  it("shows a backend error without inventing wording", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({
        type: "error",
        code: "rate_limited",
        text: "Too many requests. Please wait about 30 seconds and try again.",
      });
    });

    renderPanel();
    ask();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/too many requests/i);
  });

  it("surfaces a generating state while the answer streams", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "status", text: "Searching this paper…" });
      await new Promise((r) => setTimeout(r, 20));
    });

    renderPanel();
    ask();

    expect(await screen.findByText("Searching this paper…")).toBeInTheDocument();
  });

  it("shows an empty state before anything is asked", () => {
    renderPanel();

    expect(screen.getByText(/ask a question to see an answer/i)).toBeInTheDocument();
  });

  it("does not call the backend for an empty question", () => {
    renderPanel();

    expect(screen.getByRole("button", { name: /^ask$/i })).toBeDisabled();
    expect(streamAskQuestion).not.toHaveBeenCalled();
  });

  it("labels evidence with text, not colour alone", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "done", citations: CITATIONS });
    });

    renderPanel();
    ask();

    expect((await screen.findAllByText("Cited")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Retrieved").length).toBeGreaterThan(0);
  });
});
