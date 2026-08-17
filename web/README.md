# Doctask — review console

Evidence-backed obligations register UI for the Doctask contract intelligence
platform. Landing page at `/`, workspace at `/collections`.

## Development

```sh
npm install
npm run dev
```

Serves at `http://localhost:8080`. Uses in-memory mock fixtures by default —
set `VITE_API_BASE_URL` to point at the FastAPI backend instead (see
`src/api/config.ts`).

## Build

```sh
npm run build
npm run preview
```

## Stack

- TanStack Start (React, file-based routing, SSR)
- TypeScript
- TanStack Query, TanStack Table
- Zustand (local review decision state)
- Tailwind CSS, Radix UI
