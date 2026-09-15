/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import type { ComponentType } from "react";

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
    session: null,
    user: null,
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

import { Route as PrivacyRoute } from "@/routes/privacy";
import { Route as TermsRoute } from "@/routes/terms";
import { Route as ContactRoute } from "@/routes/contact";

const SUPPORT_EMAIL = "harishkumarsg.03@gmail.com";

// Claims the pages must never make: nothing here is verifiable, and a privacy
// page is the worst possible place to overstate what the service guarantees.
const UNSUPPORTED_CLAIMS = [
  /SOC ?2|ISO ?27001|HIPAA|PCI ?DSS/i,
  /end-to-end encrypt/i,
  /military[- ]grade/i,
  /we never store/i,
  /100% (secure|private)/i,
  /(guarantee|guaranteed) (security|privacy|uptime)/i,
  // Infrastructure regions are not independently verified, so they must not
  // be published as fact.
  /Mumbai|Frankfurt|ap-south-\d|eu-central-\d/i,
  // A universal statutory deadline is a promise the service cannot make to
  // every user, whatever law applies to them.
  /within one month/i,
];

const PrivacyPage = PrivacyRoute.options.component as ComponentType;
const TermsPage = TermsRoute.options.component as ComponentType;
const ContactPage = ContactRoute.options.component as ComponentType;

const pages: [string, ComponentType][] = [
  ["privacy", PrivacyPage],
  ["terms", TermsPage],
  ["contact", ContactPage],
];

describe("public legal pages", () => {
  for (const [name, Page] of pages) {
    it(`${name} renders with the shared chrome, a heading and the support email`, () => {
      const { container } = render(<Page />);

      expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
      expect(screen.getByRole("contentinfo")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "ResearchMind Lab home" })).toHaveAttribute(
        "href",
        "/",
      );
      expect(container.textContent).toContain("Last updated");
      expect(container.querySelector(`a[href="mailto:${SUPPORT_EMAIL}"]`)).not.toBeNull();
    });

    it(`${name} makes no unverifiable security or privacy claim`, () => {
      const { container } = render(<Page />);
      const text = container.textContent ?? "";

      for (const claim of UNSUPPORTED_CLAIMS) expect(text).not.toMatch(claim);
    });

    it(`${name} links to the other legal pages from the footer`, () => {
      render(<Page />);

      const footer = within(screen.getByRole("navigation", { name: "Footer" }));
      for (const [label, href] of [
        ["Privacy Policy", "/privacy"],
        ["Terms of Service", "/terms"],
        ["Contact", "/contact"],
      ]) {
        expect(footer.getByRole("link", { name: label })).toHaveAttribute("href", href);
      }
    });
  }
});

describe("Privacy Policy content required for Google OAuth review", () => {
  it("names the controller and states the exact Google scopes requested", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    expect(text).toContain("Harish Kumar S G");
    expect(text).toMatch(/email/);
    expect(text).toMatch(/profile/);
    expect(text).toMatch(/No other Google scopes are requested/i);
    expect(text).toMatch(/Gmail, Drive,\s*Calendar/i);
  });

  it("names every third party that receives user content", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    for (const provider of ["Supabase", "Qdrant", "Voyage AI", "Groq", "Vercel", "Railway"]) {
      expect(text).toContain(provider);
    }
    expect(text).toMatch(/Google Fonts/);
  });

  it("describes embedding across every feature that uses it, not just search", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    // encode_query() runs for search, ask, compare, research, paper-details
    // and summarize — the disclosure has to cover all of them.
    expect(text).toMatch(/a paper comparison/i);
    expect(text).toMatch(/report topic/i);
    expect(text).toMatch(/details or summary/i);
  });

  it("attributes IP and browser data to the infrastructure, not to the application", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    expect(text).toMatch(/those providers may receive standard request information/i);
    expect(text).toMatch(/application itself does not read or store either/i);
    // It must not be listed among the things the service itself collects.
    expect(text).not.toMatch(/Basic technical records that any web service receives/i);
  });

  it("says fonts load on every page, and that logs can contain submitted text", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    expect(text).toMatch(/Every page, including the signed-in application/i);
    expect(text).not.toMatch(/Public pages load typefaces/i);
    expect(text).toMatch(/Diagnostic logs on the API host/i);
    expect(text).toMatch(/a search query or a\s*report topic/i);
  });

  it("states retention honestly, including what paper deletion leaves behind", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    expect(text).toMatch(/no automatic expiry/i);
    expect(text).toMatch(/does\s*not\s*remove reports/i);
    expect(text).toMatch(/chat\s*history/i);
    expect(text).toMatch(/account deletion/i);
  });

  it("describes full deletion as manual, with no self-service feature implied", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    expect(text).toMatch(/no\s*self-service button/i);
    expect(text).toMatch(/carried out manually/i);
    // Only claimed because the audit confirmed each store can actually be
    // cleared by hand: Postgres cascades from the auth user, and Storage and
    // Qdrant are removed per account.
    expect(text).toMatch(/uploaded PDF files/i);
    expect(text).toMatch(/embeddings/i);
  });

  it("frames data rights conditionally rather than asserting the law applies", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    expect(text).toMatch(/If an applicable privacy law gives you rights/i);
    expect(text).toMatch(/depends on the law that applies to you/i);
    expect(text).toMatch(/erased/i);
    expect(text).toMatch(/portable/i);
    expect(text).toMatch(/data protection authority/i);
    // GDPR may be referenced, but never as a settled conclusion about the reader.
    expect(text).not.toMatch(/(we are|I am|is) GDPR[- ]compliant/i);
    expect(text).not.toMatch(/the UK and EU GDPR give you/i);
  });

  it("states no infrastructure region as fact", () => {
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? "";

    expect(text).not.toMatch(/Mumbai|Frankfurt|ap-south-\d|eu-central-\d/i);
    // The honest version: providers operate in various countries.
    expect(text).toMatch(/outside the country you\s*are in/i);
  });
});

describe("Terms of Service content", () => {
  it("states age, copyright responsibility, AI limits and governing law", () => {
    const { container } = render(<TermsPage />);
    const text = container.textContent ?? "";

    expect(text).toMatch(/at least 16 years old/i);
    expect(text).toMatch(/necessary rights or permission to upload/i);
    expect(text).toMatch(/confidently wrong|can be incomplete/i);
    expect(text).toMatch(/laws of India/i);
    expect(text).toMatch(/Tamil Nadu/);
  });

  it("does not let Google sign-in stand in for copyright permission", () => {
    const { container } = render(<TermsPage />);
    const text = container.textContent ?? "";

    expect(text).toMatch(/grants no rights over any\s*paper/i);
    expect(text).toMatch(/permission you need from the rights holder/i);
  });

  it("does not promise uptime or permanence", () => {
    const { container } = render(<TermsPage />);
    const text = container.textContent ?? "";

    expect(text).toMatch(/no\s*uptime guarantee/i);
    expect(text).toMatch(/as is/i);
  });
});
