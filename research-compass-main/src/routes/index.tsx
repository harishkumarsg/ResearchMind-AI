import { createFileRoute, Link } from "@tanstack/react-router";
import { motion } from "framer-motion";
import {
  ArrowRight,
  Download,
  FileText,
  GitCompare,
  KeyRound,
  Library,
  MessageSquare,
  Quote,
  Search,
  ShieldCheck,
  Upload,
  UserRound,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { SiteHeader } from "@/components/site-header";
import { SiteFooter } from "@/components/site-footer";
import {
  AskWorkspacePreview,
  BrowserFrame,
  ComparePreview,
  LibrarySearchPreview,
  ReportPreview,
} from "@/components/product-preview";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";

const TITLE = "ResearchMind Lab — Research you can verify";
const SUMMARY =
  "Upload papers, search your private research library, ask grounded questions, and generate reports with page-level citations.";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: TITLE },
      { name: "description", content: SUMMARY },
      { property: "og:title", content: TITLE },
      { property: "og:description", content: SUMMARY },
    ],
  }),
  component: Index,
});

// Every statement below describes behaviour the application implements today.

const flow = ["Upload", "Index", "Search", "Ask", "Export"];

const capabilities = [
  {
    icon: Search,
    title: "Search your papers",
    body: "Semantic retrieval across your private research library, showing the paper and page each result came from.",
  },
  {
    icon: Quote,
    title: "Ask with evidence",
    body: "Answers grounded in passages retrieved from your papers, with page citations. Passages that were retrieved but not cited are listed too.",
  },
  {
    icon: GitCompare,
    title: "Compare studies",
    body: "Put two indexed papers side by side and compare their approaches and findings.",
  },
  {
    icon: FileText,
    title: "Generate reports",
    body: "Turn a research question into a structured literature review with citations, and export it as a PDF.",
  },
];

const steps = [
  { n: "01", icon: Upload, title: "Upload papers", body: "Add research papers as PDF files." },
  {
    n: "02",
    icon: Library,
    title: "Build your library",
    body: "Each paper is split into passages and indexed, so the whole library is searchable by meaning.",
  },
  {
    n: "03",
    icon: MessageSquare,
    title: "Ask grounded questions",
    body: "Answers are generated from the most relevant passages and cite the pages they use.",
  },
  {
    n: "04",
    icon: FileText,
    title: "Generate reports",
    body: "Draft a structured literature review from your library, with its citations.",
  },
  { n: "05", icon: Download, title: "Export findings", body: "Download a report as a PDF." },
];

const safeguards = [
  {
    icon: KeyRound,
    title: "Authenticated access",
    body: "You sign in with Google, and every request for library data is checked against your signed-in account.",
  },
  {
    icon: UserRound,
    title: "Private research data",
    body: "Papers, chats and reports belong to your account. Uploaded files are kept in a per-account folder other users cannot access.",
  },
  {
    icon: ShieldCheck,
    title: "Owner-scoped retrieval",
    body: "Search and answers only draw on passages from your own papers, and database rows are restricted per account with row-level security.",
  },
  {
    icon: Quote,
    title: "Citation-backed answers",
    body: "Each answer lists the passages it was given and marks which pages it cited, so you can check the source.",
  },
];

const walkthrough = [
  {
    title: "Library search",
    body: "Semantic results from your own papers, with the page each match came from.",
    path: "/search",
    label: "The ResearchMind Library screen with sample search results",
    Preview: LibrarySearchPreview,
  },
  {
    title: "Compare studies",
    body: "Two indexed papers compared side by side.",
    path: "/compare",
    label: "The ResearchMind Compare screen comparing two sample papers",
    Preview: ComparePreview,
  },
  {
    title: "Literature review",
    body: "A structured review with its citations, ready to export as a PDF.",
    path: "/reports",
    label: "The ResearchMind Literature Review screen with a sample report",
    Preview: ReportPreview,
  },
];

const faqs = [
  {
    q: "How is ResearchMind different from ChatGPT or Perplexity?",
    a: "ResearchMind answers from the papers you upload, not the open web. Each answer is generated from passages retrieved from your library, cites the pages it used, and says so when those passages don't support an answer.",
  },
  {
    q: "What file formats are supported?",
    a: "PDF. Uploaded PDF papers are indexed for search, questions, comparisons and reports.",
  },
  {
    q: "Is my research private?",
    a: "Your papers, chats and reports are tied to your signed-in account, and search only retrieves passages from your own library. To search and answer questions, passages from your papers are processed by the third-party embedding and language-model services that power those features.",
  },
  {
    q: "Can I cite generated answers?",
    a: "Treat an answer as a guide to your sources rather than a source itself. Every answer lists the pages it drew on, so open those pages and cite the original papers.",
  },
  {
    q: "How does search work?",
    a: "When a paper is indexed, it is split into passages and each passage is turned into a vector embedding. Your query is embedded the same way and matched against passages from your own papers, and the closest matches are shown with their paper and page.",
  },
];

// One content grid for every section. SiteHeader and SiteFooter use the same
// max-w-6xl container, so all left and right edges line up.
const container = "mx-auto w-full max-w-6xl px-6";
const sectionPadding = "py-18 md:py-21";

const eyebrow = "text-xs uppercase tracking-[0.18em] text-primary";
const sectionTitle =
  "mt-3 text-balance text-3xl font-medium leading-[1.05] tracking-tight sm:text-4xl md:text-5xl";

// Product and Security cards share identical internals so both sections carry
// the same visual weight.
const cardGrid =
  "grid gap-px overflow-hidden rounded-2xl border border-border bg-border sm:grid-cols-2";
const card = "bg-surface p-6 md:p-7";
const cardTitle = "mt-4 text-lg font-medium tracking-tight";
const cardBody = "mt-2 text-sm leading-relaxed text-muted-foreground";

function Index() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <SiteHeader />

      <main>
        {/* HERO */}
        <section className="relative overflow-hidden">
          <div className="absolute inset-0 grid-paper opacity-50 [mask-image:radial-gradient(ellipse_at_top,black,transparent_70%)]" />
          <div className={`relative ${container} pt-14 pb-18 md:pt-18 md:pb-20`}>
            <motion.div
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
              className="mx-auto max-w-3xl text-center"
            >
              <h1 className="text-balance text-[2.625rem] font-medium leading-[1.04] tracking-tight text-ink sm:text-5xl md:text-7xl md:leading-[1.02]">
                Research you can{" "}
                <span className="italic" style={{ fontFamily: "var(--font-serif)" }}>
                  verify
                </span>
                .
              </h1>
              <p className="mx-auto mt-5 max-w-2xl text-balance text-base leading-relaxed text-muted-foreground md:text-lg">
                {SUMMARY}
              </p>
              <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
                <Button size="lg" className="h-11 px-5 text-[14px]" asChild>
                  <Link to="/dashboard">
                    Start researching <ArrowRight className="ml-1 h-4 w-4" />
                  </Link>
                </Button>
                <Button size="lg" variant="ghost" className="h-11 px-5 text-[14px]" asChild>
                  <a href="#how-it-works">See how it works</a>
                </Button>
              </div>
              <ol
                aria-label="Product flow"
                className="mt-6 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 font-mono text-[12px] text-muted-foreground"
              >
                {flow.map((step, i) => (
                  <li key={step} className="flex items-center gap-2">
                    {i > 0 && (
                      <ArrowRight aria-hidden className="h-3 w-3 text-muted-foreground/60" />
                    )}
                    {step}
                  </li>
                ))}
              </ol>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, y: 24 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.9, delay: 0.15, ease: [0.22, 1, 0.36, 1] }}
              className="mt-10 md:mt-11"
            >
              <BrowserFrame
                path="/ask"
                label="The ResearchMind Ask AI screen with a sample question, answer and page citations"
              >
                <AskWorkspacePreview />
              </BrowserFrame>
              <p className="mt-3 text-center text-xs text-muted-foreground">
                The Ask AI screen, shown with a public sample paper (Vaswani et al., 2017).
              </p>
            </motion.div>
          </div>
        </section>

        {/* PRODUCT */}
        <section id="product" className="scroll-mt-16 border-y border-border/60 bg-surface/40">
          <div className={`${container} ${sectionPadding}`}>
            <div className="max-w-2xl">
              <div className={eyebrow}>Product</div>
              <h2 className={sectionTitle}>One workspace for the papers you rely on.</h2>
            </div>
            <div className={`mt-10 ${cardGrid} lg:grid-cols-4`}>
              {capabilities.map(({ icon: Icon, title, body }) => (
                <div key={title} className={card}>
                  <Icon className="h-5 w-5 text-primary" strokeWidth={1.75} />
                  <h3 className={cardTitle}>{title}</h3>
                  <p className={cardBody}>{body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* HOW IT WORKS */}
        <section id="how-it-works" className="scroll-mt-16">
          <div className={`${container} ${sectionPadding}`}>
            <div className="grid gap-10 lg:grid-cols-[1fr_2fr] lg:gap-16">
              <div>
                <div className={eyebrow}>How it works</div>
                <h2 className={sectionTitle}>From PDFs to a report you can check.</h2>
                <p className="mt-4 max-w-md text-muted-foreground">
                  Five steps. Answers and reports point back to the pages they draw on.
                </p>
              </div>
              <ol className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-surface">
                {steps.map(({ n, icon: Icon, title, body }) => (
                  <li
                    key={n}
                    className="grid grid-cols-[1.75rem_2.25rem_minmax(0,1fr)] items-center gap-x-4 px-5 py-4.5 md:gap-x-5 md:px-6"
                  >
                    <span className="font-mono text-xs tabular-nums text-muted-foreground">
                      {n}
                    </span>
                    <span className="grid h-9 w-9 place-items-center rounded-lg border border-border bg-background">
                      <Icon className="h-4 w-4" strokeWidth={1.75} />
                    </span>
                    <div>
                      <h3 className="text-base font-medium tracking-tight">{title}</h3>
                      <p className="mt-0.5 text-sm text-muted-foreground">{body}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </section>

        {/* WALKTHROUGH */}
        <section id="walkthrough" className="scroll-mt-16 border-y border-border/60 bg-surface/40">
          <div className={`${container} ${sectionPadding}`}>
            <div className="max-w-2xl">
              <div className={eyebrow}>Research workspace</div>
              <h2 className={sectionTitle}>The screens you work in.</h2>
              <p className="mt-4 text-muted-foreground">
                Library search, study comparison and literature reviews as they appear after you
                sign in — shown here with two public sample papers.
              </p>
            </div>
            {/* Frames stretch to the tallest preview so the captions line up. */}
            <div className="mt-10 grid gap-8 lg:grid-cols-3 lg:gap-6">
              {walkthrough.map(({ title, body, path, label, Preview }) => (
                <div key={title} className="grid grid-rows-[1fr_auto]">
                  <BrowserFrame path={path} label={label} className="h-full">
                    <Preview />
                  </BrowserFrame>
                  <div>
                    <h3 className="mt-4 text-base font-medium tracking-tight">{title}</h3>
                    <p className="mt-1 text-sm text-muted-foreground">{body}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* SECURITY */}
        <section id="security" className="scroll-mt-16">
          <div className={`${container} ${sectionPadding}`}>
            <div className="grid gap-10 lg:grid-cols-[1fr_2fr] lg:gap-16">
              <div>
                <div className={eyebrow}>Security</div>
                <h2 className={sectionTitle}>Your library stays yours.</h2>
                <p className="mt-4 max-w-md text-muted-foreground">
                  How access to your research is controlled in the application today.
                </p>
              </div>
              <div className={cardGrid}>
                {safeguards.map(({ icon: Icon, title, body }) => (
                  <div key={title} className={card}>
                    <Icon className="h-5 w-5 text-primary" strokeWidth={1.75} />
                    <h3 className={cardTitle}>{title}</h3>
                    <p className={cardBody}>{body}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* FAQ */}
        <section id="faq" className="scroll-mt-16 border-t border-border/60 bg-surface/40">
          <div className={`${container} ${sectionPadding}`}>
            <div className="grid gap-10 lg:grid-cols-[1fr_2fr] lg:gap-16">
              <div>
                <div className={eyebrow}>FAQ</div>
                <h2 className={sectionTitle}>Common questions.</h2>
              </div>
              <Accordion type="single" collapsible className="w-full">
                {faqs.map((f, i) => (
                  <AccordionItem key={f.q} value={`faq-${i}`} className="border-border">
                    <AccordionTrigger className="py-4 text-left text-base font-medium tracking-tight hover:no-underline">
                      {f.q}
                    </AccordionTrigger>
                    <AccordionContent className="pb-4 text-[15px] leading-relaxed text-muted-foreground">
                      {f.a}
                    </AccordionContent>
                  </AccordionItem>
                ))}
              </Accordion>
            </div>
          </div>
        </section>

        {/* FINAL CTA */}
        <section>
          <div className={`${container} py-16 md:py-18`}>
            <div className="relative overflow-hidden rounded-3xl border border-border bg-ink px-6 py-11 text-center sm:px-10 md:px-16 md:py-14">
              <div className="absolute inset-0 grid-paper opacity-[0.08]" />
              <div className="relative">
                <h2 className="mx-auto max-w-2xl text-balance text-3xl font-medium leading-[1.05] tracking-tight text-background sm:text-4xl md:text-5xl">
                  Start with your papers.
                </h2>
                <p className="mx-auto mt-4 max-w-lg text-balance text-background/75">
                  Sign in with Google, upload a PDF, and ask your first question with page-level
                  citations.
                </p>
                <Button size="lg" variant="secondary" className="mt-7 h-11 px-5" asChild>
                  <Link to="/dashboard">
                    Start researching <ArrowRight className="ml-1 h-4 w-4" />
                  </Link>
                </Button>
              </div>
            </div>
          </div>
        </section>
      </main>

      <SiteFooter />
    </div>
  );
}
