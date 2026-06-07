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
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getPaperDetails, summarizePaper, deletePaper } from "@/lib/api";
import { useState } from "react";
import type { ReactNode } from "react";

export const Route = createFileRoute("/paper/$paperName")({
  head: ({ params }) => ({
    meta: [{ title: `${params.paperName.replace(/_/g, " ")} · ResearchMind` }],
  }),
  component: PaperDetailsPage,
});

function PaperDetailsPage() {
  const { paperName } = Route.useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [summary, setSummary] = useState("");
  const [summarizing, setSummarizing] = useState(false);
  const [summaryError, setSummaryError] = useState("");
  const [deleteError, setDeleteError] = useState("");

  const {
    data: details,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["paper-details", paperName],
    queryFn: () => getPaperDetails(paperName),
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
      qc.invalidateQueries({ queryKey: ["papers"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
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
                  <p className="whitespace-pre-wrap text-[15px] leading-[1.75] text-foreground/90">
                    {summary}
                  </p>
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
