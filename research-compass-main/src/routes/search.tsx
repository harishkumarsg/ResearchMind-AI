import { createFileRoute } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import {
  Bookmark,
  FileText,
  Search as SearchIcon,
  Loader2,
} from "lucide-react";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { searchPapers } from "@/lib/api";

export const Route = createFileRoute("/search")({
  head: () => ({
    meta: [{ title: "Library · ResearchMind" }],
  }),
  component: SearchPage,
});

function SearchPage() {
  const [query, setQuery] = useState("");

  const {
    data: results = [],
    isLoading,
    error,
  } = useQuery({
    queryKey: ["papers", query],
    queryFn: () => searchPapers(query),
    enabled: query.length > 2,
  });

  return (
    <AppShell
      title="Research Library"
      subtitle="Semantic search across indexed research papers"
    >
      {/* Search Bar */}

      <div className="rounded-2xl border border-border bg-surface p-2 shadow-[0_20px_60px_-30px_rgba(15,23,42,0.18)]">
        <div className="flex items-center gap-3 px-3">
          <SearchIcon className="h-4 w-4 text-muted-foreground" />

          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="flex-1 bg-transparent py-3 text-[15px] outline-none placeholder:text-muted-foreground"
            placeholder="Search papers, methods, datasets, algorithms..."
          />
        </div>
      </div>

      {/* Search Info */}

      <div className="mt-6 flex items-center justify-between">
        <div className="text-xs text-muted-foreground">
          {query
            ? `Searching for "${query}"`
            : "Enter a search query"}
        </div>

        {query.length > 2 && (
          <div className="font-mono text-xs text-muted-foreground">
            {results.length} result(s)
          </div>
        )}
      </div>

      {/* Loading */}

      {isLoading && (
        <div className="mt-10 flex items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-primary" />
        </div>
      )}

      {/* Error */}

      {error && (
        <div className="mt-8 rounded-xl border border-red-500/20 bg-red-500/5 p-4 text-sm text-red-400">
          Failed to search papers.
        </div>
      )}

      {/* Results */}

      <div className="mt-6 space-y-3">
        {results.map((paper: any) => (
          <article
            key={`${paper.paper}-${paper.page}`}
            className="group grid grid-cols-[1fr_auto] gap-6 rounded-xl border border-border bg-surface p-6 transition-colors hover:border-primary/40"
          >
            <div>
              <div className="flex items-center gap-3 text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
                <span>{paper.source}</span>
                <span>·</span>
                <span>Page {paper.page}</span>
              </div>

              <h3 className="mt-2 text-[17px] font-medium leading-snug tracking-tight">
                {paper.paper}
              </h3>

              {paper.authors && (
                <div className="mt-1 text-[13px] text-muted-foreground">
                  {paper.authors}
                </div>
              )}

              <p className="mt-3 max-w-3xl text-[13.5px] leading-relaxed text-foreground/80">
                {paper.abstract ||
                  paper.text ||
                  "No abstract available"}
              </p>

              {paper.keywords && (
                <div className="mt-4 flex flex-wrap gap-1.5">
                  {paper.keywords
                    .split(",")
                    .slice(0, 8)
                    .map((keyword: string) => (
                      <span
                        key={keyword}
                        className="rounded-full bg-muted px-2.5 py-0.5 text-[11px] text-muted-foreground"
                      >
                        {keyword.trim()}
                      </span>
                    ))}
                </div>
              )}
            </div>

            <div className="flex flex-col items-end gap-2">
              <button className="grid h-8 w-8 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground">
                <Bookmark className="h-4 w-4" />
              </button>

              <button className="grid h-8 w-8 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground">
                <FileText className="h-4 w-4" />
              </button>
            </div>
          </article>
        ))}
      </div>

      {/* Empty State */}

      {!isLoading &&
        query.length > 2 &&
        results.length === 0 && (
          <div className="mt-12 text-center text-muted-foreground">
            No papers found.
          </div>
        )}
    </AppShell>
  );
}