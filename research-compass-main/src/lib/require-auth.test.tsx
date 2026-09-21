/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * Signed-out visitors must not sit on a protected route.
 *
 * Browser QA found that signing out on /paper/<name> left the user on
 * that URL. The cause was not one missing redirect but two gaps: signOut
 * never navigated, and no route guard existed anywhere — protected pages
 * rendered regardless of session, with only their data queries gated.
 *
 * useRequireAuth closes all three entry points, which are the same
 * condition seen at different moments:
 *
 *   * sign-out while on the page   — session becomes null, effect re-runs
 *   * direct visit while signed out — null on first render
 *   * Back after sign-out           — route remounts, effect runs again
 *
 * This is a navigation guard, not an authorization boundary. The backend
 * remains the boundary, and these tests assert the guard does not pretend
 * otherwise.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

const navigate = vi.hoisted(() => vi.fn());
const auth = vi.hoisted(() => ({
  state: { session: null as any, isLoading: false },
}));

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
  Link: ({ children, to }: any) => <a href={to}>{children}</a>,
  createFileRoute: () => (options: any) => ({ options }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    session: auth.state.session,
    user: auth.state.session?.user ?? null,
    isLoading: auth.state.isLoading,
    isConfigured: true,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

import { useRequireAuth } from "@/lib/require-auth";

const SESSION = { access_token: "t", user: { id: "user-1" } };

function Protected() {
  const { isAuthenticated } = useRequireAuth();
  return <div>{isAuthenticated ? "paper workspace" : "redirecting"}</div>;
}

function renderProtected() {
  return render((<Protected />) as ReactNode);
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.state = { session: null, isLoading: false };
});

describe("useRequireAuth", () => {
  it("redirects a signed-out visitor to the public landing page", async () => {
    renderProtected();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/", replace: true }));
  });

  it("replaces history so Back does not return to the protected URL", async () => {
    renderProtected();

    await waitFor(() => expect(navigate).toHaveBeenCalled());
    // replace: true is what stops Back from re-entering the paper route
    // after sign-out.
    expect(navigate.mock.calls[0][0]).toEqual({ to: "/", replace: true });
  });

  it("leaves an authenticated visitor where they are", async () => {
    auth.state = { session: SESSION, isLoading: false };

    renderProtected();

    expect(await screen.findByText("paper workspace")).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("waits for the session to resolve before redirecting", () => {
    // A normal reload starts with isLoading true and no session yet.
    // Redirecting then would bounce an authenticated user out.
    auth.state = { session: null, isLoading: true };

    renderProtected();

    expect(navigate).not.toHaveBeenCalled();
  });

  it("redirects when the session disappears, as it does on sign-out", async () => {
    auth.state = { session: SESSION, isLoading: false };
    const { rerender } = renderProtected();

    expect(await screen.findByText("paper workspace")).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();

    // Sign out: AuthProvider clears the session and the page re-renders.
    auth.state = { session: null, isLoading: false };
    rerender((<Protected />) as ReactNode);

    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/", replace: true }));
  });

  it("redirects on a fresh mount, as it does when Back is pressed", async () => {
    const { unmount } = renderProtected();
    await waitFor(() => expect(navigate).toHaveBeenCalled());
    unmount();
    navigate.mockClear();

    // Back re-mounts the route while still signed out.
    renderProtected();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/", replace: true }));
  });

  it("reports authentication state to the caller", async () => {
    auth.state = { session: SESSION, isLoading: false };
    renderProtected();
    expect(await screen.findByText("paper workspace")).toBeInTheDocument();

    auth.state = { session: null, isLoading: false };
    renderProtected();
    expect(await screen.findAllByText("redirecting")).toBeTruthy();
  });
});
