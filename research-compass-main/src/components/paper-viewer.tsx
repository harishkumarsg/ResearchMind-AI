import { useEffect, useRef, useState } from "react";
import { AlertCircle, ChevronLeft, ChevronRight, FileText, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getPaperFileUrl } from "@/lib/api";

/**
 * The owner's PDF, rendered by the browser's own viewer.
 *
 * The bytes arrive over the authenticated API and become an in-memory
 * blob URL: the private Storage object is never exposed, there is no
 * signed URL, and nothing is cached to disk. The URL is revoked on
 * unmount and whenever the paper changes, so a signed-out user cannot
 * hold a live reference to the document.
 *
 * KNOWN LIMITATION, stated rather than hidden: page navigation uses the
 * `#page=N` fragment, which is a convention of each browser's built-in
 * PDF viewer, not a web standard. Chrome, Edge and Firefox honour it;
 * Safari and some embedded viewers ignore it. Changing the fragment on a
 * blob URL also reloads the iframe rather than scrolling within it, so
 * jumping to a citation is a reload, not a smooth scroll. Precise
 * in-document navigation needs a real PDF renderer, which this phase
 * deliberately does not add.
 */
export function PaperViewer({
  paperId,
  page,
  onPageChange,
}: {
  paperId: string;
  page: number;
  onPageChange: (page: number) => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const urlRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");

    getPaperFileUrl(paperId)
      .then((objectUrl) => {
        if (cancelled) {
          // Arrived after unmount — release it rather than leak it.
          URL.revokeObjectURL(objectUrl);
          return;
        }
        urlRef.current = objectUrl;
        setUrl(objectUrl);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "The paper file is unavailable.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      if (urlRef.current) {
        URL.revokeObjectURL(urlRef.current);
        urlRef.current = null;
      }
    };
  }, [paperId]);

  if (loading) {
    return (
      <div
        className="flex h-full min-h-[420px] items-center justify-center rounded-xl border border-border bg-surface"
        role="status"
        aria-live="polite"
      >
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Loading paper…
        </div>
      </div>
    );
  }

  if (error || !url) {
    return (
      <div className="flex h-full min-h-[420px] flex-col items-center justify-center gap-3 rounded-xl border border-border bg-surface p-8 text-center">
        <AlertCircle className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
        <p className="text-sm text-muted-foreground">{error || "The paper file is unavailable."}</p>
        <p className="max-w-xs text-xs text-muted-foreground">
          You can still ask questions about this paper — the AI panel uses the indexed text,
          not this file.
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col gap-2">
      <div className="flex items-center justify-between gap-2 rounded-lg border border-border bg-surface px-3 py-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <FileText className="h-3.5 w-3.5" aria-hidden="true" />
          <span>Paper</span>
        </div>

        <div className="flex items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0"
            onClick={() => onPageChange(Math.max(1, page - 1))}
            disabled={page <= 1}
            aria-label="Previous page"
          >
            <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>

          <label htmlFor="paper-page" className="sr-only">
            Page number
          </label>
          <input
            id="paper-page"
            type="number"
            min={1}
            value={page}
            onChange={(e) => {
              const next = Number.parseInt(e.target.value, 10);
              if (Number.isFinite(next) && next >= 1) onPageChange(next);
            }}
            className="h-7 w-14 rounded border border-border bg-transparent px-2 text-center text-xs outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />

          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0"
            onClick={() => onPageChange(page + 1)}
            aria-label="Next page"
          >
            <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        </div>
      </div>

      {/* keyed on the page so a citation jump re-targets the fragment */}
      <iframe
        key={page}
        src={`${url}#page=${page}`}
        title="Paper document"
        className="h-full min-h-[420px] w-full rounded-xl border border-border bg-surface"
      />
    </div>
  );
}
