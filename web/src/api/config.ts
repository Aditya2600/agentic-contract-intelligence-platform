/**
 * One switch decides where all data access goes.
 * Unset VITE_API_BASE_URL -> in-memory mock fixtures.
 * Set VITE_API_BASE_URL   -> REST calls against that base, same function signatures.
 */
const base = (import.meta.env["VITE_API_BASE_URL"] as string | undefined)?.replace(/\/$/, "");

export const API_BASE_URL = base ?? null;
export const USE_MOCKS = API_BASE_URL === null;

export async function rest<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
  return (await res.json()) as T;
}

/** Keeps mock reads asynchronous so loading states are real. */
export function delay<T>(value: T, ms = 120): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}
