import type { ReactNode } from "react";
import { SiteHeader } from "@/components/site-header";
import { SiteFooter } from "@/components/site-footer";

// Shared chrome for the public legal pages. Same tokens as the homepage,
// narrowed to a single readable column for long-form text.
const container = "mx-auto w-full max-w-3xl px-6";

export const SUPPORT_EMAIL = "harishkumarsg.03@gmail.com";

export function LegalPage({
  title,
  updated,
  children,
}: {
  title: string;
  updated: string;
  children: ReactNode;
}) {
  return (
    <div className="min-h-screen bg-background text-ink">
      <SiteHeader />
      <main className={`${container} py-14 md:py-18`}>
        <h1 className="font-serif text-4xl tracking-tight md:text-[44px]">{title}</h1>
        <p className="mt-3 font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
          Last updated {updated}
        </p>
        {children}
      </main>
      <SiteFooter />
    </div>
  );
}

export function LegalSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-10 border-t border-border/60 pt-8">
      <h2 className="text-lg font-medium tracking-tight">{title}</h2>
      <div className="mt-3 space-y-3 text-[15px] leading-relaxed text-foreground/85">
        {children}
      </div>
    </section>
  );
}

export function LegalList({ items }: { items: ReactNode[] }) {
  return (
    <ul className="space-y-2 pl-5">
      {items.map((item, index) => (
        <li key={index} className="list-disc marker:text-muted-foreground">
          {item}
        </li>
      ))}
    </ul>
  );
}

export function SupportLink() {
  return (
    <a className="text-primary underline underline-offset-4" href={`mailto:${SUPPORT_EMAIL}`}>
      {SUPPORT_EMAIL}
    </a>
  );
}
