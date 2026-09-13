/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { AuthProvider, useAuth } from "@/lib/auth-context";
import { queryKeys } from "@/lib/query-keys";

vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: vi.fn(),
  isSupabaseConfigured: vi.fn(() => true),
}));

import { getSupabaseClient } from "@/lib/supabase";

const USER_A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const USER_B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

function sessionFor(id: string, email: string) {
  return { access_token: `tok-${id}`, user: { id, email } };
}

/**
 * AuthProvider now calls useQueryClient(), so it must be rendered inside
 * a QueryClientProvider. Returns the client so tests can inspect the
 * cache directly.
 */
function renderWithQueryClient(ui: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  return { queryClient, ...result };
}

function makeFakeSupabase(initialSession: any) {
  let authStateCallback: ((event: string, session: any) => void) | null = null;
  return {
    auth: {
      getSession: vi.fn().mockResolvedValue({ data: { session: initialSession } }),
      onAuthStateChange: vi.fn((cb: any) => {
        authStateCallback = cb;
        return { data: { subscription: { unsubscribe: vi.fn() } } };
      }),
      signInWithOAuth: vi.fn().mockResolvedValue({}),
      signOut: vi.fn().mockResolvedValue({}),
      _fireAuthStateChange: (event: string, session: any) => authStateCallback?.(event, session),
    },
  };
}

function Probe() {
  const { session, user, isLoading } = useAuth();
  return (
    <div>
      <div data-testid="loading">{String(isLoading)}</div>
      <div data-testid="email">{user?.email ?? "none"}</div>
      <div data-testid="has-session">{String(Boolean(session))}</div>
    </div>
  );
}

describe("AuthProvider / useAuth", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("starts with no session for a logged-out user", async () => {
    const fake = makeFakeSupabase(null);
    (getSupabaseClient as any).mockReturnValue(fake);

    renderWithQueryClient(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("has-session").textContent).toBe("false");
    expect(screen.getByTestId("email").textContent).toBe("none");
  });

  it("reflects an existing session's user", async () => {
    const fake = makeFakeSupabase({
      access_token: "tok",
      user: { email: "person@example.com" },
    });
    (getSupabaseClient as any).mockReturnValue(fake);

    renderWithQueryClient(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("has-session").textContent).toBe("true"));
    expect(screen.getByTestId("email").textContent).toBe("person@example.com");
  });

  it("clears session state (User B starts clean) when a sign-out event fires", async () => {
    const fake = makeFakeSupabase({ access_token: "tok", user: { email: "a@b.com" } });
    (getSupabaseClient as any).mockReturnValue(fake);

    renderWithQueryClient(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("has-session").textContent).toBe("true"));

    act(() => {
      fake.auth._fireAuthStateChange("SIGNED_OUT", null);
    });

    await waitFor(() => expect(screen.getByTestId("has-session").textContent).toBe("false"));
    expect(screen.getByTestId("email").textContent).toBe("none");
  });

  it("does not crash when Supabase is not configured, and reports no session", async () => {
    (getSupabaseClient as any).mockReturnValue(null);

    renderWithQueryClient(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("has-session").textContent).toBe("false");
  });
});

describe("AuthProvider — query cache isolation across users (D1)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  async function mount(initialSession: any) {
    const fake = makeFakeSupabase(initialSession);
    (getSupabaseClient as any).mockReturnValue(fake);

    const { queryClient } = renderWithQueryClient(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    return { fake, queryClient };
  }

  it("does NOT clear the cache when the initial session resolves", async () => {
    const fake = makeFakeSupabase(sessionFor(USER_A_ID, "a@example.com"));
    (getSupabaseClient as any).mockReturnValue(fake);

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(queryKeys.papers(USER_A_ID), ["A-paper.pdf"]);

    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));

    // Requirement 5: the first observed session must not wipe a cache
    // that was just populated for that same user.
    expect(queryClient.getQueryData(queryKeys.papers(USER_A_ID))).toEqual(["A-paper.pdf"]);
  });

  it("clears the cache on SIGNED_OUT", async () => {
    const { fake, queryClient } = await mount(sessionFor(USER_A_ID, "a@example.com"));
    queryClient.setQueryData(queryKeys.papers(USER_A_ID), ["A-paper.pdf"]);

    act(() => {
      fake.auth._fireAuthStateChange("SIGNED_OUT", null);
    });

    await waitFor(() =>
      expect(queryClient.getQueryData(queryKeys.papers(USER_A_ID))).toBeUndefined(),
    );
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it("clears the cache when a different user signs in", async () => {
    const { fake, queryClient } = await mount(sessionFor(USER_A_ID, "a@example.com"));
    queryClient.setQueryData(queryKeys.papers(USER_A_ID), ["A-paper.pdf"]);
    queryClient.setQueryData(queryKeys.stats(USER_A_ID), { total_papers: 7 });

    act(() => {
      fake.auth._fireAuthStateChange("SIGNED_IN", sessionFor(USER_B_ID, "b@example.com"));
    });

    await waitFor(() => expect(queryClient.getQueryCache().getAll()).toHaveLength(0));
  });

  it("does NOT clear the cache on TOKEN_REFRESHED for the same user", async () => {
    const { fake, queryClient } = await mount(sessionFor(USER_A_ID, "a@example.com"));
    queryClient.setQueryData(queryKeys.papers(USER_A_ID), ["A-paper.pdf"]);

    act(() => {
      // Same user id, new token — this fires routinely and must not wipe
      // the cache, or every refresh would cause a full refetch storm.
      fake.auth._fireAuthStateChange("TOKEN_REFRESHED", sessionFor(USER_A_ID, "a@example.com"));
    });

    expect(queryClient.getQueryData(queryKeys.papers(USER_A_ID))).toEqual(["A-paper.pdf"]);
  });

  it("User B can never read User A's cached data after a switch", async () => {
    const { fake, queryClient } = await mount(sessionFor(USER_A_ID, "a@example.com"));
    queryClient.setQueryData(queryKeys.papers(USER_A_ID), ["A-confidential.pdf"]);

    act(() => {
      fake.auth._fireAuthStateChange("SIGNED_IN", sessionFor(USER_B_ID, "b@example.com"));
    });

    await waitFor(() => expect(screen.getByTestId("email").textContent).toBe("b@example.com"));

    // Both halves of the guarantee: the cache was cleared, AND B's key is
    // a different key, so even a late-resolving A response cannot surface.
    expect(queryClient.getQueryData(queryKeys.papers(USER_B_ID))).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.papers(USER_A_ID))).toBeUndefined();
  });

  it("a late response from the previous user cannot leak into the next user's key", async () => {
    const { fake, queryClient } = await mount(sessionFor(USER_A_ID, "a@example.com"));

    act(() => {
      fake.auth._fireAuthStateChange("SIGNED_IN", sessionFor(USER_B_ID, "b@example.com"));
    });

    // Simulate User A's in-flight request resolving AFTER the switch.
    queryClient.setQueryData(queryKeys.papers(USER_A_ID), ["A-late-response.pdf"]);

    // It lands under A's key and is invisible under B's — the structural
    // half of the fix, independent of clearing.
    expect(queryClient.getQueryData(queryKeys.papers(USER_B_ID))).toBeUndefined();
  });
});
