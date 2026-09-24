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

  /**
   * Every paper with its indexing state, including the uploading,
   * indexing and failed ones that `papers` deliberately omits. Its own
   * namespace rather than a child of ["papers", ...]: React Query
   * matches by prefix, and these two hold different shapes.
   */
  papersDetailed: (userId?: string) => ["papers-detailed", userId] as const,

  /** The owner's own PDF bytes, fetched through the authenticated API. */
  paperFile: (userId: string | undefined, paperId: string) =>
    ["paper-file", userId, paperId] as const,

  /**
   * One paper's stored Paper Intelligence.
   *
   * Keyed on BOTH the authenticated user and the paper: the user id is
   * what stops one account's analysis being served from cache to the
   * next, and the paper id is what stops one paper's analysis appearing
   * under another. Its own namespace rather than a child of
   * ["paper-details", ...] — React Query matches by prefix, and these
   * two hold different shapes.
   */
  paperIntelligence: (userId: string | undefined, paperId: string) =>
    ["paper-intelligence", userId, paperId] as const,

  /**
   * One owner's generated relationship for an UNORDERED pair of papers.
   *
   * The two ids are SORTED into the key. That is load-bearing rather than
   * tidy: the relationship between two papers is one fact and the database
   * stores it as one row (migration 0006 enforces
   * `paper_a_id < paper_b_id`), so A+B and B+A must be one cache entry
   * too. Without the sort, swapping the two selects would cache the same
   * row twice and the setQueryData after a generation would update only
   * the orientation the user happened to be looking at, leaving the other
   * serving a stale copy.
   *
   * Its own namespace rather than a child of ["paper-intelligence", ...]:
   * React Query matches by prefix, these hold different shapes, and
   * regenerating a relationship must not invalidate either paper's stored
   * analysis — the analyses did not change.
   */
  paperRelationship: (
    userId: string | undefined,
    paperAId: string,
    paperBId: string,
  ) => ["paper-relationship", userId, ...[paperAId, paperBId].sort()] as const,

  stats: (userId?: string) => ["stats", userId] as const,

  /** The owner's most recent stored report, restored on page load. */
  latestReport: (userId?: string) => ["latest-report", userId] as const,

  paperDetails: (userId: string | undefined, paperName: string) =>
    ["paper-details", userId, paperName] as const,

  search: (userId: string | undefined, query: string) => ["search", userId, query] as const,

  /** Prefix used to invalidate every cached search result at once. */
  searchAll: () => ["search"] as const,
};
