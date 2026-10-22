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
