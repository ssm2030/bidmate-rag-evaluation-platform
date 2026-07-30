"use client";

import { CheckCircle2, Download } from "lucide-react";
import { useState } from "react";

import type { ReviewExport } from "@/lib/eval-review/types";

import styles from "./eval-review.module.css";

export function ExportDialog({
  datasetId,
  exportState,
  onExport,
}: {
  datasetId: string | null;
  exportState: ReviewExport | null;
  onExport: () => Promise<ReviewExport>;
}) {
  const [running, setRunning] = useState(false);
  const [latest, setLatest] = useState<ReviewExport | null>(null);
  if (!datasetId) return null;
  const status = latest ?? exportState;
  return (
    <div className={styles.exportControl}>
      {status ? (
        <div className={styles.exportStatus} data-testid="export-status">
          <CheckCircle2 aria-hidden="true" size={14} />
          <span>내보내기 완료</span>
          <code title={status.relative_path}>{status.checksum.slice(0, 10)}</code>
        </div>
      ) : null}
      <button
        className={styles.buttonSecondary}
        data-testid="export-legacy"
        disabled={running}
        onClick={async () => {
          setRunning(true);
          try {
            setLatest(await onExport());
          } finally {
            setRunning(false);
          }
        }}
        type="button"
      >
        <Download size={14} /> {running ? "내보내는 중…" : "승인본 내보내기"}
      </button>
    </div>
  );
}