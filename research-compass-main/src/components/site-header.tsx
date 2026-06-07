import { Link } from "@tanstack/react-router";
import { Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";

const nav = [
  { to: "/", label: "Product" },
  { to: "/dashboard", label: "Workspace" },
  { to: "/ask", label: "Ask AI" },
  { to: "/search", label: "Library" },
  { to: "/reports", label: "Reports" },
  { to: "/compare", label: "Compare" },
];

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-40 border-b border-border/60 bg-background/80 backdrop-blur-xl">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-8 px-6">
        <Link to="/" className="flex items-center gap-2 font-medium tracking-tight">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-primary text-primary-foreground">
            <Sparkles className="h-3.5 w-3.5" strokeWidth={2.5} />
          </span>
          <span className="text-[15px]">ResearchMind</span>
          <span className="rounded-sm border border-border px-1.5 py-px text-[10px] font-medium uppercase tracking-wider text-muted-foreground">Beta</span>
        </Link>
        <nav className="hidden flex-1 items-center gap-1 md:flex">
          {nav.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className="rounded-md px-3 py-1.5 text-[13px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              activeOptions={{ exact: item.to === "/" }}
              activeProps={{ className: "rounded-md px-3 py-1.5 text-[13px] text-foreground bg-muted" }}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="sm" className="text-[13px]">Sign in</Button>
          <Button size="sm" className="text-[13px]" asChild>
            <Link to="/dashboard">Start research</Link>
          </Button>
        </div>
      </div>
    </header>
  );
}