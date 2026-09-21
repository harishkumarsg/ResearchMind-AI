import { createFileRoute } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { AlertCircle, ArrowUp, FileText, Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useRef, useState } from "react";
import {
  streamAskQuestion,
  partitionCitations,
  AuthenticationRequiredError,
  type AskStreamEvent,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useRequireAuth } from "@/lib/require-auth";

export const Route = createFileRoute("/ask")({
  head: () => ({ meta: [{ title: "Ask AI · ResearchMind" }] }),
  component: AskPage,
});

interface Citation {
  paper: string;
  source: string;
  page: number | string;
  cited?: boolean;
}

type Stage = "idle" | "searching" | "reranking" | "generating" | "done" | "error";

function AskPage() {
  // Signed-out visitors are sent to the public landing page.
  useRequireAuth();

  const { session, isLoading: authLoading, signInWithGoogle } = useAuth();
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [stage, setStage] = useState<Stage>("idle");
  const [statusText, setStatusText] = useState("");
  const [error, setError] = useState("");
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const stageLabel: Record<Stage, string> = {
    idle: "",
    searching: "Searching papers…",
    reranking: "Reranking results…",
    generating: "Generating answer…",
    done: "",
    error: "",
  };

  const askAI = async () => {
    if (!question.trim()) return;

    // Reset state
    setAnswer("");
    setCitations([]);
    setError("");
    setNeedsSignIn(false);
    setStage("searching");

    // Cancel any in-flight stream before starting a new one — this is the
    // fetch()-based reader's equivalent of the previous
    // `esRef.current?.close()` on the raw EventSource.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const handleEvent = (msg: AskStreamEvent) => {
      if (msg.type === "status") {
        setStatusText(msg.text ?? "");
        if (msg.text?.includes("Searching")) setStage("searching");
        else if (msg.text?.includes("Reranking")) setStage("reranking");
        else if (msg.text?.includes("Generating")) setStage("generating");
      } else if (msg.type === "token") {
        setAnswer((prev) => prev + (msg.text ?? ""));
      } else if (msg.type === "done") {
        setCitations((msg.citations as Citation[]) ?? []);
        setStage("done");
      } else if (msg.type === "error") {
        setError(msg.text ?? "Something went wrong.");
        setStage("error");
      }
    };

    try {
      // Authorization is sent as a normal fetch() header here — never a
      // query-string token — which is exactly why this replaced the
      // previous raw EventSource (which cannot set custom headers at all).
      await streamAskQuestion(question, handleEvent, controller.signal);
    } catch (err) {
      if (controller.signal.aborted) return; // superseded by a newer question
      if (err instanceof AuthenticationRequiredError) {
        setNeedsSignIn(true);
        setError(err.message);
      } else {
        setError(
          err instanceof Error ? err.message : "Connection to backend lost. Is the server running?",
        );
      }
      setStage("error");
    }
  };

  const isStreaming = stage === "searching" || stage === "reranking" || stage === "generating";
  const { cited: citedSources, alsoRetrieved } = partitionCitations(citations);

  return (
    <AppShell title="Ask AI" subtitle="Grounded answers from your indexed research papers">
      <div className="mx-auto max-w-3xl space-y-6">
        {!authLoading && !session && (
          <div className="flex items-center justify-between gap-3 rounded-xl border border-border bg-surface px-4 py-3 text-sm text-muted-foreground">
            <span>Sign in to ask questions about your papers.</span>
            <Button size="sm" onClick={() => signInWithGoogle()}>
              Sign in with Google
            </Button>
          </div>
        )}

        {/* Input box */}
        <div className="rounded-xl border border-border bg-surface p-4">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) askAI();
            }}
            placeholder="Ask a question about your indexed papers…"
            className="w-full resize-none bg-transparent text-[15px] outline-none min-h-[100px] placeholder:text-muted-foreground"
          />
          <div className="mt-3 flex items-center justify-between">
            <span className="text-xs text-muted-foreground">Ctrl+Enter to ask</span>
            <Button onClick={askAI} disabled={isStreaming || !question.trim() || !session}>
              {isStreaming ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />{" "}
                  {stage === "generating"
                    ? "Generating…"
                    : stage === "reranking"
                      ? "Reranking…"
                      : "Searching…"}
                </>
              ) : (
                <>
                  <ArrowUp className="mr-2 h-4 w-4" /> Ask
                </>
              )}
            </Button>
          </div>
        </div>

        {/* Status bar during stream */}
        {isStreaming && (
          <div className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin shrink-0" />
            {statusText || stageLabel[stage]}
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="flex items-center gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" />
            <span className="flex-1">{error}</span>
            {needsSignIn && (
              <Button size="sm" variant="outline" onClick={() => signInWithGoogle()}>
                Sign in
              </Button>
            )}
          </div>
        )}

        {/* Streaming answer */}
        {answer && (
          <div className="space-y-4">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Sparkles className="h-4 w-4" />
              ResearchMind Answer
              {isStreaming && (
                <span className="inline-block h-4 w-0.5 animate-pulse bg-primary ml-0.5" />
              )}
            </div>
            <div className="rounded-xl border border-border bg-surface p-6">
              <p className="whitespace-pre-wrap leading-[1.75] text-[15px] text-foreground/90">
                {answer}
              </p>
            </div>
          </div>
        )}

        {/* Evidence — shown after done. Split on the backend's `cited`
            flag: passages the answer actually cited vs. passages that were
            retrieved and supplied to the model but went unused. Both are
            disclosed; only the labelling distinguishes them. */}
        {stage === "done" && citedSources.length > 0 && (
          <div className="space-y-3">
            <div className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
              Sources
            </div>
            {citedSources.map((c, i) => (
              <div
                key={i}
                className="flex items-start gap-3 rounded-xl border border-border bg-surface p-4"
              >
                <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <div>
                  <div className="text-sm font-medium leading-snug">
                    {c.paper.replace(/_/g, " ")}
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">Page {c.page}</div>
                </div>
              </div>
            ))}
          </div>
        )}

        {stage === "done" && alsoRetrieved.length > 0 && (
          <div className="space-y-3 opacity-60">
            <div className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
              Also retrieved · not cited in this answer
            </div>
            {alsoRetrieved.map((c, i) => (
              <div
                key={i}
                className="flex items-start gap-3 rounded-xl border border-dashed border-border bg-surface p-4"
              >
                <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <div>
                  <div className="text-sm font-medium leading-snug">
                    {c.paper.replace(/_/g, " ")}
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">Page {c.page}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </AppShell>
  );
}
