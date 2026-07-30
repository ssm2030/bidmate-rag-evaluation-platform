"use client";

import { Filter, Link2 } from "lucide-react";

import type { ReviewItemSummary, ReviewStatus } from "@/lib/eval-review/types";

import styles from "./eval-review.module.css";

const statusLabels: Record<string, string> = {
  all: "전체",
  needs_review: "검수 필요",
  needs_anchor_fix: "근거 수정",
  draft: "작성 중",
  approved: "승인",
  rejected: "반려",
};

function statusClass(status: ReviewStatus): string {
  if (status === "approved") return styles.statusApproved;
  if (status === "rejected") return styles.statusRejected;
  if (status === "draft") return styles.statusDraft;
  return styles.statusNeeds;
}

export function ItemList({
  items,
  selectedItemId,
  onSelect,
  filters,
  onFiltersChange,
}: {
  items: ReviewItemSummary[];
  selectedItemId: string | null;
  onSelect: (itemId: string) => void;
  filters: { status: string; sopType: string; difficulty: string };
  onFiltersChange: (filters: { status: string; sopType: string; difficulty: string }) => void;
}) {
  const shown = items.filter((item) =>
    (filters.status === "all" || item.status === filters.status)
    && (filters.sopType === "all" || item.sop_type === filters.sopType)
    && (filters.difficulty === "all" || item.difficulty === filters.difficulty));

  return (
    <aside aria-label="검수 문항 큐" className={styles.pane} data-testid="review-queue-pane">
      <div className={styles.paneHeader}>
        <div><h2>검수 큐</h2><p>{shown.length} / {items.length}개 표시</p></div>
        <Filter aria-hidden="true" size={16} />
      </div>
      <div className={styles.filterStack}>
        <label className={styles.compactField}>상태
          <select
            data-testid="item-status-filter"
            onChange={(event) => onFiltersChange({ ...filters, status: event.target.value })}
            value={filters.status}
          >
            {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className={styles.compactField}>SOP 유형
          <select data-testid="sop-type-filter" onChange={(event) => onFiltersChange({ ...filters, sopType: event.target.value })} value={filters.sopType}>
            <option value="all">전체</option>
            {["A", "B", "C", "D", "E"].map((value) => <option key={value} value={value}>{value}형</option>)}
          </select>
        </label>
        <label className={styles.compactField}>난이도
          <select onChange={(event) => onFiltersChange({ ...filters, difficulty: event.target.value })} value={filters.difficulty}>
            <option value="all">전체</option>
            <option value="low">낮음</option>
            <option value="medium">중간</option>
            <option value="high">높음</option>
          </select>
        </label>
      </div>
      <div className={styles.itemList}>
        {shown.length ? shown.map((item) => (
          <button
            aria-current={item.item_id === selectedItemId ? "true" : undefined}
            className={`${styles.itemButton} ${item.item_id === selectedItemId ? styles.itemButtonActive : ""}`}
            data-testid={`item-${item.item_id}`}
            key={item.item_id}
            onClick={() => onSelect(item.item_id)}
            type="button"
          >
            <span className={styles.itemMeta}>
              <span className={styles.inlineRow} style={{ gap: 6 }}>
                <span className={`${styles.statusDot} ${statusClass(item.status)}`} />
                {statusLabels[item.status] ?? item.status}
              </span>
              <span>{item.sop_type ?? "—"}형 · {item.difficulty}</span>
            </span>
            <span className={styles.itemQuestion}>{item.question}</span>
            <span className={styles.itemMeta} style={{ marginTop: 8 }}>
              <span className={styles.inlineRow} style={{ gap: 4 }}><Link2 size={11} /> 근거 {item.anchor_count}</span>
              <span>r{item.revision}</span>
            </span>
          </button>
        )) : <div className={styles.empty}>현재 필터에 맞는 문항이 없습니다.</div>}
      </div>
    </aside>
  );
}