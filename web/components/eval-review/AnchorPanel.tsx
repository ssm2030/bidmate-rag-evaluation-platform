import { FileCheck2, RefreshCw, ShieldCheck } from "lucide-react";

import type { ReviewItem } from "@/lib/eval-review/types";

import styles from "./eval-review.module.css";

export function AnchorPanel({
  item,
  activeAnchorId,
  onSelect,
  onAutoResolve,
  busy,
}: {
  item: ReviewItem;
  activeAnchorId: string | null;
  onSelect: (anchorId: string) => void;
  onAutoResolve: (anchorId: string) => void;
  busy: boolean;
}) {
  if (!item.evidence_anchors.length) {
    return (
      <div className={styles.empty} data-testid="zero-anchor-state">
        <ShieldCheck size={26} />
        <p>0-anchor 안전 문항</p>
        <small>문서에 답이 없음을 검증하는 D형 문항입니다.</small>
      </div>
    );
  }
  const active = item.evidence_anchors.find((anchor) => anchor.anchor_id === activeAnchorId)
    ?? item.evidence_anchors[0];
  return (
    <>
      <div className={styles.anchorTabs} aria-label="근거 anchor 목록">
        {item.evidence_anchors.map((anchor) => (
          <button
            aria-pressed={anchor.anchor_id === active.anchor_id}
            className={`${styles.anchorTab} ${anchor.anchor_id === active.anchor_id ? styles.anchorTabActive : ""}`}
            data-testid={`anchor-${anchor.anchor_id}`}
            key={anchor.anchor_id}
            onClick={() => onSelect(anchor.anchor_id)}
            type="button"
          >
            <strong>근거 {anchor.ordinal + 1} · p.{anchor.pdf_page_number}</strong>
            <span>{anchor.resolution_status} · {anchor.resolution_method ?? "미해결"}</span>
          </button>
        ))}
      </div>
      <blockquote className={styles.quote} data-testid="anchor-quote">
        <FileCheck2 aria-hidden="true" size={15} /> {active.exact_quote}
      </blockquote>
      {active.resolution_status !== "resolved" ? (
        <button className={styles.buttonSecondary} disabled={busy} onClick={() => onAutoResolve(active.anchor_id)} type="button">
          <RefreshCw size={13} /> 자동 재탐색
        </button>
      ) : null}
    </>
  );
}