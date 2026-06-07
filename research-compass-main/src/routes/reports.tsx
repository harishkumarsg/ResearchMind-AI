import { createFileRoute } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { AlertCircle, ArrowUp, Download, FileText, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { generateReport, exportReport } from "@/lib/api";
import type { ResearchResult } from "@/lib/api";

export const Route = createFileRoute("/reports")({
  head: () => ({ meta: [{ title: "Reports · ResearchMind" }] }),
  component: ReportsPage,
});

function ReportsPage() {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<ResearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");

  const handleGenerate = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const data = await generateReport(query);
      setResult(data);
    } catch (err: any) {
      setError(err.message || "Report generation failed.");
    } finally {
      setLoading(false);
    }
  };

  const handleExport = async () => {
    setExporting(true);
    try {
      await exportReport();
    } catch (err: any) {
      setError(err.message || "Export failed.");
    } finally {
      setExporting(false);
    }
  };

  return (
    <AppShell
      title="Literature Review"
      subtitle="Generate a research report from your indexed papers"
      actions={
        result && (
          <Button size="sm" onClick={handleExport} disabled={exporting}>
            {exporting ? (
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download className="mr-1.5 h-3.5 w-3.5" />
            )}
            Export PDF
          </Button>
        )
      }
    >
      {/* Topic Input */}
      <div className="mx-auto max-w-2xl">
        <div className="rounded-xl border border-border bg-surface p-4">
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey))
                handleGenerate();
            }}
            placeholder="Enter your research topic or question…"
            className="w-full resize-none bg-transparent text-[15px] outline-none min-h-[80px] placeholder:text-muted-foreground"
          />
          <div className="mt-3 flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              Ctrl+Enter to generate
            </span>
            <Button
              onClick={handleGenerate}
              disabled={loading || !query.trim()}
              size="sm"
            >
              {loading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Generating…
                </>
              ) : (
                <>
                  <ArrowUp className="mr-2 h-4 w-4" />
                  Generate Report
                </>
              )}
            </Button>
          </div>
        </div>
      </div>

      {/* Loading */}
      {loading && (

        <div className="mt-10 flex flex-col items-center gap-3 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
          <span className="text-sm">
            Searching papers, reranking, generating report…
          </span>
          <span className="text-xs">This may take ~25 seconds on CPU</span>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="mt-6 flex items-center gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Report */}
      {result && !loading && (
        <div className="mt-8 grid gap-8 lg:grid-cols-[1fr_280px]">
          <article className="rounded-2xl border border-border bg-surface p-10">
            <div className="text-[11px] font-mono uppercase tracking-[0.18em] text-muted-foreground">
              Literature Review
            </div>
            <h1 className="mt-3 text-2xl font-medium leading-snug tracking-tight">
              {result.query}
            </h1>
            <div className="mt-2 text-xs text-muted-foreground">
              {result.sources_used} sources · {result.chunks_used} chunks
            </div>
            <div className="mt-8 prose prose-sm max-w-none text-foreground/90
              prose-headings:font-semibold prose-headings:tracking-tight prose-headings:text-foreground
              prose-h3:text-lg prose-h4:text-base
              prose-p:leading-[1.75] prose-p:text-[15px]
              prose-li:text-[15px] prose-li:leading-[1.75]
              prose-strong:text-foreground prose-strong:font-semibold
              prose-ul:my-3 prose-ol:my-3">
              <ReactMarkdown>{result.report}</ReactMarkdown>
            </div>
          </article>

          <aside>
            <div className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
              Citations
            </div>
            <div className="mt-3 space-y-2.5">
              {result.citations.map((c, i) => (
                <div
                  key={i}
                  className="rounded-lg border border-border bg-surface p-3"
                >
                  <div className="flex items-center justify-between text-[10px] font-mono text-muted-foreground">
                    <span>[{i + 1}]</span>
                    <FileText className="h-3 w-3" />
                  </div>
                  <div className="mt-1 text-[12.5px] font-medium leading-snug">
                    {c.paper}
                  </div>
                  <div className="text-[10px] text-muted-foreground">
                    {c.source} · Page {c.page}
                  </div>
                </div>
              ))}
            </div>
          </aside>
        </div>
      )}
    </AppShell>
  );
}

