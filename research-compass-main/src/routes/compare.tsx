import { createFileRoute } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { AlertCircle, ArrowUp, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { comparePapers, getPapers } from "@/lib/api";
import type { CompareResult } from "@/lib/api";

export const Route = createFileRoute("/compare")({
  head: () => ({ meta: [{ title: "Compare · ResearchMind" }] }),
  component: ComparePage,
});

// Strip .pdf extension if the user typed or pasted a full filename
function normalizePaperName(name: string): string {
  return name.trim().replace(/\.pdf$/i, "");
}

// Parse a markdown table string into a header row + body rows.
function parseMarkdownTable(
  md: string
): { headers: string[]; rows: string[][] } {
  const lines = md
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.startsWith("|"));

  const parseRow = (line: string) =>
    line
      .split("|")
      .slice(1, -1)
      .map((c) => c.trim());

  const dataLines = lines.filter(
    (l) => !/^\|[-| :]+\|$/.test(l)
  );

  if (dataLines.length === 0) return { headers: [], rows: [] };

  const [headerLine, ...bodyLines] = dataLines;
  return {
    headers: parseRow(headerLine),
    rows: bodyLines.map(parseRow),
  };
}

function ComparePage() {
  const [paper1, setPaper1] = useState("");
  const [paper2, setPaper2] = useState("");
  const [result, setResult] = useState<CompareResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const { data: papers = [] } = useQuery({
    queryKey: ["papers"],
    queryFn: getPapers,
  });

  const handleCompare = async () => {
    const p1 = normalizePaperName(paper1);
    const p2 = normalizePaperName(paper2);
    if (!p1 || !p2) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const data = await comparePapers(p1, p2);
      setResult(data);
    } catch (err: any) {
      setError(err.message || "Comparison failed.");
    } finally {
      setLoading(false);
    }
  };

  const table = result ? parseMarkdownTable(result.comparison) : null;

  return (
    <AppShell
      title="Compare studies"
      subtitle="Side-by-side comparison of two indexed papers"
    >
      {/* Input Form */}
      <div className="mx-auto max-w-2xl space-y-4">
        {papers.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {papers.length} paper{papers.length !== 1 ? "s" : ""} indexed — select from the list or type a name
          </p>
        )}

        {/* Hidden datalists for autocomplete */}
        <datalist id="papers-list">
          {papers.map((p) => (
            <option key={p} value={p} />
          ))}
        </datalist>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
              Paper 1
            </label>
            <input
              value={paper1}
              onChange={(e) => setPaper1(e.target.value)}
              list="papers-list"
              placeholder="Select or type a paper name…"
              className="w-full rounded-xl border border-border bg-surface px-4 py-2.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary/50"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
              Paper 2
            </label>
            <input
              value={paper2}
              onChange={(e) => setPaper2(e.target.value)}
              list="papers-list"
              placeholder="Select or type a paper name…"
              className="w-full rounded-xl border border-border bg-surface px-4 py-2.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary/50"
            />
          </div>
        </div>

        <Button
          onClick={handleCompare}
          disabled={loading || !paper1.trim() || !paper2.trim()}
        >
          {loading ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Comparing…
            </>
          ) : (
            <>
              <ArrowUp className="mr-2 h-4 w-4" />
              Compare Papers
            </>
          )}
        </Button>
      </div>

      {/* Loading */}
      {loading && (
        <div className="mt-10 flex flex-col items-center gap-3 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
          <span className="text-sm">
            Retrieving paper contexts and generating comparison…
          </span>
          <span className="text-xs">This may take ~25 seconds on CPU</span>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="mx-auto mt-6 max-w-2xl flex items-center gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Comparison Table */}
      {result && !loading && table && table.headers.length > 0 && (
        <div className="mt-8 overflow-hidden rounded-2xl border border-border bg-surface">
          {/* Header */}
          <div
            className="grid border-b border-border bg-muted/30"
            style={{
              gridTemplateColumns: `160px repeat(${table.headers.length - 1}, 1fr)`,
            }}
          >
            {table.headers.map((h, i) => (
              <div
                key={i}
                className={`px-5 py-4 ${i > 0 ? "border-l border-border" : ""}`}
              >
                {i === 0 ? (
                  <span className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                    {h}
                  </span>
                ) : (
                  <div>
                    <div
                      className="truncate text-[10px] font-mono uppercase tracking-wider text-muted-foreground"
                      title={i === 1 ? result.paper1 : result.paper2}
                    >
                      {i === 1 ? result.paper1 : result.paper2}
                    </div>
                    <div className="mt-1 text-[13.5px] font-medium leading-snug tracking-tight">
                      {h}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Body */}
          {table.rows.map((row, ri) => (
            <div
              key={ri}
              className="grid border-b border-border last:border-b-0"
              style={{
                gridTemplateColumns: `160px repeat(${table.headers.length - 1}, 1fr)`,
              }}
            >
              {row.map((cell, ci) => (
                <div
                  key={ci}
                  className={`px-5 py-5 ${ci > 0 ? "border-l border-border" : ""}`}
                >
                  {ci === 0 ? (
                    <span className="text-[12px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      {cell}
                    </span>
                  ) : (
                    <span className="text-[14px] leading-relaxed text-foreground/85">
                      {cell}
                    </span>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {/* Fallback: raw text if not a table */}
      {result && !loading && (!table || table.headers.length === 0) && (
        <div className="mt-8 rounded-2xl border border-border bg-surface p-8">
          <pre className="whitespace-pre-wrap text-[14px] leading-relaxed text-foreground/85">
            {result.comparison}
          </pre>
        </div>
      )}
    </AppShell>
  );
}
