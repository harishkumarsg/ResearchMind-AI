import { Link } from "@tanstack/react-router";
import { BrandMark } from "@/components/brand-mark";

// Sections of the public homepage, linked absolutely so the footer works
// from the legal pages too.
const sectionLinks = [
  { href: "/#product", label: "Product" },
  { href: "/#how-it-works", label: "How it works" },
  { href: "/#security", label: "Security" },
  { href: "/#faq", label: "FAQ" },
];

const legalLinks = [
  { href: "/privacy", label: "Privacy Policy" },
  { href: "/terms", label: "Terms of Service" },
  { href: "/contact", label: "Contact" },
];

export function SiteFooter() {
  return (
    <footer className="border-t border-border/60 bg-background">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-6 py-10 md:flex-row md:items-center md:justify-between">
        <div>
          <div className="flex items-center gap-2.5 text-[15px] font-medium tracking-tight text-ink">
            <BrandMark size={24} />
            <span>
              ResearchMind <span className="font-normal text-muted-foreground">Lab</span>
            </span>
          </div>
          <p className="mt-3 max-w-sm text-sm text-muted-foreground">
            AI-assisted research grounded in the papers you provide.
          </p>
        </div>
        <nav aria-label="Footer" className="flex flex-col gap-3 md:items-end">
          <ul className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
            {sectionLinks.map((link) => (
              <li key={link.href}>
                <a className="text-foreground/80 hover:text-foreground" href={link.href}>
                  {link.label}
                </a>
              </li>
            ))}
          </ul>
          <ul className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
            {legalLinks.map((link) => (
              <li key={link.href}>
                <Link className="text-muted-foreground hover:text-foreground" to={link.href}>
                  {link.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </div>
      <div className="border-t border-border/60">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-2 px-6 py-5 text-xs text-muted-foreground">
          <div>© {new Date().getFullYear()} ResearchMind Lab</div>
          <div className="font-mono">researchmindlab.com</div>
        </div>
      </div>
    </footer>
  );
}
