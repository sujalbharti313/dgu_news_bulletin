import type { Page } from "@/lib/types";
import { API_BASE_URL } from "./config";

export class ApiUnavailableError extends Error {}

/** Typed GET against the FastAPI service. Throws ApiUnavailableError on any failure. */
export async function apiGet<T>(path: string, revalidateSeconds = 300): Promise<T> {
  if (!API_BASE_URL) throw new ApiUnavailableError("API base URL is not configured");
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, {
      headers: { Accept: "application/json" },
      next: { revalidate: revalidateSeconds },
    });
  } catch {
    throw new ApiUnavailableError(`Could not reach the AI//CAMPUS API at ${path}`);
  }
  if (res.status === 404) throw new NotFoundError(path);
  if (!res.ok) throw new ApiUnavailableError(`API responded with ${res.status} for ${path}`);
  return (await res.json()) as T;
}

export class NotFoundError extends Error {}

const PAGE_SIZE = 100;
const MAX_PAGES = 20;

/** Follows the API's page/pageSize pagination and returns every item (bounded to MAX_PAGES pages). */
export async function apiGetAll<T>(path: string, revalidateSeconds = 300): Promise<T[]> {
  const separator = path.includes("?") ? "&" : "?";
  const items: T[] = [];
  for (let page = 1; page <= MAX_PAGES; page++) {
    const data = await apiGet<Page<T>>(`${path}${separator}page=${page}&pageSize=${PAGE_SIZE}`, revalidateSeconds);
    items.push(...data.items);
    if (page >= data.totalPages) break;
  }
  return items;
}
