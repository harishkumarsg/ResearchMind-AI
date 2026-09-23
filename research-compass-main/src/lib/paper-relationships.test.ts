/**
 * Deterministic cross-paper comparability.
 *
 * Two properties carry the weight, and both are about attribution
 * rather than logic:
 *
 *   * a paper's content must never appear on the other paper's side.
 *     A comparison that quietly swaps or copies claims looks
 *     authoritative while being wrong, which is worse than no
 *     comparison at all;
 *   * an ungrounded side must never be promoted to "comparable".
 *     Corrupt or absent data has to degrade toward "neither", because
 *     the alternative is inventing a relationship.
 *
 * Pure and offline: no provider, no network, no mocks needed at all.
 */
import { describe, it, expect } from "vitest";
import { compareIntelligence, type SectionComparison } from "@/lib/paper-relationships";
import {
  INTELLIGENCE_SECTION_ORDER,
  type IntelligenceSection,
  type IntelligenceSectionName,
  type PaperIntelligenceSections,
} from "@/lib/api";

function answered(marker: string, page = 3, chunkId = 0): IntelligenceSection {
  return {
    status: "answered",
    summary: `${marker} summary`,
    evidence: [{ page, chunk_id: chunkId, quote: null }],
  };
}

function notSpecified(): IntelligenceSection {
  return { status: "not_specified", summary: null, evidence: [] };
}

/** A whole paper whose every section is answered, marked for tracing. */
function paper(marker: string): PaperIntelligenceSections {
  const sections = {} as PaperIntelligenceSections;
  for (const name of INTELLIGENCE_SECTION_ORDER) {
    sections[name] = answered(`${marker} ${name}`);
  }
  return sections;
}

function find(results: SectionComparison[], name: IntelligenceSectionName) {
  const hit = results.find((r) => r.section === name);
  if (!hit) throw new Error(`missing section ${name}`);
  return hit;
}

const verdicts = (results: SectionComparison[]) => results.map((r) => r.verdict);

describe("Shape and ordering", () => {
  it("always returns exactly ten results", () => {
    expect(compareIntelligence(paper("A"), paper("B"))).toHaveLength(10);
    expect(compareIntelligence(null, null)).toHaveLength(10);
    expect(compareIntelligence(paper("A"), null)).toHaveLength(10);
  });

  it("returns them in the canonical section order", () => {
    const results = compareIntelligence(paper("A"), paper("B"));
    expect(results.map((r) => r.section)).toEqual([...INTELLIGENCE_SECTION_ORDER]);
  });

  it("returns the same order regardless of input", () => {
    for (const [a, b] of [
      [null, null],
      [paper("A"), null],
      [null, paper("B")],
    ] as const) {
      expect(compareIntelligence(a, b).map((r) => r.section)).toEqual([
        ...INTELLIGENCE_SECTION_ORDER,
      ]);
    }
  });

  it("emits no relationship field — that is a later step", () => {
    const results = compareIntelligence(paper("A"), paper("B"));
    for (const result of results) {
      expect(Object.keys(result).sort()).toEqual([
        "paper_a_claim",
        "paper_b_claim",
        "section",
        "verdict",
      ]);
      expect("relation" in result).toBe(false);
      expect("relationship" in result).toBe(false);
    }
  });

  it("produces no score, rank or ordering", () => {
    const results = compareIntelligence(paper("A"), paper("B"));
    const encoded = JSON.stringify(results).toLowerCase();
    for (const banned of ["score", "rank", "winner", "better", "confidence"]) {
      expect(encoded).not.toContain(banned);
    }
  });
});

describe("Verdicts", () => {
  it("both answered gives comparable", () => {
    expect(verdicts(compareIntelligence(paper("A"), paper("B")))).toEqual(
      Array(10).fill("comparable"),
    );
  });

  it("both null gives ten neither", () => {
    expect(verdicts(compareIntelligence(null, null))).toEqual(Array(10).fill("neither"));
  });

  it("A answered and B null gives a_only", () => {
    expect(verdicts(compareIntelligence(paper("A"), null))).toEqual(
      Array(10).fill("a_only"),
    );
  });

  it("A null and B answered gives b_only", () => {
    expect(verdicts(compareIntelligence(null, paper("B")))).toEqual(
      Array(10).fill("b_only"),
    );
  });

  it("treats undefined exactly like null", () => {
    expect(verdicts(compareIntelligence(undefined, paper("B")))).toEqual(
      Array(10).fill("b_only"),
    );
    expect(verdicts(compareIntelligence(paper("A"), undefined))).toEqual(
      Array(10).fill("a_only"),
    );
  });

  it("A not_specified and B answered gives b_only", () => {
    const a = paper("A");
    a.dataset = notSpecified();

    expect(find(compareIntelligence(a, paper("B")), "dataset").verdict).toBe("b_only");
  });

  it("A answered and B not_specified gives a_only", () => {
    const b = paper("B");
    b.dataset = notSpecified();

    expect(find(compareIntelligence(paper("A"), b), "dataset").verdict).toBe("a_only");
  });

  it("both not_specified gives neither", () => {
    const a = paper("A");
    const b = paper("B");
    a.reproducibility = notSpecified();
    b.reproducibility = notSpecified();

    expect(find(compareIntelligence(a, b), "reproducibility").verdict).toBe("neither");
  });

  it("handles a mixed ten-section spread", () => {
    const a = paper("A");
    const b = paper("B");
    a.dataset = notSpecified(); // -> b_only
    b.limitations = notSpecified(); // -> a_only
    a.reproducibility = notSpecified(); // -> neither
    b.reproducibility = notSpecified();

    const results = compareIntelligence(a, b);

    expect(find(results, "research_problem").verdict).toBe("comparable");
    expect(find(results, "dataset").verdict).toBe("b_only");
    expect(find(results, "limitations").verdict).toBe("a_only");
    expect(find(results, "reproducibility").verdict).toBe("neither");
    // The other six are untouched.
    expect(verdicts(results).filter((v) => v === "comparable")).toHaveLength(7);
  });

  it("a not_specified section is never comparable even against itself", () => {
    const a = paper("A");
    const b = paper("B");
    a.methodology = notSpecified();
    b.methodology = notSpecified();

    expect(find(compareIntelligence(a, b), "methodology").verdict).not.toBe("comparable");
  });
});

describe("Preservation", () => {
  it("passes the original section object through by reference", () => {
    const a = paper("A");
    const b = paper("B");

    const result = find(compareIntelligence(a, b), "methodology");

    // Identity, not deep equality: nothing was rebuilt or rewritten.
    expect(result.paper_a_claim).toBe(a.methodology);
    expect(result.paper_b_claim).toBe(b.methodology);
  });

  it("preserves the summary exactly", () => {
    const a = paper("A");
    a.key_results = answered("Precision rose to 0.94 on the held-out split");

    const result = find(compareIntelligence(a, paper("B")), "key_results");

    expect(result.paper_a_claim?.summary).toBe(
      "Precision rose to 0.94 on the held-out split summary",
    );
  });

  it("preserves evidence identity, page and chunk_id alike", () => {
    const a = paper("A");
    a.dataset = {
      status: "answered",
      summary: "A dataset.",
      evidence: [
        { page: 3, chunk_id: 0, quote: null },
        { page: 3, chunk_id: 1, quote: "A stored passage." },
        { page: 7, chunk_id: 0, quote: null },
      ],
    };

    const claim = find(compareIntelligence(a, paper("B")), "dataset").paper_a_claim;

    expect(claim?.evidence.map((e) => [e.page, e.chunk_id])).toEqual([
      [3, 0],
      [3, 1],
      [7, 0],
    ]);
    // The same chunk_id on two different pages stays distinguishable.
    expect(claim?.evidence[1].quote).toBe("A stored passage.");
  });

  it("does not mutate its arguments", () => {
    const a = paper("A");
    const b = paper("B");
    const beforeA = JSON.stringify(a);
    const beforeB = JSON.stringify(b);

    compareIntelligence(a, b);

    expect(JSON.stringify(a)).toBe(beforeA);
    expect(JSON.stringify(b)).toBe(beforeB);
  });

  it("is deterministic", () => {
    const a = paper("A");
    const b = paper("B");

    expect(JSON.stringify(compareIntelligence(a, b))).toBe(
      JSON.stringify(compareIntelligence(a, b)),
    );
  });
});

describe("Attribution — content never crosses papers", () => {
  it("never puts A's content on B's side", () => {
    const results = compareIntelligence(paper("ALPHA"), paper("BETA"));

    for (const result of results) {
      expect(result.paper_b_claim?.summary ?? "").not.toContain("ALPHA");
      expect(result.paper_a_claim?.summary ?? "").toContain("ALPHA");
    }
  });

  it("never puts B's content on A's side", () => {
    const results = compareIntelligence(paper("ALPHA"), paper("BETA"));

    for (const result of results) {
      expect(result.paper_a_claim?.summary ?? "").not.toContain("BETA");
      expect(result.paper_b_claim?.summary ?? "").toContain("BETA");
    }
  });

  it("leaves the missing side null rather than borrowing the other", () => {
    const results = compareIntelligence(paper("ALPHA"), null);

    for (const result of results) {
      expect(result.paper_b_claim).toBeNull();
      expect(result.paper_a_claim).not.toBeNull();
      expect(JSON.stringify(result.paper_b_claim)).not.toContain("ALPHA");
    }
  });

  it("keeps each section's claim to its own section", () => {
    const a = paper("A");
    const results = compareIntelligence(a, paper("B"));

    for (const result of results) {
      expect(result.paper_a_claim?.summary).toContain(result.section);
    }
  });

  it("nulls the ungrounded side while keeping the grounded one", () => {
    const a = paper("ALPHA");
    a.dataset = notSpecified();

    const result = find(compareIntelligence(a, paper("BETA")), "dataset");

    expect(result.paper_a_claim).toBeNull();
    expect(result.paper_b_claim?.summary).toContain("BETA");
  });
});

describe("Malformed input fails safe", () => {
  const ungrounded: Array<[string, unknown]> = [
    ["missing section", undefined],
    ["null section", null],
    ["not an object", "answered"],
    ["answered with no summary", { status: "answered", summary: null, evidence: [{ page: 1, chunk_id: 0 }] }],
    ["answered with a blank summary", { status: "answered", summary: "   ", evidence: [{ page: 1, chunk_id: 0 }] }],
    ["answered with no evidence", { status: "answered", summary: "x", evidence: [] }],
    ["answered with evidence not an array", { status: "answered", summary: "x", evidence: "page 3" }],
    ["evidence missing chunk_id", { status: "answered", summary: "x", evidence: [{ page: 1 }] }],
    ["evidence missing page", { status: "answered", summary: "x", evidence: [{ chunk_id: 0 }] }],
    ["page as a string", { status: "answered", summary: "x", evidence: [{ page: "1", chunk_id: 0 }] }],
    ["page below one", { status: "answered", summary: "x", evidence: [{ page: 0, chunk_id: 0 }] }],
    ["negative chunk_id", { status: "answered", summary: "x", evidence: [{ page: 1, chunk_id: -1 }] }],
    ["fractional page", { status: "answered", summary: "x", evidence: [{ page: 1.5, chunk_id: 0 }] }],
    ["unknown status", { status: "probably", summary: "x", evidence: [{ page: 1, chunk_id: 0 }] }],
  ];

  it.each(ungrounded)("treats %s as ungrounded, never comparable", (_label, broken) => {
    const a = paper("A") as unknown as Record<string, unknown>;
    a.methodology = broken;

    const result = find(
      compareIntelligence(a as unknown as PaperIntelligenceSections, paper("B")),
      "methodology",
    );

    // Degrades toward the other paper, never toward a comparison.
    expect(result.verdict).toBe("b_only");
    expect(result.paper_a_claim).toBeNull();
  });

  it("one bad evidence item disqualifies the whole section", () => {
    const a = paper("A") as unknown as Record<string, unknown>;
    a.dataset = {
      status: "answered",
      summary: "Partly usable.",
      evidence: [
        { page: 3, chunk_id: 0 },
        { page: null, chunk_id: 0 },
      ],
    };

    const result = find(
      compareIntelligence(a as unknown as PaperIntelligenceSections, paper("B")),
      "dataset",
    );

    // Partial trust is not trust: the section is not half-grounded.
    expect(result.verdict).toBe("b_only");
    expect(result.paper_a_claim).toBeNull();
  });

  it("survives entirely non-object input", () => {
    for (const junk of ["", 0, false, "intelligence", 42, []] as unknown[]) {
      const results = compareIntelligence(
        junk as unknown as PaperIntelligenceSections,
        junk as unknown as PaperIntelligenceSections,
      );
      expect(results).toHaveLength(10);
      expect(verdicts(results)).toEqual(Array(10).fill("neither"));
    }
  });

  it("an object missing every section yields ten neither", () => {
    const results = compareIntelligence(
      {} as unknown as PaperIntelligenceSections,
      {} as unknown as PaperIntelligenceSections,
    );

    expect(verdicts(results)).toEqual(Array(10).fill("neither"));
  });

  it("extra unknown keys are ignored, not compared", () => {
    const a = { ...paper("A"), future_work: answered("FUTURE") } as unknown;

    const results = compareIntelligence(
      a as PaperIntelligenceSections,
      paper("B"),
    );

    expect(results).toHaveLength(10);
    expect(results.map((r) => r.section)).toEqual([...INTELLIGENCE_SECTION_ORDER]);
    expect(JSON.stringify(results)).not.toContain("FUTURE");
  });
});

describe("Production shape — one paper analysed, one not", () => {
  it("supports A analysed and B never analysed", () => {
    // Exactly today's production state: ETASR_18859 has intelligence,
    // the other paper has none.
    const results = compareIntelligence(paper("ETASR"), null);

    expect(verdicts(results)).toEqual(Array(10).fill("a_only"));
    for (const result of results) {
      expect(result.paper_a_claim).not.toBeNull();
      expect(result.paper_b_claim).toBeNull();
    }
  });

  it("still reports not_specified sections as neither when B is absent", () => {
    const a = paper("ETASR");
    a.reproducibility = notSpecified();

    const results = compareIntelligence(a, null);

    expect(find(results, "reproducibility").verdict).toBe("neither");
    expect(find(results, "methodology").verdict).toBe("a_only");
  });
});
