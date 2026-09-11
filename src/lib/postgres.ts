import { randomUUID } from "node:crypto";

import { Pool, type PoolClient } from "pg";

import type { DatasetRequestPayload } from "@/types/dataset-request";

type JsonObject = { [key: string]: unknown };

type PersistedUpload = {
  bytes: Buffer;
  lastModified: number | null;
  name: string;
  size: number;
  type: string;
};

type StoredDatasetRequest = {
  createdAt: string;
  id: string;
};

export type DatasetRequestSummary = {
  id: string;
  query: string;
};

class DatabaseConfigurationError extends Error {
  constructor() {
    super("DATABASE_URL is not configured");
    this.name = "DatabaseConfigurationError";
  }
}

const globalForPostgres = globalThis as typeof globalThis & {
  oafePostgresPool?: Pool;
};

function getPostgresConnectionOptions(connectionString: string) {
  try {
    const url = new URL(connectionString);
    const sslMode = url.searchParams.get("sslmode");

    if (!sslMode) {
      return { connectionString };
    }

    url.searchParams.delete("sslmode");

    return {
      connectionString: url.toString(),
      ssl:
        sslMode === "disable"
          ? undefined
          : { rejectUnauthorized: sslMode === "verify-full" },
    };
  } catch {
    return { connectionString };
  }
}

function getPool() {
  const connectionString = process.env.DATABASE_URL;

  if (!connectionString) {
    throw new DatabaseConfigurationError();
  }

  if (!globalForPostgres.oafePostgresPool) {
    globalForPostgres.oafePostgresPool = new Pool({
      ...getPostgresConnectionOptions(connectionString),
      allowExitOnIdle: true,
      connectionTimeoutMillis: 5_000,
      idleTimeoutMillis: 30_000,
      max: 2,
    });
  }

  return globalForPostgres.oafePostgresPool;
}

function asJsonObject(value: unknown): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function getLastModified(descriptor: unknown) {
  if (
    descriptor !== null &&
    typeof descriptor === "object" &&
    "lastModified" in descriptor &&
    typeof descriptor.lastModified === "number"
  ) {
    return descriptor.lastModified;
  }

  return null;
}

async function insertUpload(client: PoolClient, requestId: string, upload: PersistedUpload, position: number) {
  await client.query(
    `
      INSERT INTO dataset_request_files (
        id,
        request_id,
        position,
        file_name,
        media_type,
        byte_size,
        last_modified_ms,
        content
      )
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    `,
    [
      randomUUID(),
      requestId,
      position,
      upload.name,
      upload.type,
      upload.size,
      upload.lastModified,
      upload.bytes,
    ],
  );
}

export async function storeDatasetRequest(payload: DatasetRequestPayload, files: File[]): Promise<StoredDatasetRequest> {
  const pool = getPool();
  const client = await pool.connect();
  const id = randomUUID();
  const uploadDescriptors = Array.isArray(payload.uploads) ? payload.uploads : [];

  try {
    await client.query("BEGIN");

    const result = await client.query<{ created_at: Date }>(
      `
        INSERT INTO dataset_requests (
          id,
          query,
          filters,
          uploads,
          request_payload,
          example_count,
          seed,
          origin_context
        )
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, $7::jsonb, $8::jsonb)
        RETURNING created_at
      `,
      [
        id,
        payload.query.trim(),
        JSON.stringify(asJsonObject(payload.filters)),
        JSON.stringify(uploadDescriptors),
        JSON.stringify(payload),
        payload.exampleCount,
        JSON.stringify(payload.seedData),
        JSON.stringify(payload.originContext),
      ],
    );

    for (const [position, file] of files.entries()) {
      const descriptorLastModified = getLastModified(uploadDescriptors[position]);
      const upload: PersistedUpload = {
        bytes: Buffer.from(await file.arrayBuffer()),
        lastModified:
          descriptorLastModified !== null
            ? descriptorLastModified
            : Number.isFinite(file.lastModified)
              ? file.lastModified
              : null,
        name: file.name,
        size: file.size,
        type: file.type || "application/octet-stream",
      };

      await insertUpload(client, id, upload, position);
    }

    await client.query("COMMIT");

    return {
      createdAt: result.rows[0].created_at.toISOString(),
      id,
    };
  } catch (error) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw error;
  } finally {
    client.release();
  }
}

export async function getDatasetRequestSummary(id: string): Promise<DatasetRequestSummary | null> {
  const result = await getPool().query<{ id: string; query: string }>(
    "SELECT id, query FROM dataset_requests WHERE id = $1 LIMIT 1",
    [id],
  );

  return result.rows[0] ?? null;
}

export { DatabaseConfigurationError };
