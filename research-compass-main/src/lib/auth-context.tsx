import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { Session, User } from "@supabase/supabase-js";
import { useQueryClient } from "@tanstack/react-query";
import { getSupabaseClient, isSupabaseConfigured } from "@/lib/supabase";

interface AuthContextValue {
  session: Session | null;
  user: User | null;
  isLoading: boolean;
  isConfigured: boolean;
  signInWithGoogle: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const configured = isSupabaseConfigured();
  const queryClient = useQueryClient();

  // `undefined` means "no auth state observed yet" — distinct from `null`,
  // which means "observed, and signed out". Without that distinction the
  // very first session would look like a user change and pointlessly wipe
  // a cache that was just populated.
  const previousUserIdRef = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    const supabase = getSupabaseClient();
    if (!supabase) {
      setIsLoading(false);
      return;
    }

    let isMounted = true;

    /**
     * Drops every cached query when the identity actually changes.
     *
     * Keyed off the USER ID, never the event type: TOKEN_REFRESHED fires
     * routinely with the same user, and clearing on it would wipe the
     * cache roughly hourly for no reason. cancelQueries() first so an
     * in-flight request belonging to the outgoing user cannot resolve and
     * repopulate the cache after the clear.
     */
    const syncCacheWithIdentity = (newSession: Session | null) => {
      const newUserId = newSession?.user?.id ?? null;
      const previousUserId = previousUserIdRef.current;
      const isFirstObservation = previousUserId === undefined;

      if (!isFirstObservation && previousUserId !== newUserId) {
        queryClient.cancelQueries();
        queryClient.clear();
      }

      previousUserIdRef.current = newUserId;
    };

    supabase.auth.getSession().then(({ data }) => {
      if (isMounted) {
        syncCacheWithIdentity(data.session);
        setSession(data.session);
        setIsLoading(false);
      }
    });

    // Reacts to sign-in, sign-out, token refresh, and the initial session
    // Supabase detects from the URL after a Google OAuth redirect — no
    // custom callback route is needed for this.
    const { data: subscription } = supabase.auth.onAuthStateChange((_event, newSession) => {
      if (isMounted) {
        syncCacheWithIdentity(newSession);
        setSession(newSession);
        setIsLoading(false);
      }
    });

    return () => {
      isMounted = false;
      subscription.subscription.unsubscribe();
    };
    // queryClient from useQueryClient() is a stable singleton, so this
    // effect still runs exactly once — the subscription is never torn
    // down and re-established.
  }, [queryClient]);

  const signInWithGoogle = async () => {
    const supabase = getSupabaseClient();
    if (!supabase) {
      throw new Error(
        "Supabase is not configured — set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY",
      );
    }
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/dashboard`,
      },
    });
  };

  const signOut = async () => {
    const supabase = getSupabaseClient();
    if (!supabase) return;
    await supabase.auth.signOut();
    setSession(null);
  };

  const value: AuthContextValue = {
    session,
    user: session?.user ?? null,
    isLoading,
    isConfigured: configured,
    signInWithGoogle,
    signOut,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth() must be used within an <AuthProvider>");
  }
  return ctx;
}
