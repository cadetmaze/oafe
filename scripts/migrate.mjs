import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import pg from "pg";

const envPath = resolve(".env.local");

if (!process.env.DATABASE_URL && existsSync(envPath)) {
  const databaseUrlLine = readFileSync(envPath, "utf8")
    .split(/\r?\n/)
    .find((line) => line.startsWith("DATABASE_URL="));

  if (databaseUrlLine) {
    process.env.DATABASE_URL = databaseUrlLine.slice("DATABASE_URL=".length).trim();
  }
}

if (!process.env.DATABASE_URL) {
  throw new Error("DATABASE_URL is required. Add it to .env.local or the shell environment.");
}

const migration = readFileSync(
  new URL("../db/migrations/0001_create_dataset_requests.sql", import.meta.url),
  "utf8",
);
const client = new pg.Client({
  connectionString: process.env.DATABASE_URL,
  connectionTimeoutMillis: 5_000,
});

try {
  await client.connect();
  await client.query(migration);
  console.log("PostgreSQL schema is up to date.");
} finally {
  await client.end();
}
