import { createFileRoute, Link } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import {
  ArrowUpRight,
  BookOpen,
  FileText,
  Layers,
  Loader2,
  MessageSquare,
  Plus,
  Upload,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getDashboardStats } from "@/lib/api";

export const Route = createFileRoute("/dashboard")({
  head: () => ({ meta: [{ title: "Workspace · ResearchMind" }] }),
  component: Dashboard,
});

function Dashboard() {
  const { data: stats, isLoading } = useQuery({
    queryKey: ["stats"],
    queryFn: getDashboardStats,
    refetchInterval: 30_000,
  });

  return (
    <AppShell
      title="Workspace"
      subtitle="Your research library"
      actions={
        <>
          <Button variant="ghost" size="sm" asChild>
            <Link to="/upload">
              <Upload className="mr-1.5 h-3.5 w-3.5" /> Upload
            </Link>
          </Button>
          <Button size="sm" asChild>
            <Link to="/reports">
              <Plus className="mr-1.5 h-3.5 w-3.5" /> New review
            </Link>
          </Button>
        </>
      }
    >
      <div>
        <h1 className="mt-1 text-balance text-4xl font-medium tracking-tight md:text-5xl">
          ResearchMind AI
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          AI-powered academic research workspace
        </p>
      </div>

      {/* Stats */}
      <section className="mt-10">
        <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
          <StatCard
            icon={<FileText className="h-4 w-4 text-muted-foreground" />}
            label="Papers indexed"
            value={isLoading ? null : (stats?.total_papers ?? 0)}
          />
          <StatCard
            icon={<Layers className="h-4 w-4 text-muted-foreground" />}
            label="Vector chunks"
            value={isLoading ? null : (stats?.total_chunks ?? 0)}
          />
          <StatCard
            icon={<BookOpen className="h-4 w-4 text-muted-foreground" />}
            label="Collection"
            value={isLoading ? null : "researchmind"}
            isText
          />
        </div>
      </section>

      {/* Recent Papers */}
      <section className="mt-14">
        <SectionHeader title="Recent uploads" link="/upload" />

        {isLoading && (
          <div className="mt-5 flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        )}

        {!isLoading && stats && stats.recent_papers.length > 0 && (
          <div className="mt-5 grid gap-4 md:grid-cols-3">
            {stats.recent_papers.map((name) => (
              <div
                key={name}
                className="rounded-xl border border-border bg-surface p-5"
              >
                <FileText className="h-4 w-4 text-muted-foreground" />
                <div className="mt-4 break-words text-[14px] font-medium leading-snug tracking-tight">
                  {name}
                </div>
              </div>
            ))}
          </div>
        )}

        {!isLoading && stats && stats.recent_papers.length === 0 && (
          <div className="mt-5 rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
            No papers uploaded yet.{" "}
            <Link
              to="/upload"
              className="text-primary underline-offset-2 hover:underline"
            >
              Upload your first paper
            </Link>
          </div>
        )}
      </section>

      <section className="mt-14 grid gap-10 lg:grid-cols-2">
        <div>
          <SectionHeader title="Ask AI" link="/ask" />
          <div className="mt-4 rounded-xl border border-border bg-surface p-5">
            <MessageSquare className="h-4 w-4 text-muted-foreground" />
            <p className="mt-3 text-sm text-muted-foreground">
              Ask grounded questions about your indexed papers with citations.
            </p>
            <Button className="mt-4" size="sm" asChild>
              <Link to="/ask">
                Open Ask AI{" "}
                <ArrowUpRight className="ml-1.5 h-3.5 w-3.5" />
              </Link>
            </Button>
          </div>
        </div>
        <div>
          <SectionHeader title="Search library" link="/search" />
          <div className="mt-4 rounded-xl border border-border bg-surface p-5">
            <BookOpen className="h-4 w-4 text-muted-foreground" />
            <p className="mt-3 text-sm text-muted-foreground">
              Semantic search across all indexed papers and chunks.
            </p>
            <Button className="mt-4" size="sm" variant="outline" asChild>
              <Link to="/search">
                Open Search{" "}
                <ArrowUpRight className="ml-1.5 h-3.5 w-3.5" />
              </Link>
            </Button>
          </div>
        </div>
      </section>
    </AppShell>
  );
}

function StatCard({
  icon,
  label,
  value,
  isText,
}: {
  icon: React.ReactNode;
  label: string;
  value: number | string | null;
  isText?: boolean;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      {icon}
      <div className="mt-3 text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 text-3xl font-semibold tracking-tight">
        {value === null ? (
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        ) : isText ? (
          <span className="font-mono text-base">{value}</span>
        ) : (
          (value as number).toLocaleString()
        )}
      </div>
    </div>
  );
}

function SectionHeader({
  title,
  link,
}: {
  title: string;
  link: string;
}) {
  return (
    <div className="flex items-center justify-between">
      <h2 className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
        {title}
      </h2>
      <Link
        to={link}
        className="text-xs text-muted-foreground hover:text-foreground"
      >
        View all â†’
      </Link>
    </div>
  );
}

