import { DatabaseConfigurationError, storeDatasetRequest } from "@/lib/postgres";
import type { DatasetRequestPayload } from "@/types/dataset-request";

export const runtime = "nodejs";

class InvalidRequestError extends Error {}

type JsonObject = { [key: string]: unknown };
const CONTENT_TYPES = new Set(["image", "video", "audio", "document", "3d"]);
const DURATIONS = new Set(["any", "under-15", "15-60", "1-5", "over-5"]);
const ORIENTATIONS = new Set(["any", "portrait", "landscape", "square"]);
const MATCH_RANGES = new Set(["very-high", "high", "balanced", "nearby"]);

function isJsonObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNullableString(value: unknown) {
  return value === null || typeof value === "string";
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function hasValidFilters(value: unknown) {
  if (!isJsonObject(value)) {
    return false;
  }

  return (
    isStringArray(value.contentTypes) &&
    value.contentTypes.every((item) => CONTENT_TYPES.has(item)) &&
    typeof value.duration === "string" &&
    DURATIONS.has(value.duration) &&
    isStringArray(value.formats) &&
    typeof value.matchRange === "string" &&
    MATCH_RANGES.has(value.matchRange) &&
    typeof value.orientation === "string" &&
    ORIENTATIONS.has(value.orientation)
  );
}

function hasValidSeedData(value: unknown) {
  if (!isJsonObject(value) || !isNullableString(value.selectedMoodboard) || !Array.isArray(value.references)) {
    return false;
  }

  return value.references.every(
    (reference) =>
      isJsonObject(reference) &&
      typeof reference.alt === "string" &&
      typeof reference.src === "string",
  );
}

function hasValidUploadDescriptors(value: unknown) {
  if (!Array.isArray(value)) {
    return false;
  }

  return value.every(
    (upload) =>
      isJsonObject(upload) &&
      typeof upload.name === "string" &&
      upload.name.length > 0 &&
      typeof upload.type === "string" &&
      typeof upload.size === "number" &&
      Number.isFinite(upload.size) &&
      upload.size >= 0 &&
      typeof upload.lastModified === "number" &&
      Number.isFinite(upload.lastModified) &&
      upload.lastModified >= 0,
  );
}

function validatePayload(value: unknown): DatasetRequestPayload {
  if (!isJsonObject(value)) {
    throw new InvalidRequestError("The request payload must be a JSON object.");
  }

  if (typeof value.query !== "string" || value.query.trim().length === 0) {
    throw new InvalidRequestError("A non-empty query is required.");
  }

  if (!Number.isInteger(value.exampleCount) || Number(value.exampleCount) < 100 || Number(value.exampleCount) > 10_000) {
    throw new InvalidRequestError("Example count must be an integer from 100 to 10000.");
  }

  if (!hasValidFilters(value.filters)) {
    throw new InvalidRequestError("Filters are incomplete or invalid.");
  }

  if (
    !isJsonObject(value.estimatedTimeMinutes) ||
    typeof value.estimatedTimeMinutes.min !== "number" ||
    typeof value.estimatedTimeMinutes.max !== "number" ||
    !Number.isFinite(value.estimatedTimeMinutes.min) ||
    !Number.isFinite(value.estimatedTimeMinutes.max) ||
    value.estimatedTimeMinutes.min < 0 ||
    value.estimatedTimeMinutes.max < value.estimatedTimeMinutes.min
  ) {
    throw new InvalidRequestError("Estimated time is incomplete or invalid.");
  }

  if (!hasValidSeedData(value.seedData)) {
    throw new InvalidRequestError("Seed data is incomplete or invalid.");
  }

  if (
    !isJsonObject(value.originContext) ||
    !isNullableString(value.originContext.category) ||
    !isNullableString(value.originContext.moodboard)
  ) {
    throw new InvalidRequestError("Origin context is incomplete or invalid.");
  }

  if (!hasValidUploadDescriptors(value.uploads)) {
    throw new InvalidRequestError("Upload metadata is incomplete or invalid.");
  }

  return value as DatasetRequestPayload;
}

function parseJson(value: string, fieldName: string): unknown {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    throw new InvalidRequestError(`${fieldName} must contain valid JSON.`);
  }
}

async function readRequest(request: Request) {
  const contentType = request.headers.get("content-type") ?? "";

  if (contentType.includes("multipart/form-data")) {
    const formData = await request.formData();
    const serializedRequest = formData.get("request");

    if (typeof serializedRequest !== "string") {
      throw new InvalidRequestError('Multipart submissions require a JSON field named "request".');
    }

    const files = formData.getAll("files").filter((entry): entry is File => typeof entry !== "string");
    const payload = validatePayload(parseJson(serializedRequest, "request"));

    if (files.length !== payload.uploads.length) {
      throw new InvalidRequestError("Every upload requires matching file bytes and metadata.");
    }

    files.forEach((file, index) => {
      const descriptor = payload.uploads[index];

      if (descriptor.name !== file.name || descriptor.size !== file.size) {
        throw new InvalidRequestError("Uploaded file metadata does not match its file bytes.");
      }
    });

    return { files, payload };
  }

  if (!contentType.includes("application/json")) {
    throw new InvalidRequestError("Use application/json or multipart/form-data.");
  }

  let body: unknown;

  try {
    body = await request.json();
  } catch {
    throw new InvalidRequestError("The request body must contain valid JSON.");
  }

  const payload = validatePayload(body);

  if (payload.uploads.length > 0) {
    throw new InvalidRequestError("JSON submissions cannot include uploads without file bytes.");
  }

  return { files: [], payload };
}

export async function POST(request: Request) {
  try {
    const { files, payload } = await readRequest(request);
    const storedRequest = await storeDatasetRequest(payload, files);

    return Response.json(
      {
        ...storedRequest,
        path: `/${storedRequest.id}`,
      },
      { status: 201 },
    );
  } catch (error) {
    if (error instanceof InvalidRequestError) {
      return Response.json({ error: error.message }, { status: 400 });
    }

    if (error instanceof DatabaseConfigurationError) {
      return Response.json({ error: "Database is not configured." }, { status: 503 });
    }

    console.error("Failed to store dataset request", error);
    return Response.json({ error: "Unable to save the dataset request." }, { status: 500 });
  }
}
