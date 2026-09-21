import { useEffect } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useAuth } from "@/lib/auth-context";

/**
 * Sends a signed-out visitor to the public landing page.
 *
 * Protected pages previously had no guard at all: they rendered whatever
 * they render regardless of session, and only their data queries were
 * gated (`enabled: !!userId`). A signed-out user therefore sat on
 * /paper/<name> looking at an empty shell — after signing out, after
 * navigating straight to the URL, and after pressing Back.
 *
 * One hook covers all three, because each ends with this component
 * mounted while `session` is null:
 *
 *   * sign-out on a protected page — AuthProvider sets session to null,
 *     this effect re-runs and navigates away;
 *   * direct visit while signed out — null on first render;
 *   * Back after sign-out — the route remounts and the effect runs again.
 *
 * `replace: true` so the protected URL does not stay in history: pressing
 * Back from the landing page must not return to it.
 *
 * Deliberately NOT done in signOut(): navigation there would leave the
 * other two paths unguarded, and would couple the auth provider to the
 * router. This is a client-side navigation guard, not an authorization
 * boundary — the real boundary is the backend, which rejects every
 * unauthenticated request and scopes every query to the verified JWT.
 */
export function useRequireAuth(): { isLoading: boolean; isAuthenticated: boolean } {
  const { session, isLoading } = useAuth();
  const navigate = useNavigate();

  const isAuthenticated = !!session;

  useEffect(() => {
    // Wait for the session to resolve, or a normal reload would bounce
    // an authenticated user out before Supabase has rehydrated.
    if (isLoading || isAuthenticated) return;

    navigate({ to: "/", replace: true });
  }, [isLoading, isAuthenticated, navigate]);

  return { isLoading, isAuthenticated };
}
