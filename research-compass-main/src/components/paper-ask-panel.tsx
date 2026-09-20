import { useRef, useState } from "react";
import { AlertCircle, ArrowUp, FileText, Loader2, Sparkles } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Button } from "@/components/ui/button";
import { partitionCitations, streamAskQuestion } from "@/lib/api";
import type { Citation } from "@/lib/api";

/**
 * "Ask about this paper".
 *
 * Reuses the existing /ask-stream pipeline rather than adding a second
 * one: same embedding, same reranker, same Groq generation, and the same
 * citation-correctness mechanism. The only addition is paperId, which the
 * server verifies belongs to the caller and then ANDs onto its own
 * unconditional owner filter.
 *
 * Citations are rendered straight from the backend's `cited` flag via
 * partitionCitations. Nothing here parses the answer text or invents a
 * page number — a page shown next to a citation is the page the backend
 * returned.
 */
export function PaperAskPanel({
  paperId,
  onCitationClick,
}: {
  paperId: string;
  onCitationClick: (page: number) => void;
}) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const handleAsk = async () => {
    if (!question.trim() || busy) return;

    setBusy(true);
    setAnswer("");
    setCitations([]);
    setError("");
    setStatus("Searching this paper…");

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamAskQuestion(
        question,
        (event) => {
          if (event.type === "status") setStatus(event.text ?? "");
          if (event.type === "token") setAnswer((prev) => prev + (event.text ?? ""));
          if (event.type === "done") {
            setCitations((event.citations as Citation[]) ?? []);
            setStatus("");
          }
          if (event.type === "error") {
            // Authored by the backend — rate limits, provider trouble and
            // quota rejections all arrive here already neutral.
            setError(event.text ?? "Something went wrong.");
            setStatus("");
          }
        },
        controller.signal,
        paperId,
      );
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
      setStatus("");
      abortRef.current = null;
    }
  };

  const { cited, alsoRetrieved } = partitionCitations(citations);
  const pageOf = (c: Citation) => Number.parseInt(String(c.page), 10);

  const renderSource = (c: Citation, index: number, isCited: boolean) => {
    const page = pageOf(c);
    const canNavigate = Number.isFinite(page) && page >= 1;

    return (
      <li key={`${c.paper}-${c.page}-${index}`}>
        <button
          type="button"
          onClick={() => canNavigate && onCitationClick(page)}
          disabled={!canNavigate}
          className="w-full rounded-lg border border-border bg-surface p-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-default disabled:hover:bg-surface"
          aria-label={canNavigate ? `Go to page ${page}` : `Source on page ${c.page}`}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-1.5 text-[11px] font-mono text-muted-foreground">
              <FileText className="h-3 w-3" aria-hidden="true" />
              Page {c.page}
            </span>
            {/* not colour alone — the label carries the meaning */}
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              {isCited ? "Cited" : "Retrieved"}
            </span>
          </div>
          {c.source && (
            <div className="mt-1 truncate text-[11px] text-muted-foreground">{c.source}</div>
          )}
        </button>
      </li>
    );
  };

  return (
    <div className="flex h-full flex-col gap-4">
      <div>
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="h-4 w-4" aria-hidden="true" />
          AI Research Assistant
        </h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Answers are drawn from this paper only.
        </p>
      </div>

      <div className="rounded-xl border border-border bg-surface p-3">
        <label htmlFor="paper-question" className="sr-only">
          Ask about this paper
        </label>
        <textarea
          id="paper-question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) handleAsk();
          }}
          placeholder="Ask about this paper…"
          rows={3}
          className="w-full resize-none bg-transparent text-[14px] outline-none placeholder:text-muted-foreground"
        />
        <div className="mt-2 flex items-center justify-between">
          <span className="text-[11px] text-muted-foreground">Ctrl+Enter to ask</span>
          <Button size="sm" onClick={handleAsk} disabled={busy || !question.trim()}>
            {busy ? (
              <>
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                Thinking…
              </>
            ) : (
              <>
                <ArrowUp className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
                Ask
              </>
            )}
          </Button>
        </div>
      </div>

      {status && (
        <div className="text-xs text-muted-foreground" role="status" aria-live="polite">
          {status}
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
        >
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          {error}
        </div>
      )}

      {answer && (
        <div className="rounded-xl border border-border bg-surface p-4">
          <h3 className="sr-only">Answer</h3>
          <div
            className="prose prose-sm max-w-none text-foreground/90
              prose-headings:font-semibold prose-headings:tracking-tight prose-headings:text-foreground
              prose-p:leading-[1.7] prose-p:text-[14px]
              prose-li:text-[14px] prose-li:leading-[1.7]
              prose-strong:text-foreground prose-strong:font-semibold"
          >
            <ReactMarkdown>{answer}</ReactMarkdown>
          </div>
        </div>
      )}

      {citations.length > 0 && (
        <div>
          <h3 className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Evidence
          </h3>

          {cited.length > 0 && (
            <ul className="mt-2 space-y-2">
              {cited.map((c, i) => renderSource(c, i, true))}
            </ul>
          )}

          {alsoRetrieved.length > 0 && (
            <>
              <div className="mt-4 text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                Also retrieved, not cited
              </div>
              <ul className="mt-2 space-y-2">
                {alsoRetrieved.map((c, i) => renderSource(c, i, false))}
              </ul>
            </>
          )}
        </div>
      )}

      {!answer && !busy && !error && (
        <p className="text-xs text-muted-foreground">
          Ask a question to see an answer with the exact pages it draws on.
        </p>
      )}
    </div>
  );
}
