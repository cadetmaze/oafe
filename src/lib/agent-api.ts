const DEFAULT_API_URL = "http://localhost:8000";
const GUEST_ID_STORAGE_KEY = "oafe:guest-id";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const API_URL = (process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");

let memoryGuestId: string | null = null;

export type AgentJsonValue =
  | boolean
  | number
  | string
  | null
  | AgentJsonValue[]
  | { [key: string]: AgentJsonValue };

export type AgentPhase =
  | "QUEUED"
  | "PLANNING"
  | "DISCOVERING"
  | "SCREENING"
  | "SAMPLING"
  | "JUDGING"
  | "REVIEW_READY"
  | "AWAITING_REVIEW"
  | "REFINING"
  | "BUILDING"
  | "EXPORTING"
  | "DONE";

export type AgentRunStatus = "RUNNING" | "AWAITING_REVIEW" | "COMPLETED" | "FAILED";
export type AgentEventKind = "explored" | "fetching" | "labeling" | "message" | "error";
export type AgentEventRole = "assistant" | "user" | null;
export type AgentDecision = "approved" | "rejected" | "pending";
export type AgentAssetModality = "audio" | "image" | "video";
export type AgentExportMode = "materialized" | "push_hf" | "references";

export type AgentAsset = {
  id: string;
  modality: AgentAssetModality;
  source: {
    config: string | null;
    dataset: string | null;
    row: number | null;
    split: string | null;
  };
  content: {
    duration: number | null;
    fps: number | null;
    height: number | null;
    preview_uri: string | null;
    thumbnail_uri: string | null;
    uri: string | null;
    width: number | null;
  };
  caption: string | null;
  labels: string[];
  tags: string[];
  license: string | null;
  dataset_kind?: string | null;
};

export type AgentJudge = {
  confidence?: number;
  evidence?: string;
  missing?: string[];
  verdict?: "match" | "reject" | "weak";
  [key: string]: AgentJsonValue | undefined;
};

export type AgentReviewItem = AgentAsset & {
  batch: number;
  cached_uri: string | null;
  decision: AgentDecision;
  judge: AgentJudge;
  rank: number;
  score: number;
};

export type AgentResult = AgentAsset & {
  position: number;
  selection: Record<string, AgentJsonValue> | null;
};

export type AgentEvent = {
  created_at: string;
  data: Record<string, AgentJsonValue>;
  kind: AgentEventKind;
  role: AgentEventRole;
  seq: number;
  text: string;
};

export type AgentDecisionCounts = {
  approved: number;
  pending: number;
  rejected: number;
};

export type AgentRun = {
  build_job_id: string | null;
  dataset_id: string | null;
  error: string | null;
  export_job_id: string | null;
  id: string;
  job_id: string | null;
  phase: AgentPhase;
  progress: number;
  review_batch: number;
  review_size: number;
  spec: Record<string, AgentJsonValue>;
  stats: Record<string, AgentJsonValue>;
  status: AgentRunStatus;
  version_id: string | null;
};

export type AgentSource = {
  discovered_via: string | null;
  reason: string | null;
  relevance: string | null;
  repo_id: string;
  rows_indexed: number;
  rows_kept: number;
};

export type AgentRequestState = {
  counts: Partial<AgentDecisionCounts>;
  request: {
    created_at: string;
    example_count: number | null;
    filters: Record<string, AgentJsonValue>;
    id: string;
    query: string;
  };
  review: {
    batch: number;
    has_more: boolean;
    items: AgentReviewItem[];
    size: number;
  };
  run: AgentRun | null;
  sources: AgentSource[];
};

export type AgentStartResponse = {
  phase: AgentPhase;
  resumed: boolean;
  review_size: number;
  run_id: string;
};

export type AgentEventsResponse = {
  events: AgentEvent[];
  latest_seq: number;
  phase?: AgentPhase;
  status?: AgentRunStatus;
};

export type AgentDecisionInput = {
  asset_id: string;
  decision: AgentDecision;
};

export type AgentDecisionsResponse = {
  applied: number;
  counts: AgentDecisionCounts;
  ok: true;
};

export type AgentMoreResponse = {
  batch?: number;
  has_more: boolean;
  items: AgentReviewItem[];
  ok: boolean;
};

export type AgentConfirmResponse = {
  build_job_id: string;
  counts: AgentDecisionCounts;
  resumed: boolean;
};

export type AgentResultsResponse = {
  count: number;
  results: AgentResult[];
  total: number;
};

export type AgentExportOptions = {
  hf_repo?: string;
  hf_token?: string;
  mode?: AgentExportMode;
  zip?: boolean;
};

export type AgentExportResponse = {
  job_id: string;
  mode: AgentExportMode;
  resumed: boolean;
  status: AgentJobStatus;
};

export type AgentJobStatus =
  | "CANCELLED"
  | "COMPLETED"
  | "FAILED"
  | "PARTIAL"
  | "QUEUED"
  | "RUNNING";

export type AgentJob = {
  error: string | null;
  id: string;
  input: Record<string, AgentJsonValue>;
  output: Record<string, AgentJsonValue> | null;
  progress: number;
  status: AgentJobStatus;
  type: string;
};

export type AgentMessageResponse = {
  ok: true;
  reply: string;
  seq: number;
};

type AgentApiErrorOptions = {
  cause?: unknown;
  payload?: unknown;
  status?: number;
};

export class AgentApiError extends Error {
  readonly cause?: unknown;
  readonly payload?: unknown;
  readonly status?: number;

  constructor(message: string, options: AgentApiErrorOptions = {}) {
    super(message);
    this.name = "AgentApiError";
    this.cause = options.cause;
    this.payload = options.payload;
    this.status = options.status;
  }
}

function createGuestId() {
  if (typeof globalThis.crypto?.randomUUID !== "function") {
    throw new AgentApiError("This browser cannot create a secure guest identity.");
  }

  return globalThis.crypto.randomUUID();
}

export function getAgentGuestId() {
  if (typeof window === "undefined") {
    throw new AgentApiError("The agent API client requires a browser guest identity.");
  }

  try {
    const storedGuestId = window.localStorage.getItem(GUEST_ID_STORAGE_KEY);

    if (storedGuestId && UUID_PATTERN.test(storedGuestId)) {
      memoryGuestId = storedGuestId.toLowerCase();
      return memoryGuestId;
    }

    const guestId = createGuestId();
    window.localStorage.setItem(GUEST_ID_STORAGE_KEY, guestId);
    memoryGuestId = guestId;
    return guestId;
  } catch (error) {
    if (memoryGuestId) {
      return memoryGuestId;
    }

    if (error instanceof AgentApiError) {
      throw error;
    }

    memoryGuestId = createGuestId();
    return memoryGuestId;
  }
}

function describeErrorPayload(payload: unknown) {
  if (typeof payload === "string" && payload.trim()) {
    return payload.trim();
  }

  if (!payload || typeof payload !== "object") {
    return null;
  }

  for (const key of ["detail", "error", "message"] as const) {
    const value = Reflect.get(payload, key);

    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }

    if (Array.isArray(value)) {
      const messages = value
        .map((item) => {
          if (typeof item === "string") {
            return item;
          }

          if (item && typeof item === "object" && typeof Reflect.get(item, "msg") === "string") {
            return Reflect.get(item, "msg") as string;
          }

          return null;
        })
        .filter((message): message is string => Boolean(message));

      if (messages.length > 0) {
        return messages.join("; ");
      }
    }
  }

  return null;
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  headers.set("X-Guest-Id", getAgentGuestId());

  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;

  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      cache: "no-store",
      headers,
    });
  } catch (cause) {
    throw new AgentApiError("Unable to reach the dataset agent.", { cause });
  }

  let responseText: string;

  try {
    responseText = await response.text();
  } catch (cause) {
    throw new AgentApiError("Unable to read the dataset agent response.", {
      cause,
      status: response.status,
    });
  }

  let payload: unknown;

  if (responseText.trim()) {
    try {
      payload = JSON.parse(responseText) as unknown;
    } catch (cause) {
      throw new AgentApiError(
        response.ok
          ? "The dataset agent returned an invalid response."
          : `The dataset agent request failed (${response.status}).`,
        { cause, payload: responseText, status: response.status },
      );
    }
  }

  if (!response.ok) {
    const detail = describeErrorPayload(payload);
    throw new AgentApiError(detail || `The dataset agent request failed (${response.status}).`, {
      payload,
      status: response.status,
    });
  }

  if (payload === undefined) {
    throw new AgentApiError("The dataset agent returned an empty response.", {
      status: response.status,
    });
  }

  return payload as T;
}

function requestPath(requestId: string, suffix = "") {
  return `/requests/${encodeURIComponent(requestId)}${suffix}`;
}

function boundedInteger(value: number, fallback: number, min: number, max: number) {
  const integer = Number.isFinite(value) ? Math.trunc(value) : fallback;
  return Math.max(min, Math.min(max, integer));
}

export function startAgentRequest(requestId: string) {
  return requestJson<AgentStartResponse>(requestPath(requestId, "/start"), { method: "POST" });
}

export function getAgentRequestState(requestId: string) {
  return requestJson<AgentRequestState>(requestPath(requestId));
}

export function getAgentRequestEvents(requestId: string, since = 0) {
  const search = new URLSearchParams({ since: String(boundedInteger(since, 0, 0, Number.MAX_SAFE_INTEGER)) });
  return requestJson<AgentEventsResponse>(`${requestPath(requestId, "/events")}?${search}`);
}

export function submitAgentDecisions(requestId: string, decisions: AgentDecisionInput[]) {
  return requestJson<AgentDecisionsResponse>(requestPath(requestId, "/decisions"), {
    body: JSON.stringify({ decisions }),
    method: "POST",
  });
}

export function getMoreAgentReviewItems(requestId: string) {
  return requestJson<AgentMoreResponse>(requestPath(requestId, "/more"), { method: "POST" });
}

export function confirmAgentReview(requestId: string) {
  return requestJson<AgentConfirmResponse>(requestPath(requestId, "/confirm"), { method: "POST" });
}

export function getAgentRequestResults(requestId: string, limit = 120) {
  const search = new URLSearchParams({ limit: String(boundedInteger(limit, 120, 1, 500)) });
  return requestJson<AgentResultsResponse>(`${requestPath(requestId, "/results")}?${search}`);
}

export function exportAgentDataset(requestId: string, options: AgentExportOptions = {}) {
  return requestJson<AgentExportResponse>(requestPath(requestId, "/export"), {
    body: JSON.stringify({ mode: "references", zip: true, ...options }),
    method: "POST",
  });
}

export function getAgentJob(jobId: string) {
  return requestJson<AgentJob>(`/jobs/${encodeURIComponent(jobId)}`);
}

export function sendAgentMessage(requestId: string, text: string) {
  return requestJson<AgentMessageResponse>(requestPath(requestId, "/message"), {
    body: JSON.stringify({ text }),
    method: "POST",
  });
}

export const agentApi = {
  confirm: confirmAgentReview,
  decisions: submitAgentDecisions,
  events: getAgentRequestEvents,
  export: exportAgentDataset,
  job: getAgentJob,
  message: sendAgentMessage,
  more: getMoreAgentReviewItems,
  results: getAgentRequestResults,
  start: startAgentRequest,
  status: getAgentRequestState,
};
