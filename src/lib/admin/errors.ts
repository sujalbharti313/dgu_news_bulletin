/** Admin API error mapping (pure; unit tested with `npm test`). */

export class AdminApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export const FRIENDLY: Record<number, string> = {
  401: "Your sign-in has expired. Sign in again.",
  403: "This account is not an editorial admin.",
  404: "Not found.",
  429: "Too many changes in a short time. Wait a minute and try again.",
  503: "The service is unavailable right now. Try again shortly.",
};

export function errorFromResponse(status: number, body: unknown): AdminApiError {
  const err = (body as { error?: { code?: unknown; message?: unknown } } | null)?.error;
  const code = typeof err?.code === "string" ? err.code : `http_${status}`;
  const message =
    typeof err?.message === "string" && err.message && status !== 401 ? err.message : FRIENDLY[status] ?? `Request failed (${status}).`;
  return new AdminApiError(status, code, message);
}

