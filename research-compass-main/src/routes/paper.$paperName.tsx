import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import {
  AlertCircle,
  BookOpen,
  FileText,
  GitCompare,
  Loader2,
  MessageSquare,
  Sparkles,
  Tag,
  Trash2,
  User,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import ReactMarkdown from "react-markdown";
import { PaperViewer } from "@/components/paper-viewer";
import { PaperAskPanel } from "@/components/paper-ask-panel";
import { PaperIntelligencePanel } from "@/components/paper-intelligence-panel";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getPaperDetails, summarizePaper, deletePaper, retryUnlessRateLimited } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { queryKeys } from "@/lib/query-keys";
import { useRequireAuth } from "@/lib/require-auth";
import { useState } from "react";
import type { ReactNode } from "react";

export const Route = createFileRoute("/paper/$paperName")({
  /**
   * Optional `?page=N`, so another view can deep-link to a specific page
   * of this paper — the comparison matrix sends a reader here from an
   * evidence reference. Optional on purpose: every existing link to this
   * route omits it and must keep working. Anything that is not a whole
   * number of at least 1 is dropped rather than coerced, so a junk value
   * lands on page 1 instead of somewhere arbitrary.
   */
  validateSearch: (search: Record<string, unknown>): { page?: number } => {
    const raw = Number(search.page);
    return Number.isFinite(raw) && raw >= 1 ? { page: Math.floor(raw) } : {};
  },
  head: ({ params }) => ({
    meta: [{ title: `${params.paperName.replace(/_/g, " ")} · ResearchMind` }],
  }),
  component: PaperDetailsPage,
});

function PaperDetailsPage() {
  // Signed-out visitors are sent to the public landing page.
  useRequireAuth();

  const { paperName } = Route.useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { user } = useAuth();
  const userId = user?.id;

  // The workspace's current page. Lifted here so a citation in the AI
  // panel can drive the viewer, and seeded from ?page=N so a deep link
  // from the comparison matrix opens on the cited page.
  const { page: requestedPage } = Route.useSearch();
  const [page, setPage] = useState(requestedPage ?? 1);

  const [summary, setSummary] = useState("");
  const [summarizing, setSummarizing] = useState(false);
  const [summaryError, setSummaryError] = useState("");
  const [deleteError, setDeleteError] = useState("");

  const {
    data: details,
    isLoading,
    error,
  } = useQuery({
    queryKey: queryKeys.paperDetails(userId, paperName),
    queryFn: () => getPaperDetails(paperName),
    enabled: !!userId,
    // Each load embeds the paper name with Voyage, so a throttled request
    // must not be retried — that only spends more of the same quota.
    retry: retryUnlessRateLimited,
  });

  const handleSummarize = async () => {
    setSummarizing(true);
    setSummaryError("");
    setSummary("");
    try {
      const result = await summarizePaper(paperName);
      setSummary(result.summary);
    } catch (err: unknown) {
      setSummaryError(
        err instanceof Error ? err.message : "Summarization failed."
      );
    } finally {
      setSummarizing(false);
    }
  };

  const handleDelete = async () => {
    if (
      !confirm(
        `Delete "${paperName.replace(/_/g, " ")}"?\n\nThis will permanently remove the PDF and all indexed vectors. This cannot be undone.`
      )
    )
      return;
    setDeleteError("");
    try {
      await deletePaper(paperName);
      qc.invalidateQueries({ queryKey: queryKeys.papers(userId) });
      qc.invalidateQueries({ queryKey: queryKeys.stats(userId) });
      // Search has its own namespace now, so invalidate it explicitly —
      // the ["papers"] prefix no longer sweeps it up.
      qc.invalidateQueries({ queryKey: queryKeys.searchAll() });
      navigate({ to: "/dashboard" });
    } catch (err: unknown) {
      setDeleteError(err instanceof Error ? err.message : "Delete failed.");
    }
  };

  const displayName = paperName.replace(/_/g, " ");

  return (
    <AppShell
      title={displayName}
      subtitle="Paper details"
      actions={
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to="/compare">
              <GitCompare className="mr-1.5 h-3.5 w-3.5" />
              Compare
            </Link>
          </Button>
          <Button variant="destructive" size="sm" onClick={handleDelete}>
            <Trash2 className="mr-1.5 h-3.5 w-3.5" />
            Delete
          </Button>
        </div>
      }
    >
      {deleteError && (
        <div className="mb-6 flex items-center gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {deleteError}
        </div>
      )}

      {isLoading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading paper details…
        </div>
      )}

      {error && (
        <div className="flex items-center gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {(error as Error).message}
        </div>
      )}

      {details && details.paper_id && (
        <section aria-label="Paper workspace" className="mb-10">
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,420px)]">
            {/* Viewer — full width on narrow screens, side-by-side on wide */}
            <div className="min-w-0 lg:h-[min(78vh,900px)]">
              <PaperViewer
                paperId={details.paper_id}
                page={page}
                onPageChange={setPage}
              />
            </div>

            {/* AI panel — Intelligence and Ask share the column beside
                the viewer. Tabs rather than stacking: both want the full
                height, and both drive the same viewer page state. */}
            <div className="flex min-w-0 flex-col lg:h-[min(78vh,900px)]">
              <Tabs defaultValue="intelligence" className="flex min-h-0 flex-1 flex-col">
                <TabsList className="w-full">
                  <TabsTrigger value="intelligence" className="flex-1">
                    Intelligence
                  </TabsTrigger>
                  <TabsTrigger value="ask" className="flex-1">
                    Ask
                  </TabsTrigger>
                </TabsList>

                <TabsContent
                  value="intelligence"
                  className="mt-3 min-h-0 flex-1 lg:overflow-y-auto"
                >
                  <PaperIntelligencePanel
                    paperId={details.paper_id}
                    onEvidenceClick={setPage}
                  />
                </TabsContent>

                <TabsContent value="ask" className="mt-3 min-h-0 flex-1 lg:overflow-y-auto">
                  <PaperAskPanel paperId={details.paper_id} onCitationClick={setPage} />
                </TabsContent>
              </Tabs>
            </div>
          </div>
        </section>
      )}

      {details && (
        <div className="grid gap-8 lg:grid-cols-[1fr_260px]">
          {/* Main */}
          <div className="space-y-6">
            {/* Stat chips */}
            <div className="flex flex-wrap gap-3">
              <Chip label="chunks" value={String(details.total_chunks)} />
              {details.source && (
                <Chip label="file" value={details.source} />
              )}
            </div>

            {details.authors && (
              <InfoBlock
                icon={<User className="h-3.5 w-3.5" />}
                label="Authors"
              >
                {details.authors}
              </InfoBlock>
            )}

            {details.keywords && (
              <InfoBlock
                icon={<Tag className="h-3.5 w-3.5" />}
                label="Keywords"
              >
                {details.keywords}
              </InfoBlock>
            )}

            {details.abstract && (
              <InfoBlock
                icon={<BookOpen className="h-3.5 w-3.5" />}
                label="Abstract"
              >
                {details.abstract}
              </InfoBlock>
            )}

            {details.preview && (
              <div>
                <div className="mb-2 text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                  Text Preview
                </div>
                <div className="rounded-xl border border-border bg-surface p-5 text-[14px] leading-[1.75] text-foreground/80 font-mono">
                  {details.preview}
                </div>
              </div>
            )}

            {/* Summarize section */}
            <div>
              {!summary && !summarizing && (
                <Button onClick={handleSummarize} variant="outline">
                  <Sparkles className="mr-2 h-4 w-4" />
                  Generate AI Summary
                </Button>
              )}

              {summarizing && (
                <div className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin shrink-0" />
                  Generating summary… this may take ~30 seconds on CPU
                </div>
              )}

              {summaryError && (
                <div className="flex items-center gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
                  <AlertCircle className="h-4 w-4 shrink-0" />
                  {summaryError}
                </div>
              )}

              {summary && (
                <div className="rounded-xl border border-border bg-surface p-6">
                  <div className="mb-3 flex items-center gap-2 text-sm text-muted-foreground">
                    <Sparkles className="h-4 w-4" />
                    AI Summary
                  </div>
                  <div className="prose prose-sm max-w-none text-foreground/90
                    prose-headings:font-semibold prose-headings:tracking-tight prose-headings:text-foreground
                    prose-h3:text-lg prose-h4:text-base
                    prose-p:leading-[1.75] prose-p:text-[15px]
                    prose-li:text-[15px] prose-li:leading-[1.75]
                    prose-strong:text-foreground prose-strong:font-semibold
                    prose-ul:my-3 prose-ol:my-3">
                    <ReactMarkdown>{summary}</ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Sidebar */}
          <aside className="space-y-4">
            <div className="rounded-xl border border-border bg-surface p-4">
              <div className="mb-3 text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                Actions
              </div>
              <div className="space-y-2">
                <Button className="w-full justify-start" size="sm" asChild>
                  <Link to="/ask">
                    <MessageSquare className="mr-2 h-3.5 w-3.5" />
                    Ask AI about papers
                  </Link>
                </Button>
                <Button
                  className="w-full justify-start"
                  size="sm"
                  variant="outline"
                  asChild
                >
                  <Link to="/compare">
                    <GitCompare className="mr-2 h-3.5 w-3.5" />
                    Compare with another
                  </Link>
                </Button>
                <Button
                  className="w-full justify-start"
                  size="sm"
                  variant="outline"
                  asChild
                >
                  <Link to="/reports">
                    <FileText className="mr-2 h-3.5 w-3.5" />
                    Generate review
                  </Link>
                </Button>
              </div>
            </div>
          </aside>
        </div>
      )}
    </AppShell>
  );
}

function Chip({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-full border border-border bg-surface px-3 py-1 text-xs">
      <span className="text-muted-foreground">{label}: </span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

function InfoBlock({
  icon,
  label,
  children,
}: {
  icon: ReactNode;
  label: string;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className="rounded-xl border border-border bg-surface p-4 text-[14px] leading-[1.75] text-foreground/85">
        {children}
      </div>
    </div>
  );
}
