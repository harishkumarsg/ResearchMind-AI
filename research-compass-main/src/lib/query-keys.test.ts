import { describe, it, expect } from "vitest";
import { queryKeys } from "@/lib/query-keys";

const USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

/** React Query matches by key PREFIX — this mirrors that semantic. */
function isPrefixOf(prefix: readonly unknown[], key: readonly unknown[]) {
  return prefix.every((segment, i) => key[i] === segment);
}

describe("queryKeys — every user-specific key carries the user id", () => {
  it("embeds the user id in papers and stats keys", () => {
    expect(queryKeys.papers(USER_A)).toContain(USER_A);
    expect(queryKeys.stats(USER_A)).toContain(USER_A);
  });

  it("embeds the user id in paper-details and search keys", () => {
    expect(queryKeys.paperDetails(USER_A, "paper.pdf")).toContain(USER_A);
    expect(queryKeys.search(USER_A, "vision models")).toContain(USER_A);
  });

  it("produces different keys for different users — the isolation guarantee", () => {
    expect(queryKeys.papers(USER_A)).not.toEqual(queryKeys.papers(USER_B));
    expect(queryKeys.stats(USER_A)).not.toEqual(queryKeys.stats(USER_B));
    expect(queryKeys.paperDetails(USER_A, "p.pdf")).not.toEqual(
      queryKeys.paperDetails(USER_B, "p.pdf"),
    );
    expect(queryKeys.search(USER_A, "q")).not.toEqual(queryKeys.search(USER_B, "q"));
  });

  it("distinguishes a signed-out (undefined) user from a real user", () => {
    expect(queryKeys.papers(undefined)).not.toEqual(queryKeys.papers(USER_A));
  });

  it("still varies by the non-user argument", () => {
    expect(queryKeys.search(USER_A, "alpha")).not.toEqual(queryKeys.search(USER_A, "beta"));
    expect(queryKeys.paperDetails(USER_A, "a.pdf")).not.toEqual(
      queryKeys.paperDetails(USER_A, "b.pdf"),
    );
  });
});

describe("queryKeys — search no longer shares the papers prefix", () => {
  it("search keys are NOT under the papers namespace", () => {
    const search = queryKeys.search(USER_A, "vision models");

    // The regression this guards: search used to be ["papers", query], so
    // invalidateQueries(["papers"]) silently discarded every cached
    // search result as collateral damage.
    expect(isPrefixOf(["papers"], search)).toBe(false);
    expect(search[0]).toBe("search");
  });

  it("invalidating the papers key cannot match a search key", () => {
    const papersKey = queryKeys.papers(USER_A);
    const searchKey = queryKeys.search(USER_A, "q");

    expect(isPrefixOf(papersKey, searchKey)).toBe(false);
  });

  it("searchAll() is a prefix of every user's search keys, so it can invalidate them all", () => {
    expect(isPrefixOf(queryKeys.searchAll(), queryKeys.search(USER_A, "q"))).toBe(true);
    expect(isPrefixOf(queryKeys.searchAll(), queryKeys.search(USER_B, "other"))).toBe(true);
  });

  it("searchAll() does not match papers, stats, or paper-details keys", () => {
    const all = queryKeys.searchAll();

    expect(isPrefixOf(all, queryKeys.papers(USER_A))).toBe(false);
    expect(isPrefixOf(all, queryKeys.stats(USER_A))).toBe(false);
    expect(isPrefixOf(all, queryKeys.paperDetails(USER_A, "p.pdf"))).toBe(false);
  });
});

describe("queryKeys.paperRelationship — one unordered pair, one cache entry", () => {
  // PAPER_LOW sorts below PAPER_HIGH, so it is the canonical first id.
  const PAPER_LOW = "11111111-1111-1111-1111-111111111111";
  const PAPER_HIGH = "22222222-2222-2222-2222-222222222222";

  it("produces the SAME key whichever order the pair is given in", () => {
    // Load-bearing, not tidy: the database stores one row per unordered
    // pair, so swapping the two selects must not cache it twice — and the
    // setQueryData after a generation must reach both orientations.
    expect(queryKeys.paperRelationship(USER_A, PAPER_LOW, PAPER_HIGH)).toEqual(
      queryKeys.paperRelationship(USER_A, PAPER_HIGH, PAPER_LOW),
    );
  });

  it("sorts the ids into the key, matching the canonical pair in migration 0006", () => {
    expect(queryKeys.paperRelationship(USER_A, PAPER_HIGH, PAPER_LOW)).toEqual([
      "paper-relationship",
      USER_A,
      PAPER_LOW,
      PAPER_HIGH,
    ]);
  });

  it("embeds the user id, so one account cannot read another's relationship", () => {
    expect(queryKeys.paperRelationship(USER_A, PAPER_LOW, PAPER_HIGH)).toContain(USER_A);
    expect(queryKeys.paperRelationship(USER_A, PAPER_LOW, PAPER_HIGH)).not.toEqual(
      queryKeys.paperRelationship(USER_B, PAPER_LOW, PAPER_HIGH),
    );
  });

  it("distinguishes a signed-out reader from a real user", () => {
    expect(queryKeys.paperRelationship(undefined, PAPER_LOW, PAPER_HIGH)).not.toEqual(
      queryKeys.paperRelationship(USER_A, PAPER_LOW, PAPER_HIGH),
    );
  });

  it("varies by the pair", () => {
    const other = "33333333-3333-3333-3333-333333333333";
    expect(queryKeys.paperRelationship(USER_A, PAPER_LOW, PAPER_HIGH)).not.toEqual(
      queryKeys.paperRelationship(USER_A, PAPER_LOW, other),
    );
  });

  it("has its own namespace, so it cannot be invalidated by paper-intelligence", () => {
    // Regenerating a relationship must not discard either paper's stored
    // analysis, and re-reading an analysis must not discard the
    // relationship: neither key is a prefix of the other.
    const relationship = queryKeys.paperRelationship(USER_A, PAPER_LOW, PAPER_HIGH);
    const intelligence = queryKeys.paperIntelligence(USER_A, PAPER_LOW);

    expect(relationship[0]).toBe("paper-relationship");
    expect(isPrefixOf(intelligence, relationship)).toBe(false);
    expect(isPrefixOf(relationship, intelligence)).toBe(false);
    expect(isPrefixOf(["papers"], relationship)).toBe(false);
  });
});
