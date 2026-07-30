import type {
  NormalizedBBox,
  PackageCandidate,
  ReviewDataset,
  ReviewExport,
  ReviewItem,
  ReviewItemPage,
  ReviewResume,
  ReviewSnapshot,
} from "./types";

const baseUrl = process.env.NEXT_PUBLIC_EVAL_REVIEW_API_BASE ?? "/review-api";
const csrfCookie = "bidmate_review_csrf";

function cookieValue(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${name}=`;
  const entry = document.cookie.split("; ").find((value) => value.startsWith(prefix));
  return entry ? decodeURIComponent(entry.slice(prefix.length)) : null;
}

async function request<T>(path: string, init?: RequestInit, mutation = false): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set("content-type", "application/json");
  if (mutation) {
    const csrf = cookieValue(csrfCookie);
    if (!csrf) throw new Error("로컬 검수 세션이 없습니다. 화면을 새로고침해 주세요.");
    headers.set("X-CSRF-Token", csrf);
  }
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    credentials: "include",
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    throw new Error(detail || response.statusText);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const reviewApi = {
  startSession: () =>
    request<{ session_id: string; expires_at: string }>("/api/session", { method: "POST" }),
  listPackages: () => request<PackageCandidate[]>("/api/packages"),
  importPackage: (packageId: string) =>
    request<ReviewDataset>(`/api/packages/${encodeURIComponent(packageId)}/import`, { method: "POST" }, true),
  listDatasets: () => request<ReviewDataset[]>("/api/datasets"),
  listItemSummaries: (datasetId: string, query = "") =>
    request<ReviewItemPage>(`/api/datasets/${datasetId}/items${query ? `?${query}` : ""}`),
  listItems: async (datasetId: string) => {
    const page = await request<ReviewItemPage>(`/api/datasets/${datasetId}/items`);
    return Promise.all(page.items.map((summary) => request<ReviewItem>(`/api/items/${summary.item_id}`)));
  },
  getItem: (itemId: string) => request<ReviewItem>(`/api/items/${itemId}`),
  getResume: (datasetId: string) => request<ReviewResume>(`/api/datasets/${datasetId}/resume`),
  setResume: (datasetId: string, itemId: string, anchorId: string | null) =>
    request<ReviewResume>(
      `/api/datasets/${datasetId}/resume`,
      { method: "PUT", body: JSON.stringify({ item_id: itemId, anchor_id: anchorId }) },
      true,
    ),
  saveDraft: (itemId: string, baseRevision: number, patch: Partial<ReviewItem>) =>
    request<ReviewItem>(
      `/api/items/${itemId}/draft`,
      { method: "PUT", body: JSON.stringify({ base_revision: baseRevision, patch }) },
      true,
    ),
  resolveAnchor: (
    itemId: string,
    anchorId: string,
    baseRevision: number,
    bbox: NormalizedBBox,
    selectedQuote: string,
    pageNumber: number,
  ) =>
    request<ReviewItem>(
      `/api/items/${itemId}/anchors/${anchorId}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({
          base_revision: baseRevision,
          method: "manual",
          bbox,
          selected_quote: selectedQuote,
          page_number: pageNumber,
        }),
      },
      true,
    ),
  autoResolveAnchor: (itemId: string, anchorId: string, baseRevision: number) =>
    request<ReviewItem>(
      `/api/items/${itemId}/anchors/${anchorId}/auto-resolve`,
      { method: "POST", body: JSON.stringify({ base_revision: baseRevision }) },
      true,
    ),
  approve: (itemId: string, baseRevision: number) =>
    request<ReviewItem>(
      `/api/items/${itemId}/approve`,
      { method: "POST", body: JSON.stringify({ base_revision: baseRevision }) },
      true,
    ),
  fork: (itemId: string, baseRevision: number) =>
    request<ReviewItem>(
      `/api/items/${itemId}/fork`,
      { method: "POST", body: JSON.stringify({ base_revision: baseRevision }) },
      true,
    ),
  reject: (itemId: string, baseRevision: number, reason: string) =>
    request<ReviewItem>(
      `/api/items/${itemId}/reject`,
      { method: "POST", body: JSON.stringify({ base_revision: baseRevision, reason }) },
      true,
    ),
  snapshots: (itemId: string) => request<ReviewSnapshot[]>(`/api/items/${itemId}/snapshots`),
  documentPdfUrl: (datasetId: string, documentId: string) =>
    `${baseUrl}/api/datasets/${datasetId}/documents/${documentId}/pdf`,
  exportLegacy: (datasetId: string) =>
    request<ReviewExport>(
      `/api/datasets/${datasetId}/exports`,
      { method: "POST" },
      true,
    ),
};