import type { ReactNode } from "react";
import { Link, useNavigate, useRouterState } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
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
import { BrandMark } from "@/components/brand-mark";
import { deletePaper, getPapersDetailed } from "@/lib/api";
import type { PaperDetail } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { queryKeys } from "@/lib/query-keys";

const sidebarNav = [
  { to: "/dashboard", label: "Workspace", icon: Home },
  { to: "/upload", label: "Upload", icon: Upload },
  { to: "/ask", label: "Ask AI", icon: MessageSquare },
  { to: "/search", label: "Library", icon: Library },
  { to: "/reports", label: "Reports", icon: FileText },
  { to: "/compare", label: "Compare", icon: GitCompare },
];

// Shared with the Library page so the sidebar control can focus its input.
export const LIBRARY_SEARCH_INPUT_ID = "library-search-input";

//: Calm, user-facing wording for each non-indexed state. Authored here;
//: the backend's status_detail is shown only for a failure, and is
//: itself application-authored rather than an exception string.
const PAPER_STATUS_LABEL: Record<string, string> = {
  uploading: "Uploading…",
  uploaded: "Waiting to process",
  indexing: "Processing…",
  failed: "Processing failed",
  deleting: "Removing…",
};

/**
 * One paper that is not yet ready to query.
 *
 * Not a link: these papers have no indexed content, so the detail page
 * would have nothing to show. No retry control is offered — there is no
 * retry endpoint to call yet.
 */
function PaperStatusRow({ paper }: { paper: PaperDetail }) {
  const failed = paper.status === "failed";
  const label = PAPER_STATUS_LABEL[paper.status] ?? "Processing…";

  return (
    <div
      className="flex items-start gap-2 rounded-md px-3 py-1.5 text-[12px] text-muted-foreground"
      title={paper.title.replace(/_/g, " ")}
    >
      {failed ? (
        <AlertCircle className="mt-0.5 h-3 w-3 shrink-0 text-destructive" />
      ) : (
        <Loader2 className="mt-0.5 h-3 w-3 shrink-0 animate-spin" />
      )}
      <div className="min-w-0">
        <div className="truncate">{paper.title.replace(/_/g, " ")}</div>
        <div className={failed ? "text-[11px] text-destructive" : "text-[11px]"}>
          {label}
        </div>
        {failed && paper.status_detail && (
          <div className="text-[11px] text-muted-foreground">{paper.status_detail}</div>
        )}
      </div>
    </div>
  );
}

function isMacPlatform() {
  if (typeof navigator === "undefined") return false;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  return /mac|iphone|ipad|ipod/i.test(nav.userAgentData?.platform || nav.platform || "");
}

// Cmd+K on macOS, Ctrl+K elsewhere. Shifted/Alt variants are left alone
// (e.g. Ctrl+Shift+K opens the Firefox console). `key` can be missing on
// synthetic keydown events such as Chrome autofill.
function isLibrarySearchShortcut(event: KeyboardEvent, isMac: boolean) {
  const modifier = isMac ? event.metaKey : event.ctrlKey;
  return modifier && !event.altKey && !event.shiftKey && event.key?.toLowerCase() === "k";
}

export function AppShell({
  children,
  title,
  subtitle,
  actions,
}: {
  children: ReactNode;
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [deletingPaper, setDeletingPaper] = useState<string | null>(null);
  const { session, user, isLoading: authLoading, signInWithGoogle, signOut } = useAuth();
  const userId = user?.id;

  const { data: allPapers = [] } = useQuery({
    queryKey: queryKeys.papersDetailed(userId),
    queryFn: getPapersDetailed,
    staleTime: 30_000,
    // Never fetch before the session resolves — an unauthenticated call
    // would throw AuthenticationRequiredError and burn retries.
    enabled: !!userId,
  });

  // The ready-to-query titles, derived rather than fetched separately so
  // the sidebar still costs one request.
  const papers = allPapers.filter((p) => p.status === "indexed").map((p) => p.title);

  // Anything still being processed, or that failed. These were invisible
  // before: /papers returns indexed titles only, so a paper whose upload
  // or indexing failed simply never appeared.
  const unfinished = allPapers.filter((p) => p.status !== "indexed");

  // navigator is unavailable during SSR, so render the non-Mac hint first
  // and switch after mount to avoid a hydration mismatch.
  const [isMac, setIsMac] = useState(false);
  useEffect(() => {
    setIsMac(isMacPlatform());
  }, []);

  const openLibrarySearch = useCallback(() => {
    if (pathname === "/search") {
      document.getElementById(LIBRARY_SEARCH_INPUT_ID)?.focus();
      return;
    }
    void navigate({ to: "/search" });
  }, [pathname, navigate]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isLibrarySearchShortcut(event, isMac)) return;
      event.preventDefault();
      openLibrarySearch();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isMac, openLibrarySearch]);

  const handleDeletePaper = async (e: React.MouseEvent, paperName: string) => {
    e.preventDefault();
    e.stopPropagation();
    if (
      !confirm(
        `Delete "${paperName.replace(/_/g, " ")}"?\n\nThis will permanently remove the PDF and all indexed vectors.`,
      )
    )
      return;
    setDeletingPaper(paperName);
    try {
      await deletePaper(paperName);
      qc.invalidateQueries({ queryKey: queryKeys.papers(userId) });
      qc.invalidateQueries({ queryKey: queryKeys.papersDetailed(userId) });
      qc.invalidateQueries({ queryKey: queryKeys.stats(userId) });
      // Search now has its own namespace, so it is no longer swept up by
      // the ["papers"] prefix — invalidate it explicitly so results for a
      // deleted paper don't linger.
      qc.invalidateQueries({ queryKey: queryKeys.searchAll() });
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
          <Link
            to="/"
            className="flex h-14 items-center gap-2.5 border-b border-border/60 px-5 text-[15px] font-medium tracking-tight"
          >
            <BrandMark size={22} />
            ResearchMind
          </Link>
          <div className="px-3 pt-4">
            <button
              type="button"
              onClick={openLibrarySearch}
              aria-keyshortcuts={isMac ? "Meta+K" : "Control+K"}
              className="flex w-full items-center justify-between rounded-lg border border-border bg-surface px-3 py-2 text-[13px] text-muted-foreground hover:text-foreground"
            >
              <span className="flex items-center gap-2">
                <Search className="h-3.5 w-3.5" /> Search…
              </span>
              <span className="font-mono text-[10px]">{isMac ? "⌘K" : "Ctrl K"}</span>
            </button>
          </div>
          <nav className="mt-3 flex-1 overflow-y-auto space-y-px px-3">
            {sidebarNav.map((item) => {
              const active = pathname === item.to;
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  className={`flex items-center gap-2.5 rounded-md px-3 py-2 text-[13px] ${active ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"}`}
                >
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

            {unfinished.length > 0 && (
              <div className="mt-4">
                <div className="px-3 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  Processing
                </div>
                {unfinished.map((p) => (
                  <PaperStatusRow key={p.paper_id} paper={p} />
                ))}
              </div>
            )}

            {papers.length === 0 && unfinished.length === 0 && (
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
          <div className="border-t border-border/60 p-3 space-y-1">
            {!authLoading && session ? (
              <>
                <div
                  className="truncate px-3 py-1 text-[12px] text-muted-foreground"
                  title={user?.email ?? ""}
                >
                  {user?.email ?? "Signed in"}
                </div>
                <button
                  onClick={() => signOut()}
                  className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-[13px] text-muted-foreground hover:text-foreground"
                >
                  <Settings className="h-3.5 w-3.5" /> Sign out
                </button>
              </>
            ) : (
              <button
                onClick={() => signInWithGoogle()}
                disabled={authLoading}
                className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-[13px] text-muted-foreground hover:text-foreground"
              >
                <Settings className="h-3.5 w-3.5" /> Sign in with Google
              </button>
            )}
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
