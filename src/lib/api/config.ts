/**
 * Public API configuration. Only the base URL lives in the browser bundle;
 * credentials for ingestion, Gmail, databases and model providers belong to
 * the FastAPI service and must never be referenced here.
 */
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") || null;

export const isLiveApiConfigured = API_BASE_URL !== null;
