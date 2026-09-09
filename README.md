# OAFE

A visual dataset discovery prototype for describing data needs, adding seed files,
filtering results, browsing references, and saving moodboards.

Built with Next.js App Router, React, TypeScript, Tailwind CSS, and Phosphor Icons.

## Development

```bash
npm install
npm run db:migrate
npm run dev
```

Open [localhost:3000](http://localhost:3000). Changes update automatically.

Dataset requests require PostgreSQL. Set `DATABASE_URL` in `.env.local` before
running `npm run db:migrate`. Query details are stored as JSONB, while uploaded
file metadata and bytes are stored in `dataset_request_files`.

## Project structure

- `src/app/page.tsx` — home page
- `src/app/layout.tsx` — shared layout and page metadata
- `src/app/globals.css` — Tailwind import and global styles
- `public/` — static assets
- `next.config.ts` — Next.js configuration

Use `@/` to import from `src/`.

## Commands

```bash
npm run dev        # Start the development server
npm run db:migrate # Apply the PostgreSQL schema
npm run lint       # Run ESLint
npm run typecheck  # Generate route types and check TypeScript
npm run build      # Create a production build
npm start          # Serve the production build
```
