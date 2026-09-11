import { existsSync, readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import pg from "pg";

const envPaths = [resolve(".env.local"), resolve(".env")];

for (const envPath of envPaths) {
  if (process.env.DATABASE_URL || !existsSync(envPath)) {
    continue;
  }

  const databaseUrlLine = readFileSync(envPath, "utf8")
    .split(/\r?\n/)
    .find((line) => line.startsWith("DATABASE_URL="));

  if (databaseUrlLine) {
    process.env.DATABASE_URL = databaseUrlLine.slice("DATABASE_URL=".length).trim();
  }
}

if (!process.env.DATABASE_URL) {
  throw new Error("DATABASE_URL is required. Add it to .env, .env.local, or the shell environment.");
}

const migrationDirectory = new URL("../db/migrations/", import.meta.url);
const migrationFiles = readdirSync(migrationDirectory, { withFileTypes: true })
  .filter((entry) => entry.isFile() && entry.name.endsWith(".sql"))
  .map((entry) => entry.name)
  .sort();

if (migrationFiles.length === 0) {
  throw new Error("No SQL migrations were found in db/migrations.");
}

function withoutTransactionBoundaries(sql) {
  return sql.replace(/^\s*(?:BEGIN|COMMIT)\s*;\s*$/gim, "");
}

function getPostgresConnectionOptions(connectionString) {
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

const client = new pg.Client({
  ...getPostgresConnectionOptions(process.env.DATABASE_URL),
  connectionTimeoutMillis: 5_000,
});

try {
  await client.connect();
  await client.query("SELECT pg_advisory_lock(hashtext($1))", ["oafe_schema_migrations"]);
  await client.query(`
    CREATE TABLE IF NOT EXISTS oafe_schema_migrations (
      filename text PRIMARY KEY,
      applied_at timestamptz NOT NULL DEFAULT now()
    )
  `);

  let appliedCount = 0;

  for (const filename of migrationFiles) {
    const applied = await client.query(
      "SELECT 1 FROM oafe_schema_migrations WHERE filename = $1",
      [filename],
    );

    if (applied.rowCount) {
      continue;
    }

    const sql = withoutTransactionBoundaries(
      readFileSync(new URL(filename, migrationDirectory), "utf8"),
    );

    try {
      await client.query("BEGIN");
      await client.query(sql);
      await client.query(
        "INSERT INTO oafe_schema_migrations (filename) VALUES ($1)",
        [filename],
      );
      await client.query("COMMIT");
      appliedCount += 1;
      console.log(`Applied ${filename}`);
    } catch (error) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw new Error(`Failed to apply ${filename}`, { cause: error });
    }
  }

  console.log(
    appliedCount === 0
      ? "PostgreSQL schema is already up to date."
      : `PostgreSQL schema is up to date (${appliedCount} migration${appliedCount === 1 ? "" : "s"} applied).`,
  );
} finally {
  await client.end();
}
