export type ReviewStatus =
  | "draft"
  | "needs_anchor_fix"
  | "needs_review"
  | "approved"
  | "rejected";

export type PackageCandidate = {
  package_id: string;
  name: string;
  status: "valid" | "invalid";
  blocking_reason: string | null;
  dataset_id: string | null;
  artifact_version: number | null;
  created_at: string | null;
  batch_id: number | null;
  mode: string | null;
  document_count: number;
  item_count: number;
  anchor_count: number;
  schema_status: string;
  checksum_status: string;
  pdf_hash_status: string;
};

export type DatasetCounts = {
  total: number;
  approved: number;
  needs_review: number;
  needs_anchor_fix: number;
  draft: number;
  rejected: number;
};

export type ReviewDataset = {
  dataset_id: string;
  schema_version: string;
  artifact_version: number | null;
  imported_at: string;
  counts: DatasetCounts;
  progress_percent: number;
  last_reviewed_at: string | null;
  export_state: ReviewExport | null;
};

export type ReviewItemSummary = {
  item_id: string;
  revision: number;
  status: ReviewStatus;
  question: string;
  sop_type: string | null;
  difficulty: string;
  answerability: string;
  document_ids: string[];
  anchor_count: number;
  blocking_reason: string | null;
};

export type ReviewItemPage = {
  items: ReviewItemSummary[];
  total: number;
  page: number;
  page_size: number;
};

export type ReviewAnchor = {
  anchor_id: string;
  ordinal: number;
  exact_quote: string;
  document_id: string;
  pdf_page_number: number;
  document_sha256: string;
  required: boolean;
  role: string;
  resolution_status: "resolved" | "unresolved" | "ambiguous" | "document_changed";
  resolution_method:
    | "exact"
    | "whitespace_normalized"
    | "context_disambiguated"
    | "bbox"
    | "manual"
    | null;
  bbox: NormalizedBBox | null;
};

export type NormalizedBBox = {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  coordinate_space: "normalized_top_left";
  page_width: number;
  page_height: number;
  rotation: number;
};

export type HistoryTurn = { role: string; content: string };

export type ReviewItem = {
  item_id: string;
  revision: number;
  status: ReviewStatus;
  question: string;
  ground_truth_answer: string;
  task_kind: string;
  document_scope: string;
  difficulty: string;
  answerability: string;
  evidence_mode: string;
  perturbation: string;
  metadata_filter: Record<string, unknown>;
  history: HistoryTurn[];
  verification_notes: string[];
  provenance: Record<string, unknown>;
  evidence_anchors: ReviewAnchor[];
};

export type ReviewResume = {
  dataset_id: string;
  item_id: string;
  anchor_id: string | null;
};

export type ReviewSnapshot = {
  snapshot_id: number;
  action: "approved" | "rejected" | "forked";
  revision: number;
  created_at: string;
};

export type ReviewExport = {
  export_id: string;
  kind: string;
  relative_path: string;
  checksum: string;
  item_count: number;
  created_at?: string;
  standard?: string;
  safety?: string;
};