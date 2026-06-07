import { createFileRoute } from "@tanstack/react-router";
import { useRef, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Loader2,
  Upload,
  X,
} from "lucide-react";
import { uploadPaper, indexDocuments } from "@/lib/api";

export const Route = createFileRoute("/upload")({
  head: () => ({ meta: [{ title: "Upload · ResearchMind" }] }),
  component: UploadPage,
});

type FileStatus = "pending" | "uploading" | "done" | "error";

interface FileEntry {
  file: File;
  status: FileStatus;
  error?: string;
}

type Stage = "idle" | "uploading" | "indexing" | "complete" | "error";

function UploadPage() {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [stage, setStage] = useState<Stage>("idle");
  const [indexMessage, setIndexMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [isDragOver, setIsDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const addFiles = (incoming: FileList | null) => {
    if (!incoming) return;
    const pdfs = Array.from(incoming).filter(
      (f) =>
        f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")
    );
    if (pdfs.length === 0) return;
    setFiles((prev) => [
      ...prev,
      ...pdfs.map((f) => ({ file: f, status: "pending" as FileStatus })),
    ]);
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    addFiles(e.dataTransfer.files);
  };

  const handleUploadAndIndex = async () => {
    const hasPending = files.some((f) => f.status === "pending");
    if (!hasPending) return;

    setStage("uploading");
    setErrorMessage("");

    let allSuccess = true;

    for (let i = 0; i < files.length; i++) {
      if (files[i].status !== "pending") continue;

      setFiles((prev) =>
        prev.map((f, idx) =>
          idx === i ? { ...f, status: "uploading" } : f
        )
      );

      try {
        await uploadPaper(files[i].file);
        setFiles((prev) =>
          prev.map((f, idx) =>
            idx === i ? { ...f, status: "done" } : f
          )
        );
      } catch (err: any) {
        setFiles((prev) =>
          prev.map((f, idx) =>
            idx === i
              ? { ...f, status: "error", error: err.message }
              : f
          )
        );
        allSuccess = false;
      }
    }

    if (!allSuccess) {
      setStage("error");
      setErrorMessage(
        "Some files failed to upload. Fix the errors above and try again."
      );
      return;
    }

    // Auto-index after all uploads succeed
    setStage("indexing");
    try {
      const result = await indexDocuments();
      setIndexMessage(
        `Indexed ${result.pdfs_indexed} PDF${result.pdfs_indexed !== 1 ? "s" : ""} · ${result.chunks_indexed} chunks stored in Qdrant`
      );
      setStage("complete");
    } catch (err: any) {
      setStage("error");
      setErrorMessage(err.message || "Indexing failed.");
    }
  };

  const reset = () => {
    setFiles([]);
    setStage("idle");
    setIndexMessage("");
    setErrorMessage("");
  };

  const pendingCount = files.filter((f) => f.status === "pending").length;
  const isProcessing = stage === "uploading" || stage === "indexing";

  return (
    <AppShell
      title="Upload Papers"
      subtitle="Add research papers to your library"
    >
      <div className="mx-auto max-w-2xl space-y-6">
        {/* Drop Zone */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setIsDragOver(true);
          }}
          onDragLeave={() => setIsDragOver(false)}
          onDrop={handleDrop}
          onClick={() => inputRef.current?.click()}
          className={`relative flex cursor-pointer flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed p-12 text-center transition-colors ${
            isDragOver
              ? "border-primary bg-primary/5"
              : "border-border bg-surface hover:border-primary/50"
          }`}
        >
          <Upload className="h-8 w-8 text-muted-foreground" />
          <div>
            <p className="text-sm font-medium">
              Drop PDFs here or click to browse
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Supports multiple PDF files
            </p>
          </div>
          <input
            ref={inputRef}
            type="file"
            accept=".pdf"
            multiple
            className="hidden"
            onChange={(e) => addFiles(e.target.files)}
          />
        </div>

        {/* File List */}
        {files.length > 0 && (
          <div className="space-y-2">
            {files.map((entry, i) => (
              <div
                key={i}
                className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3"
              >
                <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="flex-1 truncate text-sm">
                  {entry.file.name}
                </span>
                <span className="text-xs text-muted-foreground">
                  {(entry.file.size / 1024 / 1024).toFixed(1)} MB
                </span>
                {entry.status === "pending" && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      removeFile(i);
                    }}
                    className="text-muted-foreground hover:text-foreground"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
                {entry.status === "uploading" && (
                  <Loader2 className="h-4 w-4 animate-spin text-primary" />
                )}
                {entry.status === "done" && (
                  <CheckCircle2 className="h-4 w-4 text-green-500" />
                )}
                {entry.status === "error" && (
                  <span title={entry.error}>
                    <AlertCircle className="h-4 w-4 text-destructive" />
                  </span>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Status Banners */}
        {stage === "indexing" && (
          <div className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3 text-sm">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />
            Building index — embedding and storing chunks in Qdrant…
          </div>
        )}

        {stage === "complete" && (
          <div className="flex items-center gap-3 rounded-xl border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-300">
            <CheckCircle2 className="h-4 w-4 shrink-0" />
            {indexMessage} — papers are ready to search and ask.
          </div>
        )}

        {stage === "error" && errorMessage && (
          <div className="flex items-center gap-3 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {errorMessage}
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-3">
          {stage !== "complete" ? (
            <Button
              onClick={handleUploadAndIndex}
              disabled={pendingCount === 0 || isProcessing}
            >
              {isProcessing && (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              )}
              {stage === "uploading"
                ? "Uploading…"
                : stage === "indexing"
                  ? "Indexing…"
                  : `Upload & Index${pendingCount > 0 ? ` (${pendingCount})` : ""}`}
            </Button>
          ) : (
            <Button onClick={reset} variant="outline">
              Upload more
            </Button>
          )}
          {files.length > 0 && stage === "idle" && (
            <Button variant="ghost" onClick={reset}>
              Clear all
            </Button>
          )}
        </div>
      </div>
    </AppShell>
  );
}