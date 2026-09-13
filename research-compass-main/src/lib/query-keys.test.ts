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
