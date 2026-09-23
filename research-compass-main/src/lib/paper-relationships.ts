import {
  INTELLIGENCE_SECTION_ORDER,
  type IntelligenceSection,
  type IntelligenceSectionName,
  type PaperIntelligenceSections,
} from "@/lib/api";

/**
 * Deterministic cross-paper comparability, computed from two papers'
 * ALREADY-VALIDATED Paper Intelligence.
 *
 * This module answers exactly one question, ten times over: does each
 * paper have a grounded claim for this section? That is a fact about
 * stored data, so it is computed rather than inferred — there is no
 * model here, no network, no similarity measure, and no word comparison.
 *
 * WHAT IT DELIBERATELY DOES NOT DO
 * --------------------------------
 * It does not decide whether two papers agree, overlap, contradict or
 * complement each other. Those are semantic judgements, and the only
 * material available at this layer is two sets of AI-written summaries —
 * comparing their words would manufacture agreement ("both mention a
 * dataset") that neither paper actually supports. A relationship field
 * is therefore absent from the result by design, not by omission.
 *
 * It also ranks nothing. There is no numeric field in this module and
 * `Verdict` has no ordering; "comparable" is not better than "a_only".
 *
 * PRESERVATION
 * ------------
 * A claim is passed through as the ORIGINAL section object, by
 * reference. Nothing is re-summarised, merged, defaulted or rewritten,
 * and evidence keeps its `(page, chunk_id)` identity and its owning
 * paper. Callers can assert `result.paper_a_claim === a.methodology`,
 * which is a stronger guarantee than any deep-equality check.
 *
 * FAILING SAFE
 * ------------
 * Malformed input never produces a comparison. A section that claims
 * `answered` but carries no usable summary or no usable evidence is
 * treated as ungrounded, so corrupt data degrades toward "neither" —
 * never toward "comparable".
 */

/** Whether each paper has a grounded claim for a section. Not a grade. */
export type Verdict = "comparable" | "a_only" | "b_only" | "neither";

export interface SectionComparison {
  section: IntelligenceSectionName;
  verdict: Verdict;
  /** Paper A's stored section, by reference, or null when ungrounded. */
  paper_a_claim: IntelligenceSection | null;
  /** Paper B's stored section, by reference, or null when ungrounded. */
  paper_b_claim: IntelligenceSection | null;
}

/** Input shape: the ten validated sections, or nothing at all. */
export type IntelligenceInput = PaperIntelligenceSections | null | undefined;

function isUsableEvidence(value: unknown): boolean {
  if (!Array.isArray(value) || value.length === 0) return false;

  // Every item must carry the (page, chunk_id) pair that IS evidence
  // identity in this system. One unusable item makes the section's
  // grounding unreliable, so the whole section is treated as ungrounded
  // rather than partially trusted.
  return value.every((item) => {
    if (!item || typeof item !== "object") return false;
    const { page, chunk_id: chunkId } = item as Record<string, unknown>;
    return (
      typeof page === "number" &&
      Number.isInteger(page) &&
      page >= 1 &&
      typeof chunkId === "number" &&
      Number.isInteger(chunkId) &&
      chunkId >= 0
    );
  });
}

/**
 * Does this side hold a grounded claim for the section?
 *
 * `not_specified` is not grounded: the paper explicitly says nothing,
 * which is a correct answer but not something to compare against. Nor
 * is a section that merely claims to be answered — the summary and the
 * evidence both have to be there. The backend validator already
 * guarantees that for anything it stored; this re-checks because a
 * caller could hand us a hand-built or corrupted object, and the safe
 * direction to fail is "not comparable".
 */
function isGrounded(section: unknown): section is IntelligenceSection {
  if (!section || typeof section !== "object") return false;

  const { status, summary, evidence } = section as Record<string, unknown>;

  if (status !== "answered") return false;
  if (typeof summary !== "string" || summary.trim().length === 0) return false;

  return isUsableEvidence(evidence);
}

/** One paper's section, or undefined if the paper or section is absent. */
function sectionOf(
  intelligence: IntelligenceInput,
  name: IntelligenceSectionName,
): unknown {
  if (!intelligence || typeof intelligence !== "object") return undefined;
  return (intelligence as Record<string, unknown>)[name];
}

/**
 * Compare two papers' saved analyses, section by section.
 *
 * Always returns exactly ten results, in INTELLIGENCE_SECTION_ORDER,
 * whatever the inputs are — including when both are null. A paper with
 * no saved analysis is not an error state here: it simply holds no
 * grounded claims, which is the current production case for a paper
 * that has never been analysed.
 *
 * Pure: no network, no provider, no I/O, no mutation of its arguments.
 * The same two objects always produce the same result.
 */
export function compareIntelligence(
  a: IntelligenceInput,
  b: IntelligenceInput,
): SectionComparison[] {
  return INTELLIGENCE_SECTION_ORDER.map((section) => {
    // Read each side independently. Nothing is ever read from one paper
    // to stand in for the other.
    const aSection = sectionOf(a, section);
    const bSection = sectionOf(b, section);

    const aGrounded = isGrounded(aSection);
    const bGrounded = isGrounded(bSection);

    const verdict: Verdict = aGrounded
      ? bGrounded
        ? "comparable"
        : "a_only"
      : bGrounded
        ? "b_only"
        : "neither";

    return {
      section,
      verdict,
      // Passed through by reference; null rather than a placeholder, so
      // an ungrounded side cannot be rendered as though it were a claim.
      paper_a_claim: aGrounded ? (aSection as IntelligenceSection) : null,
      paper_b_claim: bGrounded ? (bSection as IntelligenceSection) : null,
    };
  });
}
