# AI//CAMPUS

A premium, 3D AI intelligence bulletin designed for the DBS Global University community (Dehradun, India).
Next.js App Router · TypeScript · Tailwind CSS 4 · React Three Fiber · Motion · Lucide.

## Run locally

```bash
npm install
npm run dev        # http://localhost:3000
npm run lint
npx tsc --noEmit
npm run build && npm start
```

## Content and data

| Source | File | Status |
| --- | --- | --- |
| Weekly stories, editions, audio briefing | `src/data/sample/stories.ts`, `src/data/sample/campus.ts` | **Sample.** Illustrative only, labelled in the UI, no original URLs |
| Campus listings | `src/data/sample/campus.ts` | **Example formats.** Not real university events |
| Tools, research papers, India ecosystem | `src/data/reference.ts` | Real, public organisations and papers with official links. No prices or benchmarks |

All pages read through `src/lib/api/index.ts`. With `NEXT_PUBLIC_API_BASE_URL` unset, the sample adapter is used.
When it is set, the app calls the FastAPI service in [`backend/`](backend/README.md) and shows error states on
failure (never silently falls back to samples). Records the API marks `provenance: "sample"` keep their SAMPLE labels.

Endpoints used (paginated collections return `{items, page, pageSize, total, totalPages}`; items match `src/lib/types.ts`):

- `GET /api/v1/editions/current` → `Edition` + `stories` (featured first)
- `GET /api/v1/editions` → `Page<Edition>` (newest first)
- `GET /api/v1/stories?edition=<slug>` → `Page<Story>`
- `GET /api/v1/stories/{slug}` → `Story` (404 when missing)
- `GET /api/v1/briefings/latest` → `Briefing` (`audioUrl` enables the player)
- `GET /api/v1/search?q=` → `Page<Story>` (PostgreSQL keyword search; semantic search is planned)
- `GET /api/v1/tools`, `/research`, `/india`, `/career-signals`, `/campus` → home-page sections

See [`backend/README.md`](backend/README.md) for running the API, Supabase setup, migrations and seeding.

Never put API keys, database credentials or OAuth secrets in this frontend.

## Brand assets

- `public/brand/dbs-global-university-logo.png`: the supplied logo, byte-for-byte unchanged. Shown on a white plate so its purple artwork keeps contrast on dark surfaces.
- `public/campus/dgu-ios-development-center.jpg`: cropped from the supplied iOS lab photo to remove the website's navigation overlay, slider arrows and caption. Not recoloured or stretched.
- The supplied website screenshot was used as a palette and identity reference only.

## Deploy on Vercel

1. Push this folder to a GitHub repository.
2. In Vercel, **Add New → Project**, import the repository. Framework preset: Next.js (defaults are correct).
3. Optionally add `NEXT_PUBLIC_API_BASE_URL` and `NEXT_PUBLIC_SITE_URL` under **Settings → Environment Variables**.
4. Deploy. Story pages are statically generated from the sample data.
