/* eslint-disable @typescript-eslint/no-explicit-any -- test mocks are loosely typed by convention */
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: vi.fn(),
}));

import { getSupabaseClient } from "@/lib/supabase";
import {
  searchPapers,
  comparePapers,
  generateReport,
  summarizePaper,
  getPaperDetails,
  retryUnlessRateLimited,
  streamAskQuestion,
  getPapers,
  getDashboardStats,
  uploadPaper,
  describeIndexResult,
  partitionCitations,
  generatePaperIntelligence,
  getPaperIntelligence,
  AuthenticationRequiredError,
  PaperIntelligenceNotFoundError,
  RateLimitError,
  type IndexResult,
  type Citation,
} from "@/lib/api";

function mockSupabaseWithSession(accessToken: string | null) {
  (getSupabaseClient as any).mockReturnValue({
    auth: {
      getSession: vi.fn().mockResolvedValue({
        data: { session: accessToken ? { access_token: accessToken } : null },
      }),
    },
  });
}

function mockSupabaseUnconfigured() {
  (getSupabaseClient as any).mockReturnValue(null);
}

describe("authenticated API calls (authFetch)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("attaches Authorization: Bearer <token> when a session exists", async () => {
    mockSupabaseWithSession("test-access-token-123");
    const fetchSpy = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ status: "success", results: [] }), { status: 200 }),
      );

    await searchPapers("explainable AI");

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [, options] = fetchSpy.mock.calls[0];
    const headers = new Headers(options?.headers);
    expect(headers.get("Authorization")).toBe("Bearer test-access-token-123");
  });

  it("throws AuthenticationRequiredError and NEVER calls fetch when there is no session", async () => {
    mockSupabaseWithSession(null);
    const fetchSpy = vi.spyOn(global, "fetch");

    await expect(searchPapers("anything")).rejects.toThrow(AuthenticationRequiredError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("throws AuthenticationRequiredError when Supabase itself is not configured", async () => {
    mockSupabaseUnconfigured();
    const fetchSpy = vi.spyOn(global, "fetch");

    await expect(searchPapers("anything")).rejects.toThrow(AuthenticationRequiredError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("searchPapers — rate limits and cancellation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockSupabaseWithSession("test-access-token-123");
  });

  const jsonResponse = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status });

  it("passes the abort signal through to fetch", async () => {
    const fetchSpy = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(jsonResponse({ status: "success", results: [] }));
    const controller = new AbortController();

    await searchPapers("attention", controller.signal);

    expect(fetchSpy.mock.calls[0][1]?.signal).toBe(controller.signal);
  });

  it("turns an HTTP 429 into RateLimitError without parsing the body", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(new Response("Too Many Requests", { status: 429 }));

    await expect(searchPapers("attention")).rejects.toBeInstanceOf(RateLimitError);
  });

  it("recognises the backend's 200 error body carrying an upstream rate-limit message", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      jsonResponse({
        status: "error",
        message: "You will have reduced rate limits of 3 RPM and 10K TPM until billing is added.",
      }),
    );

    const error = await searchPapers("attention").catch((e) => e);

    expect(error).toBeInstanceOf(RateLimitError);
    expect(error.message).not.toMatch(/RPM|billing/);
  });

  it("keeps any other backend error an ordinary error", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      jsonResponse({ status: "error", message: "Collection not found" }),
    );

    const error = await searchPapers("attention").catch((e) => e);

    expect(error).not.toBeInstanceOf(RateLimitError);
    expect(error.message).toBe("Collection not found");
  });
});

describe("upstream throttling is classified the same way on every provider-backed call", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockSupabaseWithSession("test-access-token-123");
  });

  const jsonResponse = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status });

  // Verbatim what Voyage returned in production, billing wording and all.
  const VOYAGE_BILLING_TEXT =
    "You will have reduced rate limits of 3 RPM and 10K TPM until billing is added.";

  const callers: [string, () => Promise<unknown>][] = [
    ["comparePapers", () => comparePapers("a.pdf", "b.pdf")],
    ["generateReport", () => generateReport("vision language models")],
    ["summarizePaper", () => summarizePaper("a.pdf")],
    ["getPaperDetails", () => getPaperDetails("a.pdf")],
  ];

  for (const [name, call] of callers) {
    it(`${name} raises RateLimitError and never leaks the provider's billing wording`, async () => {
      vi.spyOn(global, "fetch").mockResolvedValue(
        jsonResponse({ status: "error", message: VOYAGE_BILLING_TEXT }),
      );

      const error: any = await call().catch((e) => e);

      expect(error).toBeInstanceOf(RateLimitError);
      expect(error.message).not.toMatch(/RPM|TPM|billing|Voyage/i);
    });
  }

  // Quota/credit phrasings carry no "rate limit" wording, yet mean the same
  // thing: retrying them would spend more of the exhausted allowance.
  for (const message of [
    "You have exceeded your monthly quota.",
    "Insufficient credits remaining on this account.",
    "429 Too Many Requests",
  ]) {
    it(`treats "${message}" as throttling, not an ordinary failure`, async () => {
      vi.spyOn(global, "fetch").mockResolvedValue(jsonResponse({ status: "error", message }));

      await expect(comparePapers("a.pdf", "b.pdf")).rejects.toBeInstanceOf(RateLimitError);
    });
  }

  it("leaves an ordinary application error generic, with its message intact", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      jsonResponse({ status: "error", message: "Paper not found: vaswani_2017.pdf" }),
    );

    const error = await comparePapers("vaswani_2017.pdf", "b.pdf").catch((e) => e);

    expect(error).not.toBeInstanceOf(RateLimitError);
    expect(error.message).toBe("Paper not found: vaswani_2017.pdf");
  });

  it("falls back to the caller's own wording when the backend sends no message", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(jsonResponse({ status: "error" }));

    const error = await generateReport("topic").catch((e) => e);

    expect(error).not.toBeInstanceOf(RateLimitError);
    expect(error.message).toBe("Report generation failed");
  });
});

describe("retryUnlessRateLimited — a throttled query must never be retried", () => {
  it("refuses to retry a RateLimitError, however early the failure", () => {
    expect(retryUnlessRateLimited(0, new RateLimitError())).toBe(false);
    expect(retryUnlessRateLimited(2, new RateLimitError())).toBe(false);
  });

  it("still retries ordinary failures, up to three attempts", () => {
    expect(retryUnlessRateLimited(0, new Error("network down"))).toBe(true);
    expect(retryUnlessRateLimited(2, new Error("network down"))).toBe(true);
    expect(retryUnlessRateLimited(3, new Error("network down"))).toBe(false);
  });
});

describe("getPapers / getDashboardStats — must be owner-scoped, not plain fetch", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("getPapers attaches Authorization: Bearer <token>", async () => {
    mockSupabaseWithSession("papers-token");
    const fetchSpy = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ status: "success", papers: [] }), { status: 200 }),
      );

    await getPapers();

    const [, options] = fetchSpy.mock.calls[0];
    const headers = new Headers(options?.headers);
    expect(headers.get("Authorization")).toBe("Bearer papers-token");
  });

  it("getPapers throws AuthenticationRequiredError and never calls fetch when there is no session", async () => {
    mockSupabaseWithSession(null);
    const fetchSpy = vi.spyOn(global, "fetch");

    await expect(getPapers()).rejects.toThrow(AuthenticationRequiredError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("getDashboardStats attaches Authorization: Bearer <token>", async () => {
    mockSupabaseWithSession("stats-token");
    const fetchSpy = vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "success",
          total_papers: 0,
          total_chunks: 0,
          recent_papers: [],
        }),
        { status: 200 },
      ),
    );

    await getDashboardStats();

    const [, options] = fetchSpy.mock.calls[0];
    const headers = new Headers(options?.headers);
    expect(headers.get("Authorization")).toBe("Bearer stats-token");
  });

  it("getDashboardStats throws AuthenticationRequiredError and never calls fetch when there is no session", async () => {
    mockSupabaseWithSession(null);
    const fetchSpy = vi.spyOn(global, "fetch");

    await expect(getDashboardStats()).rejects.toThrow(AuthenticationRequiredError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("uploadPaper — a failed-status body must never be treated as success", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("resolves normally for a genuine duplicate (status: uploaded)", async () => {
    mockSupabaseWithSession("tok");
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          message: "This file was already uploaded",
          paper_id: "p1",
          filename: "paper.pdf",
          status: "uploaded",
          saved_path: "owner/p1/original.pdf",
          duplicate: true,
        }),
        { status: 200 },
      ),
    );

    const result = await uploadPaper(new File(["x"], "paper.pdf"));
    expect(result.duplicate).toBe(true);
    expect(result.status).toBe("uploaded");
  });

  it("throws even on HTTP 200 if the response body's status is 'failed'", async () => {
    mockSupabaseWithSession("tok");
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          message: "Storage upload failed",
          paper_id: "p1",
          filename: "paper.pdf",
          status: "failed",
          saved_path: "owner/p1/original.pdf",
          duplicate: true,
        }),
        { status: 200 },
      ),
    );

    await expect(uploadPaper(new File(["x"], "paper.pdf"))).rejects.toThrow(
      "Storage upload failed",
    );
  });
});

describe("describeIndexResult — no field can ever render as 'undefined'", () => {
  function makeResult(overrides: Partial<IndexResult>): IndexResult {
    return {
      status: "success",
      papers_found: 0,
      papers_indexed: 0,
      papers_failed: 0,
      indexed: [],
      failed: [],
      ...overrides,
    };
  }

  it("sums points across all indexed papers and reports the real counts", () => {
    const outcome = describeIndexResult(
      makeResult({
        papers_found: 2,
        papers_indexed: 2,
        indexed: [
          { paper_id: "p1", points: 12 },
          { paper_id: "p2", points: 8 },
        ],
      }),
    );

    expect(outcome.kind).toBe("indexed");
    expect(outcome.message).toBe("Indexed 2 PDFs · 20 chunks stored in Qdrant");
    expect(outcome.message).not.toContain("undefined");
  });

  it("singularizes correctly for exactly one paper and one chunk", () => {
    const outcome = describeIndexResult(
      makeResult({
        papers_found: 1,
        papers_indexed: 1,
        indexed: [{ paper_id: "p1", points: 1 }],
      }),
    );

    expect(outcome.message).toBe("Indexed 1 PDF · 1 chunk stored in Qdrant");
  });

  it("reports 'already indexed', not a false success message, when nothing new was indexed", () => {
    const outcome = describeIndexResult(makeResult({ papers_found: 0 }));

    expect(outcome.kind).toBe("already-indexed");
    expect(outcome.message).not.toContain("undefined");
  });

  it("reports a real failure — not a success — when indexing fails", () => {
    const outcome = describeIndexResult(
      makeResult({
        papers_found: 1,
        papers_failed: 1,
        failed: [{ paper_id: "p1", error: "Voyage API failure" }],
      }),
    );

    expect(outcome.kind).toBe("failed");
    expect(outcome.message).toBe("Voyage API failure");
  });

  it("replaces upstream throttling text with our own wording", () => {
    const outcome = describeIndexResult(
      makeResult({
        papers_found: 1,
        papers_failed: 1,
        failed: [
          {
            paper_id: "p1",
            error: "You will have reduced rate limits of 3 RPM and 10K TPM until billing is added.",
          },
        ],
      }),
    );

    expect(outcome.kind).toBe("failed");
    expect(outcome.message).not.toMatch(/RPM|TPM|billing/i);
    expect(outcome.message).toMatch(/busy right now/i);
  });
});

describe("streamAskQuestion — fetch()-based SSE, deliberately not EventSource", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  function sseBodyStream(frames: string[]) {
    const encoder = new TextEncoder();
    let i = 0;
    return new ReadableStream({
      pull(controller) {
        if (i < frames.length) {
          controller.enqueue(encoder.encode(frames[i]));
          i++;
        } else {
          controller.close();
        }
      },
    });
  }

  it("sends the token as a request header, never in the URL/query string", async () => {
    mockSupabaseWithSession("stream-token-456");
    const body = sseBodyStream([
      `data: ${JSON.stringify({ type: "status", text: "Searching…" })}\n\n`,
      `data: ${JSON.stringify({ type: "done", citations: [] })}\n\n`,
    ]);
    const fetchSpy = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(new Response(body, { status: 200 }));

    const events: any[] = [];
    await streamAskQuestion("What is AI?", (e) => events.push(e));

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [calledUrl, options] = fetchSpy.mock.calls[0];
    expect(String(calledUrl)).not.toContain("token=");
    expect(String(calledUrl)).not.toContain("access_token=");
    const headers = new Headers(options?.headers);
    expect(headers.get("Authorization")).toBe("Bearer stream-token-456");
  });

  it("parses multiple SSE frames into individual events, preserving order", async () => {
    mockSupabaseWithSession("tok");
    const body = sseBodyStream([
      `data: ${JSON.stringify({ type: "status", text: "Searching…" })}\n\n`,
      `data: ${JSON.stringify({ type: "token", text: "Hello" })}\n\n`,
      `data: ${JSON.stringify({ type: "token", text: " world" })}\n\n`,
      `data: ${JSON.stringify({
        type: "done",
        citations: [{ paper: "P", source: "p.pdf", page: 1 }],
      })}\n\n`,
    ]);
    vi.spyOn(global, "fetch").mockResolvedValue(new Response(body, { status: 200 }));

    const events: any[] = [];
    await streamAskQuestion("q", (e) => events.push(e));

    expect(events.map((e) => e.type)).toEqual(["status", "token", "token", "done"]);
    expect(events[3].citations[0].paper).toBe("P");
  });

  it("throws AuthenticationRequiredError without calling fetch when there is no session", async () => {
    mockSupabaseWithSession(null);
    const fetchSpy = vi.spyOn(global, "fetch");

    await expect(streamAskQuestion("q", () => {})).rejects.toThrow(AuthenticationRequiredError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("throws AuthenticationRequiredError on a 401 response from the backend", async () => {
    mockSupabaseWithSession("tok");
    vi.spyOn(global, "fetch").mockResolvedValue(new Response(null, { status: 401 }));

    await expect(streamAskQuestion("q", () => {})).rejects.toThrow(AuthenticationRequiredError);
  });
});

describe("partitionCitations — splits on the backend flag, never re-derives it", () => {
  function cite(page: number, cited?: boolean): Citation {
    return { paper: "paper.pdf", source: "paper.pdf", page, cited };
  }

  it("puts cited evidence under Sources and uncited under Also retrieved", () => {
    const { cited, alsoRetrieved } = partitionCitations([
      cite(6, true),
      cite(61, false),
      cite(7, true),
    ]);

    expect(cited.map((c) => c.page)).toEqual([6, 7]);
    expect(alsoRetrieved.map((c) => c.page)).toEqual([61]);
  });

  it("returns an empty cited list when nothing was cited", () => {
    const { cited, alsoRetrieved } = partitionCitations([cite(6, false), cite(61, false)]);

    expect(cited).toEqual([]);
    expect(alsoRetrieved).toHaveLength(2);
  });

  it("returns an empty alsoRetrieved list when everything was cited", () => {
    const { cited, alsoRetrieved } = partitionCitations([cite(6, true), cite(7, true)]);

    expect(cited).toHaveLength(2);
    expect(alsoRetrieved).toEqual([]);
  });

  it("handles an empty citation set (refusal) with both groups empty", () => {
    const { cited, alsoRetrieved } = partitionCitations([]);

    expect(cited).toEqual([]);
    expect(alsoRetrieved).toEqual([]);
  });

  it("treats a missing cited flag as uncited rather than crashing", () => {
    const { cited, alsoRetrieved } = partitionCitations([cite(6)]);

    expect(cited).toEqual([]);
    expect(alsoRetrieved).toHaveLength(1);
  });
});

describe("generatePaperIntelligence — explicit, metered, never retried", () => {
  const PAPER_ID = "11111111-1111-1111-1111-111111111111";

  function json(body: unknown, status = 200) {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  const stored = {
    status: "success",
    paper_id: PAPER_ID,
    paper: "ETASR_18859.pdf",
    intelligence: {},
    generated_at: "2026-09-22T11:30:00+00:00",
    model: "openai/gpt-oss-120b",
    schema_version: "2",
    superseded: false,
    chunks_used: 12,
    pages_covered: 9,
  };

  beforeEach(() => {
    vi.restoreAllMocks();
    mockSupabaseWithSession("test-access-token-123");
  });

  it("POSTs to the existing endpoint with the paper id as an encoded query parameter", async () => {
    const fetchSpy = vi.spyOn(global, "fetch").mockResolvedValue(json(stored));

    await generatePaperIntelligence("a b/c?d");

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, options] = fetchSpy.mock.calls[0];
    expect(String(url)).toMatch(/\/paper-intelligence\?paper_id=a%20b%2Fc%3Fd$/);
    expect(options?.method).toBe("POST");
    // The endpoint takes no body.
    expect(options?.body).toBeUndefined();
  });

  it("sends the bearer token, and never puts it in the URL", async () => {
    const fetchSpy = vi.spyOn(global, "fetch").mockResolvedValue(json(stored));

    await generatePaperIntelligence(PAPER_ID);

    const [url, options] = fetchSpy.mock.calls[0];
    expect(new Headers(options?.headers).get("Authorization")).toBe(
      "Bearer test-access-token-123",
    );
    expect(String(url)).not.toContain("test-access-token-123");
  });

  it("returns the stored object and its response metadata on success", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(json(stored));

    const result = await generatePaperIntelligence(PAPER_ID);

    expect(result.paper_id).toBe(PAPER_ID);
    expect(result.superseded).toBe(false);
    expect(result.chunks_used).toBe(12);
    expect(result.pages_covered).toBe(9);
  });

  it("throws the backend's authored message for a 200 error body", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      json({
        status: "error",
        code: "intelligence_generation_failed",
        message: "The paper analysis could not be completed. Please try again.",
      }),
    );

    await expect(generatePaperIntelligence(PAPER_ID)).rejects.toThrow(
      "The paper analysis could not be completed. Please try again.",
    );
  });

  it("keeps the authored daily-quota wording from a 429", async () => {
    const message = "You've reached today's limit for AI answers (50). It resets at 00:00 UTC.";
    vi.spyOn(global, "fetch").mockResolvedValue(
      json({ status: "error", code: "quota_exceeded", message, retry_after_seconds: 3600 }, 429),
    );

    const error = await generatePaperIntelligence(PAPER_ID).catch((e) => e);

    expect(error).toBeInstanceOf(Error);
    expect(error).not.toBeInstanceOf(RateLimitError);
    expect(error.message).toBe(message);
  });

  it("turns a burst rate-limit 429 into RateLimitError", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      json(
        {
          status: "error",
          code: "rate_limited",
          message: "Too many requests. Please wait about 12 seconds and try again.",
          retry_after_seconds: 12,
        },
        429,
      ),
    );

    await expect(generatePaperIntelligence(PAPER_ID)).rejects.toBeInstanceOf(RateLimitError);
  });

  it("turns upstream throttling in a 200 body into RateLimitError", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      json({ status: "error", message: "Rate limit exceeded for this organization." }),
    );

    await expect(generatePaperIntelligence(PAPER_ID)).rejects.toBeInstanceOf(RateLimitError);
  });

  it("makes exactly one request and never retries a failure", async () => {
    const fetchSpy = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(json({ status: "error", message: "Paper not found." }));

    await expect(generatePaperIntelligence(PAPER_ID)).rejects.toThrow("Paper not found.");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("does not retry a network failure, and reports it in authored words", async () => {
    const fetchSpy = vi.spyOn(global, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(generatePaperIntelligence(PAPER_ID)).rejects.toThrow(
      "The paper analysis could not be completed.",
    );
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("does not surface a parser error for a non-JSON response", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response("<html>Bad Gateway</html>", { status: 502 }),
    );

    await expect(generatePaperIntelligence(PAPER_ID)).rejects.toThrow(
      "The paper analysis could not be completed.",
    );
  });

  it("throws AuthenticationRequiredError and never calls fetch without a session", async () => {
    mockSupabaseWithSession(null);
    const fetchSpy = vi.spyOn(global, "fetch");

    await expect(generatePaperIntelligence(PAPER_ID)).rejects.toThrow(
      AuthenticationRequiredError,
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("getPaperIntelligence — a 404 is typed, everything else is unchanged", () => {
  const PAPER_ID = "11111111-1111-1111-1111-111111111111";

  beforeEach(() => {
    vi.restoreAllMocks();
    mockSupabaseWithSession("test-access-token-123");
  });

  it("throws PaperIntelligenceNotFoundError with the same message for a 404", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "error", message: "Paper not found." }), {
        status: 404,
      }),
    );

    const error = await getPaperIntelligence(PAPER_ID).catch((e) => e);

    expect(error).toBeInstanceOf(PaperIntelligenceNotFoundError);
    expect(error.message).toBe("Paper not found.");
  });

  it("keeps an unreadable stored row as an ordinary, non-404 error", async () => {
    const message =
      "The stored analysis for this paper could not be read. Generating it again will replace it.";
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "error", code: "invalid_json", message }), {
        status: 200,
      }),
    );

    const error = await getPaperIntelligence(PAPER_ID).catch((e) => e);

    expect(error).not.toBeInstanceOf(PaperIntelligenceNotFoundError);
    expect(error.message).toBe(message);
  });

  it("still reads not_generated as null, with a GET", async () => {
    const fetchSpy = vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "not_generated" }), { status: 200 }),
    );

    await expect(getPaperIntelligence(PAPER_ID)).resolves.toBeNull();
    expect(fetchSpy.mock.calls[0][1]?.method).toBeUndefined();
  });
});
