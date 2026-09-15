import { Link } from "@tanstack/react-router";
import { BrandMark } from "@/components/brand-mark";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth-context";

// Sections of the public homepage. Paths are absolute ("/#product", not
// "#product") because the legal pages render this same header, where a bare
// fragment would point at a section that is not on the page.
const nav = [
  { href: "/#product", label: "Product" },
  { href: "/#how-it-works", label: "How it works" },
  { href: "/#security", label: "Security" },
  { href: "/#walkthrough", label: "Research" },
];

export function SiteHeader() {
  const { session, isLoading, signInWithGoogle, signOut } = useAuth();

  return (
    <header className="sticky top-0 z-40 border-b border-border/60 bg-background/80 backdrop-blur-xl">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-4 px-6 md:gap-8">
        <Link
          to="/"
          aria-label="ResearchMind Lab home"
          className="flex items-center gap-2.5 tracking-tight"
        >
          <BrandMark size={24} />
          <span className="hidden text-[15px] font-medium text-ink sm:inline">
            ResearchMind <span className="font-normal text-muted-foreground">Lab</span>
          </span>
        </Link>
        <nav aria-label="Main" className="hidden flex-1 items-center gap-1 md:flex">
          {nav.map((item) => (
            <a
              key={item.href}
              href={item.href}
              className="rounded-md px-3 py-1.5 text-[13px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              {item.label}
            </a>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-2">
          {!isLoading && session ? (
            <Button variant="ghost" size="sm" className="text-[13px]" onClick={() => signOut()}>
              Sign out
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="text-[13px]"
              onClick={() => signInWithGoogle()}
              disabled={isLoading}
            >
              Sign in
            </Button>
          )}
          <Button size="sm" className="text-[13px]" asChild>
            <Link to="/dashboard">Start researching</Link>
          </Button>
        </div>
      </div>
    </header>
  );
}
