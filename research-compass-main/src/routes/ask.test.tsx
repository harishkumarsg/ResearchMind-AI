/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Ask page — how a streamed answer that fails partway is shown.
 *
 * Answer text that has already arrived is kept, and the error frame that
 * ended the stream is shown beneath it, so the reader sees both what was
 * written and why it stopped.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (options: any) => ({ options }),
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

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  streamAskQuestion: vi.fn(),
}));

import { Route } from "@/routes/ask";
import { streamAskQuestion } from "@/lib/api";

const AskPage = (Route as any).options.component;

const INCOMPLETE = "The answer could not be completed. Please try again.";

function askQuestion() {
  render(<AskPage />);
  fireEvent.change(screen.getByPlaceholderText(/ask a question/i), {
    target: { value: "What is the method?" },
  });
  fireEvent.click(screen.getByRole("button", { name: /ask/i }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Ask page — a stream that fails after answer text arrived", () => {
  it("keeps the partial answer visible, with the error beneath it", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "status", text: "Generating answer…" });
      onEvent({ type: "token", text: "The method combines " });
      onEvent({ type: "token", text: "two encoders" });
      onEvent({ type: "error", code: "generation_incomplete", text: INCOMPLETE });
    });

    askQuestion();

    const error = await screen.findByText(INCOMPLETE);
    const partial = screen.getByText("The method combines two encoders");
    expect(partial.compareDocumentPosition(error) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The stream is over: no spinner, and the Ask button is usable again.
    expect(screen.queryByText("Generating answer…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ask/i })).toBeEnabled();
  });

  it("shows an error with no answer when nothing arrived first", async () => {
    (streamAskQuestion as any).mockImplementation(async (_q: string, onEvent: any) => {
      onEvent({ type: "error", code: "provider_timeout", text: "Took too long." });
    });

    askQuestion();

    expect(await screen.findByText("Took too long.")).toBeInTheDocument();
    expect(screen.queryByText("ResearchMind Answer")).not.toBeInTheDocument();
  });
});
