import { createFileRoute } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { AlertCircle, ArrowUp, FileText, Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useRef, useState } from "react";
import { API_BASE_URL } from "@/lib/api";

export const Route = createFileRoute("/ask")({
  head: () => ({ meta: [{ title: "Ask AI · ResearchMind" }] }),
  component: AskPage,
});

interface Citation {
  paper: string;
  source: string;
  page: number | string;
}

type Stage = "idle" | "searching" | "reranking" | "generating" | "done" | "error";

function AskPage() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [stage, setStage] = useState<Stage>("idle");
  const [statusText, setStatusText] = useState("");
  const [error, setError] = useState("");
  const esRef = useRef<EventSource | null>(null);

  const stageLabel: Record<Stage, string> = {
    idle: "",
    searching: "Searching papers…",
    reranking: "Reranking results…",
    generating: "Generating answer…",
    done: "",
    error: "",
  };

  const askAI = () => {
    if (!question.trim()) return;

    // Reset state
    setAnswer("");
    setCitations([]);
    setError("");
    setStage("searching");

    // Close any existing stream
    esRef.current?.close();

    const url = `${API_BASE_URL}/ask-stream?question=${encodeURIComponent(question)}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);

        if (msg.type === "status") {
          setStatusText(msg.text);
          if (msg.text.includes("Searching")) setStage("searching");
          else if (msg.text.includes("Reranking")) setStage("reranking");
          else if (msg.text.includes("Generating")) setStage("generating");
        } else if (msg.type === "token") {
          setAnswer((prev) => prev + msg.text);
        } else if (msg.type === "done") {
          setCitations(msg.citations || []);
          setStage("done");
          es.close();
        } else if (msg.type === "error") {
          setError(msg.text);
          setStage("error");
          es.close();
        }
      } catch {
        // ignore parse errors on empty keep-alive lines
      }
    };

    es.onerror = () => {
      setError("Connection to backend lost. Is the server running?");
      setStage("error");
      es.close();
    };
  };

  const isStreaming = stage === "searching" || stage === "reranking" || stage === "generating";

  return (
    <AppShell
      title="Ask AI"
      subtitle="Grounded answers from your indexed research papers"
    >
      <div className="mx-auto max-w-3xl space-y-6">

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
            <Button onClick={askAI} disabled={isStreaming || !question.trim()}>
              {isStreaming ? (
                <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> {stage === "generating" ? "Generating…" : stage === "reranking" ? "Reranking…" : "Searching…"}</>
              ) : (
                <><ArrowUp className="mr-2 h-4 w-4" /> Ask</>
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
            {error}
          </div>
        )}

        {/* Streaming answer */}
        {answer && (
          <div className="space-y-4">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Sparkles className="h-4 w-4" />
              ResearchMind Answer
              {isStreaming && <span className="inline-block h-4 w-0.5 animate-pulse bg-primary ml-0.5" />}
            </div>
            <div className="rounded-xl border border-border bg-surface p-6">
              <p className="whitespace-pre-wrap leading-[1.75] text-[15px] text-foreground/90">
                {answer}
              </p>
            </div>
          </div>
        )}

        {/* Citations — shown after done */}
        {citations.length > 0 && stage === "done" && (
          <div className="space-y-3">
            <div className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
              Sources
            </div>
            {citations.map((c, i) => (
              <div
                key={i}
                className="flex items-start gap-3 rounded-xl border border-border bg-surface p-4"
              >
                <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <div>
                  <div className="text-sm font-medium leading-snug">
                    {c.paper.replace(/_/g, " ")}
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    Page {c.page}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

      </div>
    </AppShell>
  );
}
