export const API_BASE_URL =
  (typeof import.meta !== "undefined" && (import.meta as any).env?.VITE_API_BASE_URL) ||
  "http://localhost:8000";

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
}

export interface AskResult {
  status: string;
  answer: string;
  citations: Citation[];
  sources: Citation[];
}

export interface UploadResult {
  message: string;
  filename: string;
  saved_path: string;
}

export interface IndexResult {
  status: string;
  message: string;
  pdfs_indexed: number;
  chunks_indexed: number;
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
  chunks_used: number;
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

// ============================================================
// Core APIs
// ============================================================

export async function searchPapers(query: string): Promise<SearchResult[]> {
  const response = await fetch(
    `${API_BASE_URL}/search?query=${encodeURIComponent(query)}`
  );
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Search failed");
  }
  return data.results;
}

export async function askQuestion(question: string): Promise<AskResult> {
  const response = await fetch(
    `${API_BASE_URL}/ask?question=${encodeURIComponent(question)}`
  );
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Ask failed");
  }
  return data;
}

export async function uploadPaper(file: File): Promise<UploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${API_BASE_URL}/upload`, {
    method: "POST",
    body: formData,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || "Upload failed");
  }
  return data;
}

export async function indexDocuments(): Promise<IndexResult> {
  const response = await fetch(`${API_BASE_URL}/index-document`);
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Indexing failed");
  }
  return data;
}

export async function getDashboardStats(): Promise<DashboardStats> {
  const response = await fetch(`${API_BASE_URL}/stats`);
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Stats fetch failed");
  }
  return data;
}

// ============================================================
// Research APIs
// ============================================================

export async function summarizePaper(
  paperName: string
): Promise<SummarizeResult> {
  const response = await fetch(
    `${API_BASE_URL}/summarize-paper?paper_name=${encodeURIComponent(paperName)}`
  );
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Summarization failed");
  }
  return data;
}

export async function comparePapers(
  paper1: string,
  paper2: string
): Promise<CompareResult> {
  const response = await fetch(
    `${API_BASE_URL}/compare-papers?paper1=${encodeURIComponent(paper1)}&paper2=${encodeURIComponent(paper2)}`
  );
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Comparison failed");
  }
  return data;
}

export async function generateReport(
  query: string
): Promise<ResearchResult> {
  const response = await fetch(
    `${API_BASE_URL}/research?query=${encodeURIComponent(query)}`
  );
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Report generation failed");
  }
  return data;
}

export async function getPaperDetails(
  paperName: string
): Promise<PaperDetails> {
  const response = await fetch(
    `${API_BASE_URL}/paper-details?paper_name=${encodeURIComponent(paperName)}`
  );
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Paper details fetch failed");
  }
  return data;
}

export async function getPapers(): Promise<string[]> {
  const response = await fetch(`${API_BASE_URL}/papers`);
  const data = await response.json();
  if (data.status !== "success") {
    throw new Error(data.message || "Failed to fetch papers");
  }
  return data.papers as string[];
}

export async function exportReport(): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/export-report`);
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
  const response = await fetch(
    `${API_BASE_URL}/paper/${encodeURIComponent(paperName)}`,
    { method: "DELETE" }
  );
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "Delete failed");
  }
}