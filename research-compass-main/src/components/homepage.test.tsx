/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentType } from "react";

const authState = vi.hoisted(() => ({ session: null as any, user: null as any }));

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to, ...rest }: any) => (
    <a href={to} {...rest}>
      {children}
    </a>
  ),
  createFileRoute: () => (options: any) => ({ options }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    ...authState,
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

import { Route } from "@/routes/index";

const Homepage = Route.options.component as ComponentType;

// Claims the public site made without evidence. None may come back.
const UNSUPPORTED_CLAIMS = [
  /14 European/i,
  /200\+/,
  /\bETH\b/,
  /Z[üu]rich/i,
  /Berlin/i,
  /TU München|\bTUM\b/,
  /\bMIT\b/,
  /Max Planck/i,
  /trusted by/i,
  /testimonial/i,
  /researchmind\.ai/i,
  /\bbeta\b/i,
  /encrypt/i,
  /GDPR|SOC ?2|ISO ?27001|HIPAA/i,
  /\bSSO\b|SCIM|audit log/i,
  /no credit card|free for/i,
  /LaTeX|BibTeX|DOCX|EPUB|arXiv|PubMed|Zotero/i,
  /peer-reviewed build/i,
];

/** All visible text, including every FAQ answer (answers mount only while open). */
function fullPageText(container: HTMLElement) {
  let text = container.textContent ?? "";
  const faq = document.getElementById("faq") as HTMLElement;
  for (const trigger of within(faq).getAllByRole("button")) {
    fireEvent.click(trigger);
    text += ` ${faq.textContent ?? ""}`;
  }
  return text;
}

beforeEach(() => {
  authState.session = null;
  authState.user = null;
});

describe("Public homepage", () => {
  it("leads with the verifiable positioning, the product flow and working calls to action", () => {
    render(<Homepage />);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Research you can verify.");
    expect(
      screen.getByText(
        "Upload papers, search your private research library, ask grounded questions, and generate reports with page-level citations.",
      ),
    ).toBeInTheDocument();

    const flow = screen.getByRole("list", { name: "Product flow" });
    expect(
      within(flow)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["Upload", "Index", "Search", "Ask", "Export"]);

    const startLinks = screen.getAllByRole("link", { name: /start researching/i });
    expect(startLinks.length).toBeGreaterThanOrEqual(3);
    for (const link of startLinks) expect(link).toHaveAttribute("href", "/dashboard");

    expect(screen.getByRole("link", { name: "See how it works" })).toHaveAttribute(
      "href",
      "#how-it-works",
    );
  });

  it("header and footer links all point at sections that exist on the page", () => {
    render(<Homepage />);

    const header = within(screen.getByRole("navigation", { name: "Main" })).getAllByRole("link");
    expect(header.map((link) => link.textContent)).toEqual([
      "Product",
      "How it works",
      "Security",
      "Research",
    ]);

    const footer = within(screen.getByRole("navigation", { name: "Footer" })).getAllByRole("link");
    for (const link of [...header, ...footer]) {
      const href = link.getAttribute("href") ?? "";
      expect(href).toMatch(/^#/);
      expect(document.getElementById(href.slice(1))).not.toBeNull();
    }
  });

  it("contains no unsupported social proof, adoption, location or capability claims", () => {
    const { container } = render(<Homepage />);
    const text = fullPageText(container) + JSON.stringify((Route.options.head as any)());

    for (const claim of UNSUPPORTED_CLAIMS) expect(text).not.toMatch(claim);
    expect(text).not.toMatch(/Built with care/i);
  });

  it("shows the product screens on researchmindlab.com, labelled as sample content", () => {
    const { container } = render(<Homepage />);

    expect(screen.getAllByRole("img").length).toBe(4);
    expect(container.textContent).toContain("researchmindlab.com/ask");
    expect(screen.getByText(/shown with a public sample paper/i)).toBeInTheDocument();
    expect(screen.getByText(/two public sample papers/i)).toBeInTheDocument();
  });

  it("never shows a signed-in visitor's identity on the public page", () => {
    authState.session = {};
    authState.user = { id: "user-a-id", email: "person@example.com" };
    const { container } = render(<Homepage />);

    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(container.textContent).not.toContain("person@example.com");
    expect(container.textContent).not.toContain("user-a-id");
  });

  it("header and footer use the ResearchMind Lab mark rather than a letter tile", () => {
    render(<Homepage />);

    const home = screen.getByRole("link", { name: "ResearchMind Lab home" });
    const footer = screen.getByRole("contentinfo");
    for (const brand of [home, footer]) {
      expect(brand.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    }
    expect(screen.queryByText(/^R$/)).toBeNull();
  });

  it("footer carries the ResearchMind Lab brand and descriptor", () => {
    render(<Homepage />);

    const footer = screen.getByRole("contentinfo");
    expect(footer).toHaveTextContent("ResearchMind Lab");
    expect(footer).toHaveTextContent("AI-assisted research grounded in the papers you provide.");
  });
});
