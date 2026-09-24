/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
/**
 * AI relationship analysis — the fourth compare tier.
 *
 * This tier is the only one on the page that reads the MEANING of two
 * papers' claims, so the tests that matter most are the ones stopping it
 * from overstating what it knows:
 *
 *   * evidence provenance must follow the PAPER, not the column. The
 *     stored pair is canonical (paper_a_id < paper_b_id), so `cites_a`
 *     belongs to whichever paper sorted first — rendering it under the
 *     left column would misattribute B's pages to A whenever the reader's
 *     selection is reversed, and it would look perfectly well-formed;
 *   * nothing may be generated on mount — a page load must never bill a
 *     generation;
 *   * a stale analysis whose SOURCE IS GONE must not offer a regenerate
 *     button, because the server would refuse it with 422;
 *   * no page chip may be presented as a quotation: the response carries
 *     (page, chunk_id) and no text at all.
 *
 * Offline. The API module is mocked, so no network, no provider and no
 * production call is involved.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within, waitFor, fireEvent, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const links = vi.hoisted(() => [] as any[]);
const auth = vi.hoisted(() => ({ userId: "user-1" as string | undefined }));
const url = vi.hoisted(() => {
  let value: Record<string, unknown> = {};
  const listeners = new Set<() => void>();
  return {
    get: () => value,
    set: (next: Record<string, unknown>) => {
      value = next;
      listeners.forEach((l) => l());
    },
    subscribe: (l: () => void) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
});

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    user: auth.userId ? { id: auth.userId } : null,
    session: auth.userId ? { access_token: "t" } : null,
    isLoading: false,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
  }),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<any>("@/lib/api");
  return {
    ...actual,
    getPapersDetailed: vi.fn(),
    getPaperRelationship: vi.fn(),
    generatePaperRelationship: vi.fn(),
  };
});

vi.mock("@tanstack/react-router", async () => {
  const React = await vi.importActual<any>("react");
  return {
    useSearch: () => React.useSyncExternalStore(url.subscribe, url.get, url.get),
    Link: ({ children, to, params, search, ...rest }: any) => {
      links.push({ to, params, search });
      return (
        <a
          href={search?.page ? `${to}?page=${search.page}` : to}
          data-paper={params?.paperName}
          {...rest}
        >
          {children}
        </a>
      );
    },
  };
});

import {
  PaperAiRelationshipPanel,
  PaperAiRelationshipSection,
  type AiRelationshipColumn,
} from "@/components/paper-ai-relationship-panel";
import { queryKeys } from "@/lib/query-keys";
import {
  RateLimitError,
  RelationshipStateError,
  generatePaperRelationship,
  getPaperRelationship,
  getPapersDetailed,
  type PaperRelationshipResult,
  type RelationshipSection,
} from "@/lib/api";

// PAPER_LOW sorts BELOW PAPER_HIGH, so PAPER_LOW is always the canonical
// paper_a_id. Tests put them in both selection orders on purpose.
const PAPER_LOW = "11111111-1111-1111-1111-111111111111";
const PAPER_HIGH = "22222222-2222-2222-2222-222222222222";
const FOREIGN = "ffffffff-ffff-ffff-ffff-ffffffffffff";

const TITLE_LOW = "Low Paper";
const TITLE_HIGH = "High Paper";

const STAMP_A = "2026-09-24T10:00:00+00:00";
const STAMP_B = "2026-09-24T10:05:00+00:00";
const RUN_AT = "2026-09-24T11:30:00+00:00";

function section(
  overrides: Partial<RelationshipSection> = {},
): RelationshipSection {
  return {
    relation: "aligned",
    statement: "Both papers report accuracy under the same protocol.",
    cites_a: [{ page: 3, chunk_id: 0 }],
    cites_b: [{ page: 7, chunk_id: 1 }],
    ...overrides,
  };
}

function stored(
  overrides: Partial<PaperRelationshipResult> = {},
): PaperRelationshipResult {
  return {
    paper_a_id: PAPER_LOW,
    paper_b_id: PAPER_HIGH,
    relationship: { sections: { methodology: section() } },
    paper_a_generated_at: STAMP_A,
    paper_b_generated_at: STAMP_B,
    generated_at: RUN_AT,
    model: "openai/gpt-oss-120b",
    schema_version: "1",
    stale: false,
    stale_reason: null,
    superseded: false,
    ...overrides,
  };
}

function column(label: string, title: string, paperId: string): AiRelationshipColumn {
  return { label, title, paperId };
}

const COL_A = column("Paper A", TITLE_LOW, PAPER_LOW);
const COL_B = column("Paper B", TITLE_HIGH, PAPER_HIGH);

function renderPanel(props: Partial<Parameters<typeof PaperAiRelationshipPanel>[0]> = {}) {
  const onGenerate = props.onGenerate ?? vi.fn();
  const view = render(
    (
      <PaperAiRelationshipPanel
        a={COL_A}
        b={COL_B}
        data={null}
        isLoading={false}
        error={null}
        generating={false}
        generateError={null}
        {...props}
        onGenerate={onGenerate}
      />
    ) as ReactNode,
  );
  return { ...view, onGenerate };
}

function renderSection() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    (
      <QueryClientProvider client={client}>
        <PaperAiRelationshipSection />
      </QueryClientProvider>
    ) as ReactNode,
  );
  return { ...view, client };
}

beforeEach(() => {
  links.length = 0;
  auth.userId = "user-1";
  url.set({ a: PAPER_LOW, b: PAPER_HIGH });
  vi.mocked(getPapersDetailed).mockResolvedValue([
    { paper_id: PAPER_LOW, title: TITLE_LOW, status: "indexed", status_detail: null },
    { paper_id: PAPER_HIGH, title: TITLE_HIGH, status: "indexed", status_detail: null },
  ] as any);
  vi.mocked(getPaperRelationship).mockResolvedValue(null);
  vi.mocked(generatePaperRelationship).mockReset();
  vi.mocked(generatePaperRelationship).mockResolvedValue(stored());
});

// ----------------------------------------------------------------------
// 1, 21, 23 — rendering a stored analysis
// ----------------------------------------------------------------------
describe("a stored relationship renders", () => {
  it("shows the statement, the relation chip and the AI interpretation marker", () => {
    renderPanel({ data: stored() });

    expect(
      screen.getByText("Both papers report accuracy under the same protocol."),
    ).toBeInTheDocument();
    expect(screen.getByText("Aligned")).toBeInTheDocument();
    expect(screen.getByText("AI interpretation")).toBeInTheDocument();
  });

  it("states its provenance as generated interpretation, not measurement", () => {
    renderPanel({ data: stored() });

    expect(
      screen.getByText(/Generated from evidence rehydrated from both papers/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/An interpretation, not a measurement/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/the structured comparison above is the factual record/),
    ).toBeInTheDocument();
  });

  it("renders the model, schema version and both source stamps", () => {
    renderPanel({ data: stored() });

    expect(screen.getByText("openai/gpt-oss-120b")).toBeInTheDocument();
    expect(screen.getByText(/schema v1/)).toBeInTheDocument();
    // The run stamp and the two source stamps are all shown and distinct.
    expect(screen.getByText("2026-09-24 11:30 UTC")).toBeInTheDocument();
    expect(screen.getByText("2026-09-24 10:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("2026-09-24 10:05 UTC")).toBeInTheDocument();
  });

  it("renders every relation label with its neutral wording", () => {
    for (const [relation, label] of [
      ["aligned", "Aligned"],
      ["divergent", "Divergent"],
      ["complementary", "Complementary"],
      ["not_comparable", "Not comparable"],
    ] as const) {
      const { unmount } = renderPanel({
        data: stored({
          relationship: { sections: { methodology: section({ relation }) } },
        }),
      });
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });

  it("renders only the comparable sections, in canonical order", () => {
    renderPanel({
      data: stored({
        relationship: {
          sections: {
            key_results: section(),
            methodology: section(),
          },
        },
      }),
    });

    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);

    // methodology precedes key_results in the canonical ten, whatever
    // order the object happened to arrive in.
    expect(headings).toEqual(["Methodology", "Key Results"]);
    // The eight sections that were not comparable are simply absent — no
    // verdict is implied for them.
    expect(screen.queryByText("Limitations")).not.toBeInTheDocument();
    expect(screen.queryByText("Dataset")).not.toBeInTheDocument();
  });

  it("offers a secondary Regenerate action, not a primary Generate one", () => {
    renderPanel({ data: stored() });

    expect(screen.getByRole("button", { name: "Regenerate" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Generate AI relationship analysis/ }),
    ).not.toBeInTheDocument();
  });
});

// ----------------------------------------------------------------------
// 19, 20 — evidence provenance follows the PAPER, not the column
// ----------------------------------------------------------------------
describe("evidence provenance", () => {
  it("links Paper A's pages to Paper A and Paper B's to Paper B", () => {
    renderPanel({ data: stored() });

    const a = screen.getByLabelText("Open Paper A at page 3");
    const b = screen.getByLabelText("Open Paper B at page 7");

    expect(a).toHaveAttribute("data-paper", TITLE_LOW);
    expect(b).toHaveAttribute("data-paper", TITLE_HIGH);
    // Never the other paper's page.
    expect(screen.queryByLabelText("Open Paper A at page 7")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Open Paper B at page 3")).not.toBeInTheDocument();
  });

  it("follows the paper id when the reader's selection is REVERSED", () => {
    // The reader put the canonically-second paper in slot A. cites_a still
    // belongs to PAPER_LOW, so it must render under whichever column holds
    // PAPER_LOW — here, "Paper B".
    renderPanel({
      a: column("Paper A", TITLE_HIGH, PAPER_HIGH),
      b: column("Paper B", TITLE_LOW, PAPER_LOW),
      data: stored(),
    });

    // cites_a = page 3 belongs to PAPER_LOW, now shown as Paper B.
    const lowPage = screen.getByLabelText("Open Paper B at page 3");
    expect(lowPage).toHaveAttribute("data-paper", TITLE_LOW);

    // cites_b = page 7 belongs to PAPER_HIGH, now shown as Paper A.
    const highPage = screen.getByLabelText("Open Paper A at page 7");
    expect(highPage).toHaveAttribute("data-paper", TITLE_HIGH);

    // The naive positional reading would have produced these two.
    expect(screen.queryByLabelText("Open Paper A at page 3")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Open Paper B at page 7")).not.toBeInTheDocument();
  });

  it("labels each evidence row with the paper it belongs to", () => {
    renderPanel({ data: stored() });

    expect(screen.getByText("Paper A evidence")).toBeInTheDocument();
    expect(screen.getByText("Paper B evidence")).toBeInTheDocument();
  });

  it("says so plainly when one side cited nothing", () => {
    renderPanel({
      data: stored({
        relationship: {
          sections: {
            methodology: section({ relation: "not_comparable", cites_a: [], cites_b: [] }),
          },
        },
      }),
    });

    expect(screen.getByText("No evidence cited for Paper A.")).toBeInTheDocument();
    expect(screen.getByText("No evidence cited for Paper B.")).toBeInTheDocument();
  });

  it("never presents a page chip as a quotation", () => {
    const { container } = renderPanel({ data: stored() });

    // The response carries no text, so nothing quotable may appear.
    expect(container.querySelector("blockquote")).toBeNull();
    expect(screen.queryByText(/"/)).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/quote/i);
  });
});

// ----------------------------------------------------------------------
// 2, 3 — the empty state
// ----------------------------------------------------------------------
describe("no stored relationship", () => {
  it("shows a dashed empty state with a Generate action", () => {
    renderPanel({ data: null });

    expect(screen.getByText("No AI relationship analysis yet")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Generate AI relationship analysis/ }),
    ).toBeInTheDocument();
  });

  it("says the generation costs an AI unit before it is clicked", () => {
    renderPanel({ data: null });
    expect(screen.getByText(/costs one AI generation/)).toBeInTheDocument();
  });

  it("performs NO generation on mount", async () => {
    renderSection();

    await waitFor(() =>
      expect(screen.getByText("No AI relationship analysis yet")).toBeInTheDocument(),
    );

    expect(generatePaperRelationship).not.toHaveBeenCalled();
  });
});

// ----------------------------------------------------------------------
// 4, 5 — staleness
// ----------------------------------------------------------------------
describe("stale relationships", () => {
  it("source_intelligence_changed shows the state and still offers Regenerate", () => {
    renderPanel({
      data: stored({ stale: true, stale_reason: "source_intelligence_changed" }),
    });

    expect(screen.getByText("Out of date")).toBeInTheDocument();
    expect(screen.getByText(/re-analysed after this comparison was generated/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeInTheDocument();
    // The stored result is still shown rather than blanked.
    expect(
      screen.getByText("Both papers report accuracy under the same protocol."),
    ).toBeInTheDocument();
  });

  it("source_intelligence_missing withholds Regenerate entirely", () => {
    renderPanel({
      data: stored({ stale: true, stale_reason: "source_intelligence_missing" }),
    });

    expect(screen.getByText("Out of date")).toBeInTheDocument();
    expect(screen.getByText(/Analyse that paper again before relating them/)).toBeInTheDocument();
    // Offering it would promise something the server answers with 422.
    expect(screen.queryByRole("button", { name: "Regenerate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("conveys staleness in text, not colour alone", () => {
    renderPanel({
      data: stored({ stale: true, stale_reason: "source_intelligence_changed" }),
    });
    // A readable label, so the state survives greyscale and a screen reader.
    expect(screen.getByText("Out of date")).toBeVisible();
  });

  it("shows no stale indicator when the analysis is current", () => {
    renderPanel({ data: stored() });
    expect(screen.queryByText("Out of date")).not.toBeInTheDocument();
  });
});

// ----------------------------------------------------------------------
// 6, 7, 8, 9 — generation through the wiring
// ----------------------------------------------------------------------
describe("generation", () => {
  it("POSTs once and renders the returned analysis", async () => {
    renderSection();

    const button = await screen.findByRole("button", {
      name: /Generate AI relationship analysis/,
    });
    fireEvent.click(button);

    await waitFor(() =>
      expect(
        screen.getByText("Both papers report accuracy under the same protocol."),
      ).toBeInTheDocument(),
    );

    expect(generatePaperRelationship).toHaveBeenCalledTimes(1);
    expect(generatePaperRelationship).toHaveBeenCalledWith(PAPER_LOW, PAPER_HIGH);
  });

  it("writes the result into the CANONICAL cache key without refetching", async () => {
    const { client } = renderSection();

    fireEvent.click(
      await screen.findByRole("button", { name: /Generate AI relationship analysis/ }),
    );

    await waitFor(() => expect(generatePaperRelationship).toHaveBeenCalled());

    const key = queryKeys.paperRelationship("user-1", PAPER_LOW, PAPER_HIGH);
    await waitFor(() =>
      expect(client.getQueryData(key)).toMatchObject({ paper_a_id: PAPER_LOW }),
    );

    // Reversed lookup hits the SAME entry, so the other orientation cannot
    // keep serving a stale copy.
    const reversed = queryKeys.paperRelationship("user-1", PAPER_HIGH, PAPER_LOW);
    expect(client.getQueryData(reversed)).toBe(client.getQueryData(key));

    // One GET on mount, and no second one after the POST.
    expect(vi.mocked(getPaperRelationship).mock.calls.length).toBe(1);
  });

  it("leaves paper-intelligence caches untouched", async () => {
    const { client } = renderSection();
    const intelKey = queryKeys.paperIntelligence("user-1", PAPER_LOW);
    client.setQueryData(intelKey, { marker: "unchanged" });

    fireEvent.click(
      await screen.findByRole("button", { name: /Generate AI relationship analysis/ }),
    );
    await waitFor(() => expect(generatePaperRelationship).toHaveBeenCalled());

    expect(client.getQueryData(intelKey)).toEqual({ marker: "unchanged" });
  });

  it("fires exactly one POST for two clicks dispatched in the SAME React batch", async () => {
    // The case the `disabled` attribute does not cover. React flushes
    // between discrete events, so two sequential fireEvent.click calls only
    // ever reach the handler once — which makes that weaker test unable to
    // detect a missing guard. Batched inside one act(), both clicks run
    // before any re-render, so `disabled` has not been applied yet and a
    // state-based guard would still read its stale `false`.
    let release: (v: PaperRelationshipResult) => void = () => {};
    vi.mocked(generatePaperRelationship).mockImplementation(
      () => new Promise<PaperRelationshipResult>((resolve) => (release = resolve)),
    );

    renderSection();
    const button = await screen.findByRole("button", {
      name: /Generate AI relationship analysis/,
    });

    act(() => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(generatePaperRelationship).toHaveBeenCalledTimes(1);

    release(stored());
    await waitFor(() =>
      expect(
        screen.getByText("Both papers report accuracy under the same protocol."),
      ).toBeInTheDocument(),
    );
    expect(generatePaperRelationship).toHaveBeenCalledTimes(1);
  });

  it("fires exactly one POST for rapid sequential clicks", async () => {
    let release: (v: PaperRelationshipResult) => void = () => {};
    vi.mocked(generatePaperRelationship).mockImplementation(
      () => new Promise<PaperRelationshipResult>((resolve) => (release = resolve)),
    );

    renderSection();
    const button = await screen.findByRole("button", {
      name: /Generate AI relationship analysis/,
    });

    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(generatePaperRelationship).toHaveBeenCalledTimes(1));

    release(stored());
    await waitFor(() => expect(generatePaperRelationship).toHaveBeenCalledTimes(1));
  });

  it("disables the button and marks it busy while generating", () => {
    renderPanel({ data: null, generating: true });

    const button = screen.getByRole("button");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
  });

  it("announces the generation through role=status without a fake animation", () => {
    renderPanel({ data: null, generating: true });

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Analysing both papers’ evidence…");
    // No "thinking" theatre: one spinner, and it is decorative.
    expect(status.querySelectorAll("svg[aria-hidden='true']").length).toBe(1);
  });
});

// ----------------------------------------------------------------------
// 10-13 — error states
// ----------------------------------------------------------------------
describe("error states", () => {
  it("renders a 404 Paper not found without distinguishing foreign from unknown", () => {
    renderPanel({ error: new Error("Paper not found.") });

    expect(screen.getByText("Paper not found.")).toBeInTheDocument();
    // No generate offer on a pair we cannot even resolve.
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("explains a 409 conflict and offers a manual Try again", () => {
    renderPanel({
      data: stored(),
      generateError: new RelationshipStateError(
        "source_intelligence_changed",
        "One of the papers was re-analysed while this comparison was running. Please try again.",
      ),
    });

    // The backend's own authored wording…
    expect(screen.getByText(/Please try again\.$/)).toBeInTheDocument();
    // …and the panel's added explanation of why nothing was saved. Matched
    // on its distinguishing tail: the two deliberately share a prefix.
    expect(
      screen.getByText(/so the result was discarded rather than saved over the newer analysis/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("does not retry a 409 automatically", async () => {
    vi.mocked(generatePaperRelationship).mockRejectedValue(
      new RelationshipStateError("source_intelligence_changed", "Conflict."),
    );

    renderSection();
    fireEvent.click(
      await screen.findByRole("button", { name: /Generate AI relationship analysis/ }),
    );

    await waitFor(() => expect(screen.getByText("Conflict.")).toBeInTheDocument());
    // Still exactly one attempt: the retry is the reader's to make.
    expect(generatePaperRelationship).toHaveBeenCalledTimes(1);
  });

  it("names the unanalysed paper on 422 source_intelligence_missing and offers no retry", () => {
    renderPanel({
      generateError: new RelationshipStateError(
        "source_intelligence_missing",
        "Both papers must be analysed before they can be related.",
        {
          paper_a_id: PAPER_LOW,
          paper_b_id: PAPER_HIGH,
          paper_a_analysed: true,
          paper_b_analysed: false,
        },
      ),
    });

    expect(
      screen.getByText(/Both papers must be analysed before they can be related/),
    ).toBeInTheDocument();
    // PAPER_HIGH is the unanalysed one, and it sits in the Paper B column.
    const link = screen.getByLabelText("Open Paper B to analyse it");
    expect(link).toHaveAttribute("data-paper", TITLE_HIGH);
    expect(screen.queryByLabelText("Open Paper A to analyse it")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("maps the missing-paper flags by id even when the selection is reversed", () => {
    renderPanel({
      a: column("Paper A", TITLE_HIGH, PAPER_HIGH),
      b: column("Paper B", TITLE_LOW, PAPER_LOW),
      generateError: new RelationshipStateError(
        "source_intelligence_missing",
        "Both papers must be analysed before they can be related.",
        {
          paper_a_id: PAPER_LOW,
          paper_b_id: PAPER_HIGH,
          paper_a_analysed: true,
          paper_b_analysed: false,
        },
      ),
    });

    // paper_b_analysed=false refers to PAPER_HIGH, which the reader has in
    // slot A. A positional reading would have blamed the wrong paper.
    const link = screen.getByLabelText("Open Paper A to analyse it");
    expect(link).toHaveAttribute("data-paper", TITLE_HIGH);
    expect(screen.queryByLabelText("Open Paper B to analyse it")).not.toBeInTheDocument();
  });

  it("explains 422 no_comparable_sections and offers no retry", () => {
    renderPanel({
      generateError: new RelationshipStateError(
        "no_comparable_sections",
        "These papers have no section with grounded evidence on both sides.",
      ),
    });

    expect(
      screen.getByText(/no section with grounded evidence on both sides/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/No section has grounded evidence on both sides/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders a rate-limit refusal with our own wording and no retry loop", async () => {
    vi.mocked(generatePaperRelationship).mockRejectedValue(new RateLimitError());

    renderSection();
    fireEvent.click(
      await screen.findByRole("button", { name: /Generate AI relationship analysis/ }),
    );

    // Asserted against the class's own message rather than a guessed
    // phrase, so a reworded constant cannot leave this passing vacuously.
    const ourWording = new RateLimitError().message;
    await waitFor(() => expect(screen.getByText(ourWording)).toBeInTheDocument());
    expect(ourWording).toMatch(/busy right now/);

    expect(generatePaperRelationship).toHaveBeenCalledTimes(1);
  });

  it("shows the backend's sanitized provider message and never a raw exception", async () => {
    vi.mocked(generatePaperRelationship).mockRejectedValue(
      new Error("The research service took too long to respond. Please try again."),
    );

    const { container } = renderSection();
    fireEvent.click(
      await screen.findByRole("button", { name: /Generate AI relationship analysis/ }),
    );

    await waitFor(() =>
      expect(
        screen.getByText("The research service took too long to respond. Please try again."),
      ).toBeInTheDocument(),
    );

    for (const leak of ["Traceback", "RuntimeError", "groq", "psycopg", "sk-", "Bearer"]) {
      expect(container.textContent).not.toContain(leak);
    }
  });

  it("surfaces a GET failure without offering a generation", () => {
    renderPanel({ error: new Error("Something went wrong on our side. Please try again.") });

    expect(
      screen.getByText("Something went wrong on our side. Please try again."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

// ----------------------------------------------------------------------
// 14-18 — selection, ownership, isolation
// ----------------------------------------------------------------------
describe("selection and ownership", () => {
  it("drives the request from the URL selection", async () => {
    renderSection();

    await waitFor(() =>
      expect(getPaperRelationship).toHaveBeenCalledWith(PAPER_LOW, PAPER_HIGH),
    );
  });

  it("makes NO relationship request for an id outside this account's papers", async () => {
    url.set({ a: PAPER_LOW, b: FOREIGN });

    const { container } = renderSection();
    await waitFor(() => expect(getPapersDetailed).toHaveBeenCalled());

    expect(getPaperRelationship).not.toHaveBeenCalled();
    expect(generatePaperRelationship).not.toHaveBeenCalled();
    // The section renders nothing at all, so no metadata is disclosed.
    expect(container.textContent).toBe("");
  });

  it("makes NO request for the same paper twice", async () => {
    url.set({ a: PAPER_LOW, b: PAPER_LOW });

    renderSection();
    await waitFor(() => expect(getPapersDetailed).toHaveBeenCalled());

    expect(getPaperRelationship).not.toHaveBeenCalled();
  });

  it("makes NO request before a selection exists", async () => {
    url.set({});

    renderSection();
    await waitFor(() => expect(getPapersDetailed).toHaveBeenCalled());

    expect(getPaperRelationship).not.toHaveBeenCalled();
  });

  it("sends only the two paper ids — never an owner id", async () => {
    renderSection();

    await waitFor(() => expect(getPaperRelationship).toHaveBeenCalled());

    const args = vi.mocked(getPaperRelationship).mock.calls[0];
    expect(args).toEqual([PAPER_LOW, PAPER_HIGH]);
    // No third argument could carry an owner or a token.
    expect(args.length).toBe(2);
  });

  it("keys the cache per user, so one account never reads another's", () => {
    const a = queryKeys.paperRelationship("user-1", PAPER_LOW, PAPER_HIGH);
    const b = queryKeys.paperRelationship("user-2", PAPER_LOW, PAPER_HIGH);

    expect(a).not.toEqual(b);
    expect(a).toContain("user-1");
  });

  it("does not fetch for a signed-out reader", async () => {
    auth.userId = undefined;

    renderSection();
    await new Promise((r) => setTimeout(r, 0));

    expect(getPaperRelationship).not.toHaveBeenCalled();
    expect(getPapersDetailed).not.toHaveBeenCalled();
  });

  it("puts no credential in any link it renders", () => {
    renderPanel({ data: stored() });

    for (const anchor of Array.from(document.querySelectorAll("a"))) {
      const href = anchor.getAttribute("href") ?? "";
      for (const secret of ["token", "access_token", "Bearer", "apikey", "jwt"]) {
        expect(href.toLowerCase()).not.toContain(secret.toLowerCase());
      }
    }
  });
});

// ----------------------------------------------------------------------
// 25 — accessibility and layout
// ----------------------------------------------------------------------
describe("accessibility and layout", () => {
  it("labels the section and every decorative icon", () => {
    const { container } = renderPanel({ data: stored() });

    expect(
      container.querySelector("section[aria-label='AI relationship analysis']"),
    ).not.toBeNull();
    for (const svg of Array.from(container.querySelectorAll("svg"))) {
      expect(svg.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("announces loading through role=status", () => {
    renderPanel({ isLoading: true });

    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading AI relationship analysis…",
    );
    // A verdict before the data arrives would be a false statement.
    expect(screen.queryByText("Aligned")).not.toBeInTheDocument();
  });

  it("uses a real button and real links", () => {
    renderPanel({ data: stored() });

    expect(screen.getByRole("button", { name: "Regenerate" }).tagName).toBe("BUTTON");
    expect(screen.getAllByRole("link").length).toBeGreaterThan(0);
  });

  it("stacks the two evidence rows on mobile and pairs them on desktop", () => {
    const { container } = renderPanel({ data: stored() });

    const grid = container.querySelector(".grid.gap-3.md\\:grid-cols-2");
    expect(grid).not.toBeNull();
    // Each stacked row keeps its paper name, so it is never orphaned.
    expect(within(grid as HTMLElement).getByText("Paper A evidence")).toBeInTheDocument();
    expect(within(grid as HTMLElement).getByText("Paper B evidence")).toBeInTheDocument();
  });

  it("names both the paper and the page in every evidence link", () => {
    renderPanel({ data: stored() });

    expect(screen.getByLabelText("Open Paper A at page 3")).toBeInTheDocument();
    expect(screen.getByLabelText("Open Paper B at page 7")).toBeInTheDocument();
  });

  it("exposes the relation as text rather than colour", () => {
    renderPanel({
      data: stored({
        relationship: { sections: { methodology: section({ relation: "divergent" }) } },
      }),
    });

    const chip = screen.getByText("Divergent");
    expect(chip).toBeVisible();
    // No positive/negative tinting: the existing neutral chip styling only.
    expect(chip.className).not.toMatch(/text-(destructive|green|red|emerald)/);
    expect(chip.className).not.toMatch(/bg-(destructive|green|red|emerald)/);
  });
});
