import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Public, browser-safe values only (Vite's VITE_ prefix ships these to the
// client bundle by design). Never put the service_role key or any other
// secret here — this file is bundled into code that runs in every visitor's
// browser.
const SUPABASE_URL = import.meta.env?.VITE_SUPABASE_URL ?? "";
const SUPABASE_ANON_KEY = import.meta.env?.VITE_SUPABASE_ANON_KEY ?? "";

let _client: SupabaseClient | null = null;
let _warnedMissingConfig = false;

/**
 * Lazily constructs the Supabase browser client. Returns null (rather than
 * throwing at import time) if VITE_SUPABASE_URL/VITE_SUPABASE_ANON_KEY
 * aren't configured yet, so the rest of the app can still render and show
 * a clear "auth not configured" state instead of a hard crash.
 */
export function getSupabaseClient(): SupabaseClient | null {
  if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
    if (!_warnedMissingConfig) {
      console.warn(
        "Supabase is not configured: set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY " +
          "(see .env.example). Auth features are disabled until these are set.",
      );
      _warnedMissingConfig = true;
    }
    return null;
  }

  if (!_client) {
    _client = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    });
  }

  return _client;
}

export function isSupabaseConfigured(): boolean {
  return Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);
}
