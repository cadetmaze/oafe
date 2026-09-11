"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import DatasetReviewStack, {
  type DatasetReviewDecision,
  type DatasetReviewItem,
} from "@/components/dataset-review-stack";
import DatasetResultsGrid from "@/components/dataset-results-grid";
import WorkspaceChat, {
  type WorkspaceChatEvent,
} from "@/components/workspace-chat";
import {
  agentApi,
  type AgentEvent,
  type AgentRequestState,
  type AgentResult,
} from "@/lib/agent-api";

type DatasetWorkspaceProps = {
  initialQuery: string;
  requestId: string;
};

type RefreshOutcome = {
  failed: boolean;
  state: AgentRequestState | null;
};

const ACTIVE_POLL_MS = 1_500;
const REVIEW_POLL_MS = 6_000;
const FINAL_REFRESH_MS = 250;
const MAX_RETRY_MS = 12_000;
const RESULT_PHASES = new Set(["BUILDING", "EXPORTING", "DONE"]);
const REVIEW_WAIT_PHASES = new Set(["REVIEW_READY", "AWAITING_REVIEW"]);

function describeError(error: unknown, fallback: string) {
  return error instanceof Error && error.message.trim()
    ? error.message.trim()
    : fallback;
}

function mergeEvents(
  currentEvents: AgentEvent[],
  incomingEvents: readonly AgentEvent[],
) {
  const knownSequences = new Set(currentEvents.map(({ seq }) => seq));
  const appendedEvents = incomingEvents
    .filter(({ seq }) => !knownSequences.has(seq))
    .sort((left, right) => left.seq - right.seq);

  return appendedEvents.length > 0
    ? [...currentEvents, ...appendedEvents]
    : currentEvents;
}

function mergeResults(
  currentResults: readonly AgentResult[],
  incomingResults: readonly AgentResult[],
) {
  const incomingById = new Map(
    incomingResults.map((result) => [result.id, result]),
  );
  const knownIds = new Set(currentResults.map(({ id }) => id));
  const updatedResults = currentResults.map(
    (result) => incomingById.get(result.id) ?? result,
  );

  incomingResults.forEach((result) => {
    if (!knownIds.has(result.id)) {
      updatedResults.push(result);
      knownIds.add(result.id);
    }
  });

  return updatedResults;
}

function shouldFetchResults(state: AgentRequestState | null) {
  return Boolean(
    state?.run &&
      (RESULT_PHASES.has(state.run.phase) || state.run.version_id),
  );
}

function isAwaitingReview(state: AgentRequestState | null) {
  return Boolean(
    state?.run &&
      (state.run.status === "AWAITING_REVIEW" ||
        REVIEW_WAIT_PHASES.has(state.run.phase)),
  );
}

function isTerminal(state: AgentRequestState | null) {
  return Boolean(
    state?.run &&
      (state.run.status === "COMPLETED" ||
        state.run.status === "FAILED" ||
        state.run.phase === "DONE"),
  );
}

export default function DatasetWorkspace({
  initialQuery,
  requestId,
}: DatasetWorkspaceProps) {
  const [requestState, setRequestState] =
    useState<AgentRequestState | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [results, setResults] = useState<AgentResult[]>([]);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [hasAttemptedConnection, setHasAttemptedConnection] = useState(false);
  const [isSendingMessage, setIsSendingMessage] = useState(false);
  const mountedRef = useRef(false);
  const latestEventSequenceRef = useRef(0);
  const latestStateRef = useRef<AgentRequestState | null>(null);

  const refreshWorkspace = useCallback(async (): Promise<RefreshOutcome> => {
    const [stateResult, eventsResult] = await Promise.allSettled([
      agentApi.status(requestId),
      agentApi.events(requestId, latestEventSequenceRef.current),
    ]);

    if (!mountedRef.current) {
      return { failed: true, state: latestStateRef.current };
    }

    let nextState = latestStateRef.current;
    let failed = false;
    let pollError: string | null = null;

    if (stateResult.status === "fulfilled") {
      nextState = stateResult.value;
      latestStateRef.current = nextState;
      setRequestState(nextState);
    } else {
      failed = true;
      pollError = describeError(
        stateResult.reason,
        "Unable to load the dataset agent.",
      );
    }

    if (eventsResult.status === "fulfilled") {
      const eventResponse = eventsResult.value;
      latestEventSequenceRef.current = Math.max(
        latestEventSequenceRef.current,
        eventResponse.latest_seq,
        ...eventResponse.events.map(({ seq }) => seq),
      );
      setEvents((currentEvents) =>
        mergeEvents(currentEvents, eventResponse.events),
      );
    } else {
      failed = true;
      pollError ??= describeError(
        eventsResult.reason,
        "Unable to load agent activity.",
      );
    }

    setConnectionError(pollError);
    setHasAttemptedConnection(true);

    if (shouldFetchResults(nextState)) {
      try {
        const response = await agentApi.results(requestId);

        if (mountedRef.current) {
          setResults((currentResults) =>
            mergeResults(currentResults, response.results),
          );
          setResultsError(null);
        }
      } catch (error) {
        failed = true;
        if (mountedRef.current) {
          setResultsError(
            describeError(error, "Unable to load dataset results."),
          );
        }
      }
    }

    return { failed, state: nextState };
  }, [requestId]);

  useEffect(() => {
    const documentElement = document.documentElement;
    const body = document.body;
    const previousDocumentOverflow = documentElement.style.overflow;
    const previousDocumentOverscroll = documentElement.style.overscrollBehavior;
    const previousBodyOverflow = body.style.overflow;
    const previousBodyOverscroll = body.style.overscrollBehavior;

    documentElement.style.overflow = "hidden";
    documentElement.style.overscrollBehavior = "none";
    body.style.overflow = "hidden";
    body.style.overscrollBehavior = "none";

    return () => {
      documentElement.style.overflow = previousDocumentOverflow;
      documentElement.style.overscrollBehavior = previousDocumentOverscroll;
      body.style.overflow = previousBodyOverflow;
      body.style.overscrollBehavior = previousBodyOverscroll;
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;
    let consecutiveFailures = 0;
    let finalRefreshScheduled = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;

    function schedulePoll(delay: number) {
      if (cancelled) {
        return;
      }

      pollTimer = setTimeout(() => {
        void poll();
      }, delay);
    }

    async function poll() {
      const outcome = await refreshWorkspace();

      if (cancelled) {
        return;
      }

      if (!outcome.state?.run) {
        try {
          await agentApi.start(requestId);
        } catch (error) {
          outcome.failed = true;
          setConnectionError(
            describeError(error, "Unable to start the dataset agent."),
          );
        }
      }

      consecutiveFailures = outcome.failed
        ? Math.min(consecutiveFailures + 1, 4)
        : 0;

      if (isTerminal(outcome.state)) {
        if (!finalRefreshScheduled) {
          finalRefreshScheduled = true;
          schedulePoll(FINAL_REFRESH_MS);
        }
        return;
      }

      const baseDelay = isAwaitingReview(outcome.state)
        ? REVIEW_POLL_MS
        : ACTIVE_POLL_MS;
      const retryDelay = outcome.failed
        ? Math.min(
            MAX_RETRY_MS,
            Math.max(
              baseDelay,
              ACTIVE_POLL_MS * 2 ** consecutiveFailures,
            ),
          )
        : baseDelay;
      schedulePoll(retryDelay);
    }

    async function start() {
      try {
        await agentApi.start(requestId);
      } catch (error) {
        if (!cancelled) {
          setConnectionError(
            describeError(error, "Unable to start the dataset agent."),
          );
        }
      }

      if (!cancelled) {
        await poll();
      }
    }

    void start();

    return () => {
      cancelled = true;
      mountedRef.current = false;

      if (pollTimer !== null) {
        clearTimeout(pollTimer);
      }
    };
  }, [refreshWorkspace, requestId]);

  async function saveDecision(
    item: DatasetReviewItem,
    decision: DatasetReviewDecision,
  ) {
    const apiDecision = decision === "approve" ? "approved" : "rejected";
    const response = await agentApi.decisions(requestId, [
      { asset_id: item.id, decision: apiDecision },
    ]);

    if (!mountedRef.current) {
      return;
    }

    setRequestState((currentState) => {
      if (!currentState) {
        return currentState;
      }

      const nextState: AgentRequestState = {
        ...currentState,
        counts: response.counts,
        review: {
          ...currentState.review,
          items: currentState.review.items.map((reviewItem) =>
            reviewItem.id === item.id
              ? { ...reviewItem, decision: apiDecision }
              : reviewItem,
          ),
        },
      };
      latestStateRef.current = nextState;
      return nextState;
    });
    void refreshWorkspace();
  }

  async function showMoreReviewItems() {
    const response = await agentApi.more(requestId);

    if (!response.ok) {
      throw new Error("No more review examples are available.");
    }

    if (!mountedRef.current) {
      return;
    }

    setRequestState((currentState) => {
      if (!currentState) {
        return currentState;
      }

      const nextState: AgentRequestState = {
        ...currentState,
        review: {
          ...currentState.review,
          batch: response.batch ?? currentState.review.batch,
          has_more: response.has_more,
          items: response.items,
          size: response.items.length,
        },
      };
      latestStateRef.current = nextState;
      return nextState;
    });
    void refreshWorkspace();
  }

  async function confirmReview() {
    await agentApi.confirm(requestId);
    await refreshWorkspace();
  }

  async function sendMessage(value: string) {
    setIsSendingMessage(true);

    try {
      await agentApi.message(requestId, value);
      await refreshWorkspace();
    } finally {
      if (mountedRef.current) {
        setIsSendingMessage(false);
      }
    }
  }

  const run = requestState?.run;
  const runError = run?.error || (run?.status === "FAILED"
    ? "The dataset agent stopped before completing this request."
    : null);
  const panelError = runError ?? connectionError;
  const showResults = Boolean(
    results.length > 0 || (run && RESULT_PHASES.has(run.phase)),
  );
  const reviewItems = requestState?.review.items ?? [];
  const reviewLoading = Boolean(
    !hasAttemptedConnection ||
      (!panelError &&
        reviewItems.length === 0 &&
        (!run || run.status === "RUNNING")),
  );
  const resultLoading = Boolean(
    !panelError &&
      run &&
      (run.phase === "BUILDING" || run.phase === "EXPORTING"),
  );
  const canExport = Boolean(
    run?.version_id &&
      (run.export_job_id ||
        run.status === "COMPLETED" ||
        run.phase === "DONE"),
  );
  const chatEvents = useMemo<WorkspaceChatEvent[]>(() => {
    const remoteEvents = events.map<WorkspaceChatEvent>((event) => ({
      kind: event.kind,
      role: event.role,
      seq: event.seq,
      text: event.text,
    }));

    if (remoteEvents.length === 0 && connectionError) {
      return [
        {
          kind: "error",
          role: "assistant",
          seq: 0,
          text: connectionError,
        },
      ];
    }

    if (
      runError &&
      !remoteEvents.some(
        (event) => event.kind === "error" && event.text === runError,
      )
    ) {
      return [
        ...remoteEvents,
        {
          kind: "error",
          role: "assistant",
          seq: Number.MAX_SAFE_INTEGER,
          text: runError,
        },
      ];
    }

    return remoteEvents;
  }, [connectionError, events, runError]);

  return (
    <>
      <section
        aria-labelledby="prompt-chat-heading"
        className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-white"
      >
        <h2 className="sr-only" id="prompt-chat-heading">
          Prompt and Chat
        </h2>
        <WorkspaceChat
          canExport={canExport}
          events={chatEvents}
          exportJobId={run?.export_job_id ?? null}
          initialQuery={initialQuery}
          isThinking={
            isSendingMessage || (!hasAttemptedConnection && !connectionError)
          }
          onSendMessage={sendMessage}
          requestId={requestId}
        />
      </section>
      <section
        aria-labelledby="preview-actions-heading"
        className="min-h-0 min-w-0 overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-[#FAFAFA]"
      >
        <h2 className="sr-only" id="preview-actions-heading">
          Preview and Actions
        </h2>
        <div aria-label="Preview and action space" className="h-full min-h-0">
          {showResults ? (
            <DatasetResultsGrid
              emptyMessage={
                run?.phase === "DONE"
                  ? "The build finished without any matching rows."
                  : "Dataset results will appear here as they are built."
              }
              error={resultsError ?? panelError}
              loading={resultLoading}
              results={results}
            />
          ) : (
            <DatasetReviewStack
              error={panelError}
              hasMore={requestState?.review.has_more ?? false}
              loading={reviewLoading}
              onConfirm={confirmReview}
              onDecision={saveDecision}
              onShowMore={showMoreReviewItems}
              remoteItems={reviewItems}
              summary={{
                approved: requestState?.counts.approved ?? 0,
                rejected: requestState?.counts.rejected ?? 0,
              }}
            />
          )}
        </div>
      </section>
    </>
  );
}
