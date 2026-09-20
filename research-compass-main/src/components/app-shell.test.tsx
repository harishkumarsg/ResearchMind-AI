/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ComponentType, ReactNode } from "react";

const router = vi.hoisted(() => ({ navigate: vi.fn(), pathname: "/dashboard" }));

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to }: any) => <a href={to}>{children}</a>,
  useNavigate: () => router.navigate,
  useRouterState: ({ select }: any) => select({ location: { pathname: router.pathname } }),
  createFileRoute: () => (options: any) => ({ options }),
}));

// Signed out, so neither the sidebar papers query nor the search query runs.
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    session: null,
    user: null,
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

vi.mock("@/lib/api", () => ({
  getPapers: vi.fn().mockResolvedValue([]),
  // AppShell reads the detailed list so it can show uploading/indexing/
  // failed papers alongside the indexed ones.
  getPapersDetailed: vi.fn().mockResolvedValue([]),
  deletePaper: vi.fn(),
  searchPapers: vi.fn().mockResolvedValue([]),
}));

import { AppShell, LIBRARY_SEARCH_INPUT_ID } from "@/components/app-shell";
import { Route as SearchRoute } from "@/routes/search";

function setPlatform(platform: string) {
  Object.defineProperty(window.navigator, "platform", { value: platform, configurable: true });
}

function withQueryClient(ui: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

function renderShell(children?: ReactNode) {
  return withQueryClient(<AppShell title="Test">{children}</AppShell>);
}

function renderSearchPage() {
  const SearchPage = SearchRoute.options.component as ComponentType;
  return withQueryClient(<SearchPage />);
}

const searchButton = () => screen.getByRole("button", { name: /search/i });
const libraryInput = () => document.getElementById(LIBRARY_SEARCH_INPUT_ID);

// fireEvent returns false when the handler called preventDefault().
const pressKey = (init: KeyboardEventInit) => fireEvent.keyDown(document.body, init);

beforeEach(() => {
  router.navigate.mockReset();
  router.pathname = "/dashboard";
  setPlatform("Win32");
});

afterEach(() => {
  // Drop the own property so jsdom's Navigator.prototype getter applies again.
  delete (window.navigator as any).platform;
});

describe("AppShell sidebar Search control", () => {
  it("shows the Ctrl K hint on non-Mac platforms", () => {
    renderShell();
    expect(searchButton()).toHaveTextContent("Ctrl K");
    expect(searchButton()).not.toHaveTextContent("⌘K");
    expect(searchButton()).toHaveAttribute("aria-keyshortcuts", "Control+K");
  });

  it("shows the ⌘K hint on macOS", () => {
    setPlatform("MacIntel");
    renderShell();
    expect(searchButton()).toHaveTextContent("⌘K");
    expect(searchButton()).not.toHaveTextContent("Ctrl K");
    expect(searchButton()).toHaveAttribute("aria-keyshortcuts", "Meta+K");
  });

  it("navigates to /search when clicked from another page", () => {
    renderShell();
    fireEvent.click(searchButton());
    expect(router.navigate).toHaveBeenCalledTimes(1);
    expect(router.navigate).toHaveBeenCalledWith({ to: "/search" });
  });

  it("focuses the Library input instead of navigating when already on /search", () => {
    router.pathname = "/search";
    renderShell(<input id={LIBRARY_SEARCH_INPUT_ID} />);
    expect(libraryInput()).not.toHaveFocus();

    fireEvent.click(searchButton());

    expect(libraryInput()).toHaveFocus();
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it.each(["Win32", "Linux x86_64"])(
    "Ctrl+K navigates to /search and prevents the browser default on %s",
    (platform) => {
      setPlatform(platform);
      renderShell();
      expect(pressKey({ key: "k", ctrlKey: true })).toBe(false);
      expect(router.navigate).toHaveBeenCalledWith({ to: "/search" });
    },
  );

  it("Cmd+K navigates to /search and prevents the browser default on macOS", () => {
    setPlatform("MacIntel");
    renderShell();
    expect(pressKey({ key: "k", metaKey: true })).toBe(false);
    expect(router.navigate).toHaveBeenCalledWith({ to: "/search" });
  });

  it("the shortcut focuses the Library input instead of navigating when already on /search", () => {
    router.pathname = "/search";
    renderShell(<input id={LIBRARY_SEARCH_INPUT_ID} />);

    expect(pressKey({ key: "K", ctrlKey: true })).toBe(false);

    expect(libraryInput()).toHaveFocus();
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it.each([
    ["Win32", { key: "k", metaKey: true }],
    ["MacIntel", { key: "k", ctrlKey: true }],
    ["Win32", { key: "k" }],
    ["Win32", { key: "k", ctrlKey: true, shiftKey: true }],
    ["Win32", { key: "k", ctrlKey: true, altKey: true }],
    ["Win32", { key: "j", ctrlKey: true }],
  ])("ignores %s %o", (platform, init) => {
    setPlatform(platform);
    renderShell();
    expect(pressKey(init)).toBe(true);
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it("stops listening for the shortcut after unmount", () => {
    const { unmount } = renderShell();
    unmount();
    expect(pressKey({ key: "k", ctrlKey: true })).toBe(true);
    expect(router.navigate).not.toHaveBeenCalled();
  });
});

describe("AppShell brand", () => {
  it("uses the ResearchMind mark in the sidebar instead of a letter tile", () => {
    renderShell();

    const brand = screen.getByRole("link", { name: "ResearchMind" });
    expect(brand.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(brand).toHaveTextContent(/^ResearchMind$/);
  });
});

describe("Library search input", () => {
  beforeEach(() => {
    router.pathname = "/search";
  });

  it("has the stable id and is focused when the page mounts", () => {
    renderSearchPage();
    expect(libraryInput()).toBe(screen.getByPlaceholderText(/search papers/i));
    expect(libraryInput()).toHaveFocus();
  });

  it("is re-focused by the sidebar click and by Ctrl+K while on /search", () => {
    renderSearchPage();
    const signIn = screen.getByRole("button", { name: /sign in/i });

    signIn.focus();
    expect(libraryInput()).not.toHaveFocus();
    fireEvent.click(searchButton());
    expect(libraryInput()).toHaveFocus();

    signIn.focus();
    expect(libraryInput()).not.toHaveFocus();
    expect(pressKey({ key: "k", ctrlKey: true })).toBe(false);
    expect(libraryInput()).toHaveFocus();

    expect(router.navigate).not.toHaveBeenCalled();
  });
});
