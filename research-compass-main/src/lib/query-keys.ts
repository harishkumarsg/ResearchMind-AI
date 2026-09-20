/**
 * Centralized React Query key factory.
 *
 * Every key for user-specific data embeds the authenticated user's id.
 * This is the structural half of the cache-isolation guarantee: even if a
 * previous user's response resolves late (after sign-out, after clear()),
 * it lands under THAT user's key and the next user reads a different key,
 * so it can never be rendered to them. Clearing the cache on user change
 * is the second, belt-and-braces half — see AuthProvider.
 *
 * Keys live here rather than inline at each call site so a new component
 * cannot quietly reintroduce an unscoped key like ["papers"].
 *
 * The user id never reaches the UI — these keys are internal to React
 * Query and are never rendered.
 *
 * Note that `search` deliberately has its OWN namespace rather than
 * living under ["papers", ...]. React Query matches by key prefix, so the
 * old ["papers", query] shape meant every
 * invalidateQueries(["papers"]) silently discarded all cached search
 * results too.
 */
export const queryKeys = {
  papers: (userId?: string) => ["papers", userId] as const,

  stats: (userId?: string) => ["stats", userId] as const,

  /** The owner's most recent stored report, restored on page load. */
  latestReport: (userId?: string) => ["latest-report", userId] as const,

  paperDetails: (userId: string | undefined, paperName: string) =>
    ["paper-details", userId, paperName] as const,

  search: (userId: string | undefined, query: string) => ["search", userId, query] as const,

  /** Prefix used to invalidate every cached search result at once. */
  searchAll: () => ["search"] as const,
};
