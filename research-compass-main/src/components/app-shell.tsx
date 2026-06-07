import type { ReactNode } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  FileText,
  GitCompare,
  Home,
  Library,
  Loader2,
  MessageSquare,
  Plus,
  Search,
  Settings,
  Upload,
  X,
} from "lucide-react";
import { deletePaper, getPapers } from "@/lib/api";

const sidebarNav = [
  { to: "/dashboard", label: "Workspace", icon: Home },
  { to: "/upload", label: "Upload", icon: Upload },
  { to: "/ask", label: "Ask AI", icon: MessageSquare },
  { to: "/search", label: "Library", icon: Library },
  { to: "/reports", label: "Reports", icon: FileText },
  { to: "/compare", label: "Compare", icon: GitCompare },
];

export function AppShell({ children, title, subtitle, actions }: { children: ReactNode; title: string; subtitle?: string; actions?: ReactNode }) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const qc = useQueryClient();
  const [deletingPaper, setDeletingPaper] = useState<string | null>(null);

  const { data: papers = [] } = useQuery({
    queryKey: ["papers"],
    queryFn: getPapers,
    staleTime: 30_000,
  });

  const handleDeletePaper = async (e: React.MouseEvent, paperName: string) => {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm(`Delete "${paperName.replace(/_/g, " ")}"?\n\nThis will permanently remove the PDF and all indexed vectors.`)) return;
    setDeletingPaper(paperName);
    try {
      await deletePaper(paperName);
      qc.invalidateQueries({ queryKey: ["papers"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    } catch {
      // ignore — paper details page also shows the error
    } finally {
      setDeletingPaper(null);
    }
  };

  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="grid min-h-screen grid-cols-[260px_1fr]">
        <aside className="sticky top-0 hidden h-screen flex-col border-r border-border/60 bg-sidebar md:flex">
          <Link to="/" className="flex h-14 items-center gap-2 border-b border-border/60 px-5 text-[15px] font-medium tracking-tight">
            <span className="grid h-7 w-7 place-items-center rounded-md bg-primary text-primary-foreground text-xs">R</span>
            ResearchMind
          </Link>
          <div className="px-3 pt-4">
            <button className="flex w-full items-center justify-between rounded-lg border border-border bg-surface px-3 py-2 text-[13px] text-muted-foreground hover:text-foreground">
              <span className="flex items-center gap-2"><Search className="h-3.5 w-3.5" /> Search…</span>
              <span className="font-mono text-[10px]">⌘K</span>
            </button>
          </div>
          <nav className="mt-3 flex-1 overflow-y-auto space-y-px px-3">
            {sidebarNav.map((item) => {
              const active = pathname === item.to;
              return (
                <Link key={item.to} to={item.to} className={`flex items-center gap-2.5 rounded-md px-3 py-2 text-[13px] ${active ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"}`}>
                  <item.icon className="h-3.5 w-3.5" strokeWidth={1.75} />
                  {item.label}
                </Link>
              );
            })}

            {/* Indexed Papers */}
            {papers.length > 0 && (
              <div className="mt-4">
                <div className="px-3 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  Indexed Papers
                </div>
                {papers.map((p) => (
                  <div key={p} className="group relative flex items-center">
                    <Link
                      to="/paper/$paperName"
                      params={{ paperName: p }}
                      className="flex flex-1 items-center gap-2 rounded-md px-3 py-1.5 text-[12px] text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                      title={p.replace(/_/g, " ")}
                    >
                      <FileText className="h-3 w-3 shrink-0" />
                      <span className="truncate">{p.replace(/_/g, " ")}</span>
                    </Link>
                    <button
                      onClick={(e) => handleDeletePaper(e, p)}
                      disabled={deletingPaper === p}
                      className="absolute right-1.5 hidden h-5 w-5 items-center justify-center rounded text-muted-foreground hover:text-destructive group-hover:flex"
                      title="Delete paper"
                    >
                      {deletingPaper === p ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <X className="h-3 w-3" />
                      )}
                    </button>
                  </div>
                ))}
              </div>
            )}

            {papers.length === 0 && (
              <div className="mt-4">
                <div className="px-3 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  Indexed Papers
                </div>
                <Link
                  to="/upload"
                  className="flex items-center gap-2 rounded-md px-3 py-1.5 text-[12px] text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                >
                  <Plus className="h-3 w-3 shrink-0" />
                  <span>Upload your first paper</span>
                </Link>
              </div>
            )}
          </nav>
          <div className="border-t border-border/60 p-3">
            <button className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-[13px] text-muted-foreground hover:text-foreground">
              <Settings className="h-3.5 w-3.5" /> Settings
            </button>
          </div>
        </aside>
        <main className="min-w-0">
          <header className="flex h-14 items-center gap-4 border-b border-border/60 px-8">
            <div>
              <div className="text-[15px] font-medium tracking-tight">{title}</div>
              {subtitle && <div className="text-[11px] text-muted-foreground">{subtitle}</div>}
            </div>
            <div className="ml-auto flex items-center gap-2">{actions}</div>
          </header>
          <div className="px-8 py-10">{children}</div>
        </main>
      </div>
    </div>
  );
}