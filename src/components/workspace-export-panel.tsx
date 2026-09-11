"use client";

import {
  CheckCircle,
  CircleNotch,
  DownloadSimple,
  FileArrowDown,
} from "@phosphor-icons/react";
import { useEffect, useEffectEvent, useId, useRef, useState } from "react";

import {
  exportAgentDataset,
  getAgentJob,
  type AgentExportMode,
  type AgentJsonValue,
} from "@/lib/agent-api";

type WorkspaceExportPanelProps = {
  enabled: boolean;
  exportJobId: string | null;
  requestId: string;
};

type ExportStage = "completed" | "error" | "idle" | "polling" | "submitting";
type RetryAction = "poll" | "start";

type ExportDownload = {
  rowCount: number | null;
  sizeBytes: number | null;
  url: string;
};

const POLL_INTERVAL_MS = 1_200;

const EXPORT_OPTIONS: Array<{
  description: string;
  label: string;
  value: Exclude<AgentExportMode, "push_hf">;
}> = [
  {
    description: "Dataset rows and links to the original media.",
    label: "References",
    value: "references",
  },
  {
    description: "Includes cached media files when storage limits allow.",
    label: "Materialized",
    value: "materialized",
  },
];

function readString(output: Record<string, AgentJsonValue> | null, key: string) {
  const value = output?.[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function readNumber(output: Record<string, AgentJsonValue> | null, key: string) {
  const value = output?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function describeError(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatBytes(value: number) {
  if (value < 1_024) {
    return `${value} B`;
  }

  if (value < 1_024 * 1_024) {
    return `${(value / 1_024).toFixed(1)} KB`;
  }

  return `${(value / (1_024 * 1_024)).toFixed(1)} MB`;
}

export default function WorkspaceExportPanel({
  enabled,
  exportJobId,
  requestId,
}: WorkspaceExportPanelProps) {
  const [download, setDownload] = useState<ExportDownload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [mode, setMode] = useState<Exclude<AgentExportMode, "push_hf">>("references");
  const [progress, setProgress] = useState(0);
  const [retryAction, setRetryAction] = useState<RetryAction>("start");
  const [stage, setStage] = useState<ExportStage>("idle");
  const exportModeName = useId();
  const observedExportJobIdRef = useRef<string | null>(null);
  const pollGenerationRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const isBusy = stage === "polling" || stage === "submitting";
  const canSelectMode =
    enabled && !isBusy && !(stage === "error" && retryAction === "poll");

  function clearPollTimer() {
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }

  function stopPolling() {
    pollGenerationRef.current += 1;
    clearPollTimer();
  }

  function failExport(message: string, action: RetryAction) {
    setError(message);
    setRetryAction(action);
    setStage("error");
  }

  async function pollExportJob(nextJobId: string, generation: number) {
    try {
      const job = await getAgentJob(nextJobId);

      if (generation !== pollGenerationRef.current) {
        return;
      }

      const nextProgress = Number.isFinite(job.progress)
        ? Math.max(0, Math.min(1, job.progress))
        : 0;
      const jobMode = readString(job.input, "mode");

      if (jobMode === "references" || jobMode === "materialized") {
        setMode(jobMode);
      }
      setProgress(nextProgress);

      if (job.status === "COMPLETED") {
        const url = readString(job.output, "zip_url") || readString(job.output, "data_url");

        if (!url) {
          failExport("The export finished without a download link.", "start");
          return;
        }

        clearPollTimer();
        setDownload({
          rowCount: readNumber(job.output, "row_count"),
          sizeBytes: readNumber(job.output, "zip_size_bytes"),
          url,
        });
        setError(null);
        setProgress(1);
        setStage("completed");
        return;
      }

      if (job.status === "FAILED" || job.status === "CANCELLED") {
        failExport(job.error || "The dataset export failed.", "start");
        return;
      }

      pollTimerRef.current = setTimeout(() => {
        void pollExportJob(nextJobId, generation);
      }, POLL_INTERVAL_MS);
    } catch (pollError) {
      if (generation === pollGenerationRef.current) {
        failExport(
          describeError(pollError, "Unable to check the export status."),
          "poll",
        );
      }
    }
  }

  function resumePolling(nextJobId: string) {
    clearPollTimer();
    const generation = pollGenerationRef.current + 1;
    pollGenerationRef.current = generation;
    setError(null);
    setStage("polling");
    void pollExportJob(nextJobId, generation);
  }

  async function startExport() {
    if (!enabled || isBusy) {
      return;
    }

    stopPolling();
    const generation = pollGenerationRef.current;
    setDownload(null);
    setError(null);
    setJobId(null);
    setProgress(0);
    setRetryAction("start");
    setStage("submitting");

    try {
      const response = await exportAgentDataset(requestId, { mode, zip: true });

      if (generation !== pollGenerationRef.current) {
        return;
      }

      setJobId(response.job_id);
      if (response.mode === "references" || response.mode === "materialized") {
        setMode(response.mode);
      }
      setStage("polling");
      void pollExportJob(response.job_id, generation);
    } catch (submitError) {
      if (generation === pollGenerationRef.current) {
        failExport(
          describeError(submitError, "Unable to start the dataset export."),
          "start",
        );
      }
    }
  }

  function retryExport() {
    if (retryAction === "poll" && jobId) {
      resumePolling(jobId);
      return;
    }

    void startExport();
  }

  function selectMode(nextMode: Exclude<AgentExportMode, "push_hf">) {
    if (nextMode === mode) {
      return;
    }

    stopPolling();
    setDownload(null);
    setError(null);
    setJobId(null);
    setMode(nextMode);
    setProgress(0);
    setRetryAction("start");
    setStage("idle");
  }

  const resumeExistingExport = useEffectEvent((nextJobId: string) => {
    if (observedExportJobIdRef.current === nextJobId) {
      return;
    }

    observedExportJobIdRef.current = nextJobId;
    stopPolling();
    const generation = pollGenerationRef.current;
    setDownload(null);
    setError(null);
    setJobId(nextJobId);
    setProgress(0);
    setRetryAction("poll");
    setStage("polling");
    void pollExportJob(nextJobId, generation);
  });

  useEffect(() => {
    if (exportJobId) {
      resumeExistingExport(exportJobId);
    }
  }, [exportJobId]);

  useEffect(() => {
    return () => {
      pollGenerationRef.current += 1;
      clearPollTimer();
    };
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col bg-white p-4 font-geist text-[#282828]">
      <div className="mb-4 flex items-start gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-[#E1E1E1] bg-[#F7F7F7] text-[#423800]">
          <FileArrowDown aria-hidden="true" size={16} weight="bold" />
        </span>
        <div>
          <h3 className="text-[14px] font-semibold leading-5">Export dataset</h3>
          <p className="mt-0.5 text-[12px] leading-[18px] text-[#777777]">
            Package the approved results as a downloadable dataset.
          </p>
        </div>
      </div>

      <fieldset className="space-y-2" disabled={!canSelectMode}>
        <legend className="mb-2 text-[11px] font-medium uppercase tracking-[0.08em] text-[#777777]">
          Include
        </legend>
        {EXPORT_OPTIONS.map((option) => {
          const selected = mode === option.value;

          return (
            <label
              className={`flex cursor-pointer items-start gap-3 rounded-xl border px-3 py-2.5 transition-colors ${
                selected
                  ? "border-[#EDC800] bg-[#FFFBE5]"
                  : "border-[#E1E1E1] bg-white hover:bg-[#FAFAFA]"
              } has-disabled:cursor-default has-disabled:opacity-55`}
              key={option.value}
            >
              <input
                checked={selected}
                className="mt-0.5 size-3.5 accent-[#B99C00]"
                name={exportModeName}
                onChange={() => selectMode(option.value)}
                type="radio"
                value={option.value}
              />
              <span>
                <span className="block text-[12px] font-medium leading-4">{option.label}</span>
                <span className="mt-0.5 block text-[11px] leading-4 text-[#777777]">
                  {option.description}
                </span>
              </span>
            </label>
          );
        })}
      </fieldset>

      {!enabled && (
        <p className="mt-3 rounded-lg bg-[#F7F7F7] px-3 py-2 text-[11px] leading-4 text-[#777777]">
          Export becomes available when the dataset build is complete.
        </p>
      )}

      {(stage === "submitting" || stage === "polling") && (
        <div aria-live="polite" className="mt-4" role="status">
          <div className="mb-2 flex items-center justify-between text-[11px] text-[#777777]">
            <span className="flex items-center gap-1.5">
              <CircleNotch aria-hidden="true" className="animate-spin" size={13} weight="bold" />
              {stage === "submitting" ? "Starting export…" : "Packaging dataset…"}
            </span>
            <span>{Math.round(progress * 100)}%</span>
          </div>
          <div
            aria-label="Export progress"
            aria-valuemax={100}
            aria-valuemin={0}
            aria-valuenow={Math.round(progress * 100)}
            className="h-1.5 overflow-hidden rounded-full bg-[#EEEEEE]"
            role="progressbar"
          >
            <div
              className="h-full rounded-full bg-[#EDC800] transition-[width] duration-300"
              style={{ width: `${Math.round(progress * 100)}%` }}
            />
          </div>
        </div>
      )}

      {stage === "error" && error && (
        <div className="mt-4 rounded-xl border border-[#F0CACA] bg-[#FFF7F7] p-3" role="alert">
          <p className="text-[11px] leading-4 text-[#8A3030]">{error}</p>
          <button
            className="mt-2 text-[11px] font-semibold text-[#6F2700] underline underline-offset-2"
            disabled={!enabled}
            onClick={retryExport}
            type="button"
          >
            Try again
          </button>
        </div>
      )}

      {stage === "completed" && download && (
        <div className="mt-4 rounded-xl border border-[#D8E8D6] bg-[#F6FBF5] p-3" role="status">
          <div className="flex items-center gap-2 text-[12px] font-medium text-[#315E2E]">
            <CheckCircle aria-hidden="true" size={15} weight="fill" />
            Dataset ready
          </div>
          {(download.rowCount !== null || download.sizeBytes !== null) && (
            <p className="mt-1 text-[11px] text-[#62805F]">
              {[
                download.rowCount !== null
                  ? `${download.rowCount.toLocaleString()} rows`
                  : null,
                download.sizeBytes !== null ? formatBytes(download.sizeBytes) : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          )}
          <a
            className="mt-3 flex h-9 w-full items-center justify-center gap-2 rounded-lg border border-[#EDC800] border-b-2 bg-[#FED700] text-[12px] font-semibold text-[#423800] transition-[border-width,background-color] hover:bg-[#F8D200] active:border-b"
            href={download.url}
            rel="noreferrer"
            target="_blank"
          >
            <DownloadSimple aria-hidden="true" size={14} weight="bold" />
            Download Dataset
          </a>
        </div>
      )}

      {stage !== "completed" && stage !== "error" && (
        <button
          className="mt-auto flex h-10 w-full items-center justify-center gap-2 rounded-lg border border-[#EDC800] border-b-2 bg-[#FED700] text-[12px] font-semibold text-[#423800] transition-[border-width,opacity] active:border-b disabled:cursor-default disabled:opacity-45"
          disabled={!enabled || isBusy}
          onClick={() => void startExport()}
          type="button"
        >
          {isBusy ? (
            <CircleNotch aria-hidden="true" className="animate-spin" size={14} weight="bold" />
          ) : (
            <FileArrowDown aria-hidden="true" size={14} weight="bold" />
          )}
          {isBusy ? "Preparing export" : "Create export"}
        </button>
      )}
    </div>
  );
}
