import { getSupabaseClient } from "@/lib/supabase";

export const API_BASE_URL = import.meta.env?.VITE_API_BASE_URL || "http://localhost:8000";

// ============================================================
// Auth
// ============================================================

/**
 * Thrown when a protected call is attempted with no active Supabase
 * session. Every route's existing catch/error-display logic surfaces
 * this like any other API error — there is never a silent fallback to
 * an unauthenticated request.
 */
export class AuthenticationRequiredError extends Error {
  constructor(message = "You must be signed in to do this.") {
    super(message);
    this.name = "AuthenticationRequiredError";
  }
}

async function getAccessToken(): Promise<string | null> {
  const supabase = getSupabaseClient();
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

/**
 * The ONE centralized authenticated request path. Every protected backend
 * call goes through this — do not re-implement token fetching per call
 * site or per component. Throws AuthenticationRequiredError rather than
 * silently sending an unauthenticated request when there's no session.
 */
async function authFetch(url: string, options: RequestInit = {}): Promise<Response> {
  const token = await getAccessToken();
  if (!token) {
    throw new AuthenticationRequiredError();
  }
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return fetch(url, { ...options, headers });
}

// ============================================================
// Types
// ============================================================

export interface SearchResult {
  paper: string;
  authors: string;
  abstract: string;
  text: string;
  page: number;
}

export interface Citation {
  paper: string;
  source: string;
  page: number;
  /**
   * Set by the backend: true when the answer explicitly cited this
   * passage's page. Evidence that was supplied to the model but not
   * cited is still returned (never filtered out) so the disclosed
   * evidence set stays exactly the set the model was given.
   */
  cited?: boolean;
}

/**
 * Splits the backend's evidence set into what the answer actually cited
 * versus what was retrieved and supplied but went unused. Purely a read
 * of the backend's `cited` flag — this never parses the answer text and
 * never reconstructs citations client-side.
 */
export function partitionCitations(citations: Citation[]): {
  cited: Citation[];
  alsoRetrieved: Citation[];
} {
  return {
    cited: citations.filter((c) => c.cited),
    alsoRetrieved: citations.filter((c) => !c.cited),
  };
}

export interface UploadResult {
  message: string;
  paper_id: string;
  filename: string;
  status: string;
  saved_path: string;
  duplicate?: boolean;
}

export interface IndexedPaper {
  paper_id: string;
  points: number;
}

export interface FailedPaper {
  paper_id: string;
  error: string;
}

// Matches the real /index-document response shape exactly — this
// previously declared pdfs_indexed/chunks_indexed, fields the backend
// has never actually returned, which is why the upload success banner
// used to read "Indexed undefined PDFs · undefined chunks stored in
// Qdrant" regardless of outcome.
export interface IndexResult {
  status: string;
  papers_found: number;
  papers_indexed: number;
  papers_failed: number;
  indexed: IndexedPaper[];
  failed: FailedPaper[];
}

export interface IndexOutcome {
  kind: "indexed" | "already-indexed" | "failed";
  message: string;
}

/**
 * Turns a raw /index-document response into an unambiguous outcome + a
 * human-readable message built only from fields the backend actually
 * returns. papers_indexed === 0 is NOT necessarily a failure (it also
 * means "nothing new to index, already indexed") — only papers_failed > 0
 * is a real failure.
 */
export function describeIndexResult(result: IndexResult): IndexOutcome {
  if (result.papers_indexed > 0) {
    const totalChunks = result.indexed.reduce((sum, p) => sum + (p.points ?? 0), 0);
    return {
      kind: "indexed",
      message: `Indexed ${result.papers_indexed} PDF${result.papers_indexed !== 1 ? "s" : ""} · ${totalChunks} chunk${totalChunks !== 1 ? "s" : ""} stored in Qdrant`,
    };
  }
  if (result.papers_failed > 0) {
    const failure = result.failed[0]?.error;
    return {
      kind: "failed",
      // Provider throttling text can carry billing wording, which is not
      // ours to show; genuine indexing errors still surface verbatim.
      message: isRateLimitMessage(failure)
        ? RATE_LIMITED_MESSAGE
        : failure || "Indexing failed for the uploaded paper.",
    };
  }
  return {
    kind: "already-indexed",
    message: "This paper is already indexed and ready to search and ask.",
  };
}

export interface SummarizeResult {
  status: string;
  paper: string;
  source: string;
  authors: string;
  keywords: string;
  abstract: string;
  summary: string;
  pages_found: string[];
  chunks_used: number;
}

export interface CompareResult {
  status: string;
  paper1: string;
  paper2: string;
  source1: string;
  source2: string;
  comparison: string;
  paper1_chunks: number;
  paper2_chunks: number;
}

export interface ResearchResult {
  status: string;
  query: string;
  report: string;
  citations: Citation[];
  /**
   * Null for a report restored from storage: the chunk count belongs to
   * the generation run and is not kept with the row, so the restored
   * view omits it rather than inventing a number.
   */
  chunks_used: number | null;
  sources_used: number;
}

export interface PaperDetails {
  status: string;
  paper: string;
  paper_id: string;
  source: string;
  authors: string;
  keywords: string;
  abstract: string;
  total_chunks: number;
  preview: string;
}

export interface DashboardStats {
  status: string;
  total_papers: number;
  total_chunks: number;
  recent_papers: string[];
}

export interface AskStreamEvent {
  type: "status" | "token" | "done" | "error";
  text?: string;
  citations?: Citation[];
  sources?: Citation[];
  is_followup?: boolean;
}

// ============================================================
// Core APIs
// ============================================================

/**
 * Shown wherever an upstream embedding call is throttled. Deliberately
 * provider-neutral and free of billing wording: this same text now backs
 * Ask, Compare, Reports, Paper details and indexing, not just search.
 */
const RATE_LIMITED_MESSAGE =
  "The research service is busy right now. Please wait a moment and try again.";

/**
 * Thrown when an upstream provider call is rate-limited. The backend never
 * answers 429 itself: once its own Voyage backoff is exhausted it returns
 * 200 with {"status": "error", "message": <Voyage's text>}, so both shapes
 * are recognised. The upstream message is not carried through — it can
 * contain provider billing details that do not belong in the UI.
 */
export class RateLimitError extends Error {
  constructor(message = RATE_LIMITED_MESSAGE) {
    super(message);
    this.name = "RateLimitError";
  }
}

// Upstream throttling as it reaches us through the backend's 200
// {"status": "error"} body. Quota/credit/billing variants are matched
// deliberately: they are the same "wait and try again" situation, and
// letting them fall through as ordinary errors is exactly what lets
// React Query retry straight back into the limit.
const RATE_LIMIT_MESSAGE = /rate.?limit|too many requests|quota|insufficient credit|billing/i;

function isRateLimitMessage(message: unknown): boolean {
  return typeof message === "string" && RATE_LIMIT_MESSAGE.test(message);
}

/**
 * The ONE place a backend 200 {"status": "error"} body becomes an
 * exception. Upstream throttling becomes RateLimitError, carrying our
 * wording rather than the provider's. Every other backend message is
 * passed through unchanged — those are this application's own diagnostics
 * ("Paper not found: x.pdf") and are worth showing.
 */
function throwForErrorBody(data: { message?: unknown }, fallback: string): never {
  if (isRateLimitMessage(data.message)) {
    throw new RateLimitError();
  }
  throw new Error(typeof data.message === "string" && data.message ? data.message : fallback);
}

/**
 * React Query retry rule for provider-backed queries: a throttled request
 * must never be retried, since that only spends more of the same quota.
 * Everything else keeps the default three attempts.
 */
export function retryUnlessRateLimited(failureCount: number, error: Error): boolean {
  return !(error instanceof RateLimitError) && failureCount < 3;
}

export async function searchPapers(query: string, signal?: AbortSignal): Promise<SearchResult[]> {
  const response = await authFetch(`${API_BASE_URL}/search?query=${encodeURIComponent(query)}`, {
    signal,
  });
  if (response.status === 429) {
    throw new RateLimitError();
  }
  const data = await response.json();
  if (data.status !== "success") {
    throwForErrorBody(data, "Search failed");
  }
  return data.results;
}

/**
 * Streams /ask-stream via fetch() + a manual SSE reader — deliberately
 * NOT EventSource, which cannot set custom request headers and so has no
 * way to carry Authorization: Bearer <token>. The token is sent as a
 * normal header here; it is never placed in the URL/query string.
 */
export async function streamAskQuestion(
  question: string,
  onEvent: (event: AskStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = await getAccessToken();
  if (!token) {
    throw new AuthenticationRequiredError();
  }

  const url = `${API_BASE_URL}/ask-stream?question=${encodeURIComponent(question)}`;
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });

  if (!response.ok || !response.body) {
    if (response.status === 401) {
      throw new AuthenticationRequiredError();
    }
    throw new Error(`Ask stream failed (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line, matching the backend's
    // `data: {json}\n\n` framing (generate_sse_event in ask_stream.py).
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      for (const line of rawEvent.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const jsonText = line.slice(5).trim();
        if (!jsonText) continue;
        try {
          onEvent(JSON.parse(jsonText) as AskStreamEvent);
        } catch {
          // Malformed frame — skip it, matching the previous
          // EventSource implementation's silent-skip behavior.
        }
      }
    }
  }
}

export async function uploadPaper(file: File): Promise<UploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await authFetch(`${API_BASE_URL}/upload`, {
    method: "POST",
    body: formData,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || data.detail || "Upload failed");
  }
  // Defense in depth: a "duplicate" response body must never be treated
  // as success if the underlying paper's status is actually "failed" —
  // the backend no longer returns this combination, but this call site
  // should never silently proceed to auto-indexing on it either way.
  if (data.status === "failed") {
    throw new Error(data.message || "Upload failed");
  }
  return data;
}

export async function indexDocuments(): Promise<IndexResult> {
  const response = await authFetch(`${API_BASE_URL}/index-document`, {
    method: "POST",
  });
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Indexing failed");
  }
  return data;
}

export async function getDashboardStats(): Promise<DashboardStats> {
  const response = await authFetch(`${API_BASE_URL}/stats`);
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Stats fetch failed");
  }
  return data;
}

// ============================================================
// Research APIs
// ============================================================

export async function summarizePaper(paperName: string): Promise<SummarizeResult> {
  const response = await authFetch(
    `${API_BASE_URL}/summarize-paper?paper_name=${encodeURIComponent(paperName)}`,
  );
  const data = await response.json();
  if (data.status !== "success") {
    throwForErrorBody(data, "Summarization failed");
  }
  return data;
}

export async function comparePapers(paper1: string, paper2: string): Promise<CompareResult> {
  const response = await authFetch(
    `${API_BASE_URL}/compare-papers?paper1=${encodeURIComponent(paper1)}&paper2=${encodeURIComponent(paper2)}`,
  );
  const data = await response.json();
  if (data.status !== "success") {
    throwForErrorBody(data, "Comparison failed");
  }
  return data;
}

export async function generateReport(query: string): Promise<ResearchResult> {
  const response = await authFetch(`${API_BASE_URL}/research?query=${encodeURIComponent(query)}`);
  const data = await response.json();
  if (data.status !== "success") {
    throwForErrorBody(data, "Report generation failed");
  }
  return data;
}

/**
 * The owner's most recent stored report, or null if they have none.
 *
 * Read-only: it never generates, so restoring the Reports page after a
 * refresh costs no AI quota and creates no new report row.
 */
export async function getLatestReport(): Promise<ResearchResult | null> {
  const response = await authFetch(`${API_BASE_URL}/latest-report`);

  // A new account with no report yet is an empty state, not an error.
  if (response.status === 404) {
    return null;
  }

  const data = await response.json();
  if (data.status !== "success") {
    throwForErrorBody(data, "Failed to load saved report");
  }

  const citations = data.citations ?? [];

  return {
    status: data.status,
    query: data.query,
    report: data.report_markdown,
    citations,
    // len(citations) is exactly how /research computes sources_used.
    sources_used: citations.length,
    chunks_used: null,
  };
}

export async function getPaperDetails(paperName: string): Promise<PaperDetails> {
  const response = await authFetch(
    `${API_BASE_URL}/paper-details?paper_name=${encodeURIComponent(paperName)}`,
  );
  const data = await response.json();
  if (data.status !== "success") {
    throwForErrorBody(data, "Paper details fetch failed");
  }
  return data;
}

export async function getPapers(): Promise<string[]> {
  const response = await authFetch(`${API_BASE_URL}/papers`);
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Failed to fetch papers");
  }
  return data.papers as string[];
}

export async function exportReport(): Promise<void> {
  const response = await authFetch(`${API_BASE_URL}/export-report`);
  if (!response.ok) {
    throw new Error("Export failed");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "research-report.pdf";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function deletePaper(paperName: string): Promise<void> {
  const response = await authFetch(`${API_BASE_URL}/paper/${encodeURIComponent(paperName)}`, {
    method: "DELETE",
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "Delete failed");
  }
}
