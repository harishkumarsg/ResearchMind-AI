export function SiteFooter() {
  return (
    <footer className="border-t border-border/60 bg-background">
      <div className="mx-auto grid max-w-[1400px] gap-12 px-6 py-16 md:grid-cols-5">
        <div className="md:col-span-2">
          <div className="flex items-center gap-2 text-[15px] font-medium">
            <span className="grid h-7 w-7 place-items-center rounded-md bg-primary text-primary-foreground text-xs">R</span>
            ResearchMind
          </div>
          <p className="mt-4 max-w-sm text-sm text-muted-foreground">
            Grounded AI for researchers. Built with care in Zürich and Berlin.
          </p>
        </div>
        {[
          { h: "Product", items: ["Workspace", "Ask AI", "Library", "Reports", "Compare"] },
          { h: "Research", items: ["Methodology", "Benchmarks", "Changelog", "Roadmap"] },
          { h: "Company", items: ["About", "Careers", "Press", "Contact"] },
        ].map((col) => (
          <div key={col.h}>
            <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">{col.h}</div>
            <ul className="mt-4 space-y-2.5 text-sm">
              {col.items.map((i) => (
                <li key={i}><a className="text-foreground/80 hover:text-foreground" href="#">{i}</a></li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <div className="border-t border-border/60">
        <div className="mx-auto flex max-w-[1400px] items-center justify-between px-6 py-5 text-xs text-muted-foreground">
          <div>© 2026 ResearchMind Labs</div>
          <div className="font-mono">v0.9.2 — peer-reviewed build</div>
        </div>
      </div>
    </footer>
  );
}