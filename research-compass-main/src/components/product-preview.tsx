import type { ComponentType, ReactNode } from "react";
import {
  ArrowUp,
  Download,
  FileText,
  GitCompare,
  Home,
  Library,
  MessageSquare,
  Search,
  Sparkles,
  Upload,
} from "lucide-react";
import { BrandMark } from "@/components/brand-mark";

/*
 * Static replicas of real ResearchMind screens for the public homepage.
 *
 * They mirror the labels and layout of the signed-in app (AppShell, Ask AI,
 * Library, Compare, Literature Review) but render sample content only: the
 * live screens need a session and would show a signed-in visitor's own data.
 *
 * The sample papers are public — Vaswani et al., "Attention Is All You Need"
 * (2017) and Devlin et al., "BERT" (2019). Every quoted statement and page
 * number below is taken from those papers.
 */

const PAPER_A = "vaswani_2017_attention.pdf";
const PAPER_B = "devlin_2019_bert.pdf";

// Same order and icons as AppShell's sidebar.
const shellNav: {
  label: string;
  icon: ComponentType<{ className?: string; strokeWidth?: number }>;
}[] = [
  { label: "Workspace", icon: Home },
  { label: "Upload", icon: Upload },
  { label: "Ask AI", icon: MessageSquare },
  { label: "Library", icon: Library },
  { label: "Reports", icon: FileText },
  { label: "Compare", icon: GitCompare },
];

// The app displays paper names with underscores replaced by spaces.
const displayName = (paper: string) => paper.replace(/_/g, " ");

export function BrowserFrame({
  path,
  label,
  className = "",
  children,
}: {
  path: string;
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      role="img"
      aria-label={label}
      className={`overflow-hidden rounded-xl border border-border bg-surface shadow-[0_24px_60px_-28px_rgba(15,23,42,0.22)] ${className}`}
    >
      <div aria-hidden className="flex items-center gap-1.5 border-b border-border px-3.5 py-2">
        <span className="h-2 w-2 rounded-full bg-muted-foreground/20" />
        <span className="h-2 w-2 rounded-full bg-muted-foreground/20" />
        <span className="h-2 w-2 rounded-full bg-muted-foreground/20" />
        <div className="ml-2.5 flex h-6 min-w-0 flex-1 items-center truncate rounded-md border border-border bg-background px-2.5 font-mono text-[11px] text-muted-foreground">
          researchmindlab.com{path}
        </div>
      </div>
      <div aria-hidden className="text-left">
        {children}
      </div>
    </div>
  );
}

function SourceRow({ paper, page, cited }: { paper: string; page: number; cited: boolean }) {
  return (
    <div
      className={`flex items-start gap-2.5 rounded-lg border bg-surface px-3 py-2.5 ${
        cited ? "border-border" : "border-dashed border-border"
      }`}
    >
      <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <div>
        <div className="text-[12px] font-medium leading-snug">{displayName(paper)}</div>
        <div className="text-[10.5px] text-muted-foreground">Page {page}</div>
      </div>
    </div>
  );
}

/** The Ask AI screen inside the workspace shell. */
export function AskWorkspacePreview() {
  return (
    <div className="grid md:grid-cols-[190px_1fr]">
      <aside className="hidden border-r border-border/60 bg-sidebar md:block">
        <div className="flex h-11 items-center gap-2 border-b border-border/60 px-4 text-[13px] font-medium tracking-tight">
          <BrandMark size={16} />
          ResearchMind
        </div>
        <div className="px-2.5 pt-3">
          <div className="flex items-center justify-between rounded-md border border-border bg-surface px-2.5 py-1.5 text-[11px] text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <Search className="h-3 w-3" /> Search…
            </span>
            <span className="font-mono text-[9px]">Ctrl K</span>
          </div>
        </div>
        <ul className="mt-2.5 space-y-px px-2.5">
          {shellNav.map(({ label, icon: Icon }) => (
            <li
              key={label}
              className={`flex items-center gap-2 rounded px-2.5 py-1.5 text-[12px] ${
                label === "Ask AI" ? "bg-muted text-foreground" : "text-muted-foreground"
              }`}
            >
              <Icon className="h-3 w-3" strokeWidth={1.75} />
              {label}
            </li>
          ))}
        </ul>
        <div className="mt-4 px-2.5 pb-4">
          <div className="px-2.5 pb-1 text-[9px] font-medium uppercase tracking-wider text-muted-foreground">
            Indexed Papers
          </div>
          {[PAPER_B, PAPER_A].map((paper) => (
            <div
              key={paper}
              className="flex items-center gap-1.5 truncate rounded px-2.5 py-1 text-[11px] text-muted-foreground"
            >
              <FileText className="h-2.5 w-2.5 shrink-0" />
              <span className="truncate">{displayName(paper)}</span>
            </div>
          ))}
        </div>
      </aside>

      <div className="min-w-0">
        <div className="flex h-11 flex-col justify-center border-b border-border/60 px-5">
          <div className="text-[13px] font-medium leading-tight tracking-tight">Ask AI</div>
          <div className="text-[10px] text-muted-foreground">
            Grounded answers from your indexed research papers
          </div>
        </div>

        <div className="space-y-3.5 p-4 md:p-5">
          <div className="rounded-lg border border-border bg-surface p-3">
            <p className="text-[13px] leading-relaxed">
              What does the Transformer use instead of recurrence, and how is its attention
              computed?
            </p>
            <div className="mt-2.5 flex items-center justify-between">
              <span className="text-[10px] text-muted-foreground">Ctrl+Enter to ask</span>
              <span className="inline-flex items-center rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground">
                <ArrowUp className="mr-1 h-3 w-3" /> Ask
              </span>
            </div>
          </div>

          <div>
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Sparkles className="h-3 w-3" />
              ResearchMind Answer
            </div>
            <div className="mt-2 rounded-lg border border-border bg-surface p-3.5">
              <p className="text-[13px] leading-[1.7] text-foreground/90">
                The Transformer is based solely on attention mechanisms, dispensing with recurrence
                and convolutions entirely [Page 1]. It uses scaled dot-product attention: the dot
                products of a query with all keys are divided by √dk, and a softmax gives the
                weights on the values [Page 4].
              </p>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <div className="text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                Sources
              </div>
              <SourceRow paper={PAPER_A} page={1} cited />
              <SourceRow paper={PAPER_A} page={4} cited />
            </div>
            <div className="space-y-1.5 opacity-60">
              <div className="text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                Also retrieved · not cited
              </div>
              <SourceRow paper={PAPER_A} page={3} cited={false} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** The Library page's semantic search results. */
export function LibrarySearchPreview() {
  const results = [
    {
      paper: PAPER_A,
      page: 4,
      abstract:
        "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder.",
    },
    {
      paper: PAPER_B,
      page: 3,
      abstract:
        "We introduce a new language representation model called BERT, which stands for Bidirectional Encoder Representations from Transformers.",
    },
  ];

  return (
    <div className="space-y-2.5 p-4">
      <div className="flex items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2 text-[12px]">
        <Search className="h-3.5 w-3.5 text-muted-foreground" />
        scaled dot-product attention
      </div>
      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>Searching for "scaled dot-product attention"</span>
        <span className="font-mono">{results.length} result(s)</span>
      </div>
      {results.map((r) => (
        <div key={r.paper} className="rounded-lg border border-border bg-surface p-3">
          <div className="flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-wider text-muted-foreground">
            <span className="truncate">{r.paper}</span>
            <span>·</span>
            <span className="shrink-0">Page {r.page}</span>
          </div>
          <div className="mt-1 truncate text-[12.5px] font-medium leading-snug">{r.paper}</div>
          <p className="mt-1.5 line-clamp-2 text-[11.5px] leading-relaxed text-foreground/80">
            {r.abstract}
          </p>
        </div>
      ))}
    </div>
  );
}

/** The Compare studies table for two indexed papers. */
export function ComparePreview() {
  const rows = [
    ["Architecture", "Encoder–decoder built on attention", "Bidirectional Transformer encoder"],
    [
      "Training",
      "Trained on translation pairs",
      "Masked language modelling and next sentence prediction",
    ],
    [
      "Evaluated on",
      "WMT 2014 English–German and English–French",
      "GLUE, SQuAD v1.1 and v2.0, SWAG",
    ],
  ];

  return (
    <div className="p-4">
      <div className="grid grid-cols-2 gap-2">
        {[PAPER_A, PAPER_B].map((paper, i) => (
          <div key={paper}>
            <div className="text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
              Paper {i + 1}
            </div>
            <div className="mt-1 truncate rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[11px]">
              {paper}
            </div>
          </div>
        ))}
      </div>
      <div className="mt-3 overflow-hidden rounded-lg border border-border bg-surface">
        {rows.map(([aspect, a, b]) => (
          <div
            key={aspect}
            className="grid grid-cols-[96px_1fr_1fr] border-b border-border text-[10.5px] last:border-b-0"
          >
            {/* Sized so the widest label ("Architecture", about 73px in Geist)
                fits without spilling; break-words is only a safety net. */}
            <div className="break-words px-2 py-2 text-[9px] font-medium uppercase leading-[17px] tracking-[0.06em] text-muted-foreground">
              {aspect}
            </div>
            <div className="border-l border-border px-2.5 py-2 leading-relaxed text-foreground/85">
              {a}
            </div>
            <div className="border-l border-border px-2.5 py-2 leading-relaxed text-foreground/85">
              {b}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** A generated literature review with its citations and PDF export. */
export function ReportPreview() {
  return (
    <div className="p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="font-mono text-[9px] uppercase tracking-[0.18em] text-muted-foreground">
          Literature Review
        </div>
        <span className="inline-flex shrink-0 items-center rounded-md bg-primary px-2 py-1 text-[10.5px] font-medium text-primary-foreground">
          <Download className="mr-1 h-3 w-3" /> Export PDF
        </span>
      </div>
      <div className="mt-2 text-[13.5px] font-medium leading-snug tracking-tight">
        How did attention-based models change language model pre-training?
      </div>
      <div className="mt-0.5 text-[10px] text-muted-foreground">2 sources · 4 chunks</div>
      <div className="mt-3 text-[12px] font-semibold text-foreground">Overview</div>
      <p className="mt-1 text-[11.5px] leading-relaxed text-foreground/85">
        Vaswani et al. introduced the Transformer, a sequence model based solely on attention.
        Devlin et al. built BERT on a bidirectional Transformer encoder pre-trained on unlabeled
        text.
      </p>
      <div className="mt-3 border-t border-border pt-2.5">
        <div className="text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
          Citations
        </div>
        <div className="mt-1.5 space-y-1">
          {[PAPER_A, PAPER_B].map((paper, i) => (
            <div key={paper} className="flex gap-2 text-[10.5px] text-muted-foreground">
              <span className="font-mono">[{i + 1}]</span>
              <span className="truncate">{paper} · Page 1</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
