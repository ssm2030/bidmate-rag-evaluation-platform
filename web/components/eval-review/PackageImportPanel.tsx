"use client";

import { AlertTriangle, Check, FileStack, Play, ShieldCheck } from "lucide-react";

import type { PackageCandidate } from "@/lib/eval-review/types";

import styles from "./eval-review.module.css";

export function PackageImportPanel({
  packages,
  busyPackageId,
  onImport,
}: {
  packages: PackageCandidate[];
  busyPackageId: string | null;
  onImport: (packageId: string) => Promise<void>;
}) {
  if (!packages.length) {
    return (
      <div className={styles.empty} data-testid="package-empty">
        <FileStack aria-hidden="true" size={28} />
        <p>configured package root에서 Schema v2 package를 찾지 못했습니다.</p>
        <small>n8n 생성기를 먼저 실행하면 이 화면에 자동으로 나타납니다.</small>
      </div>
    );
  }

  return (
    <div className={styles.cardGrid} data-testid="package-grid">
      {packages.map((candidate) => {
        const valid = candidate.status === "valid";
        return (
          <article
            className={`${styles.card} ${valid ? "" : styles.cardInvalid}`}
            data-testid={`package-card-${candidate.package_id}`}
            key={candidate.package_id}
          >
            <div className={styles.cardBar} />
            <div className={styles.cardBody}>
              <div className={styles.cardTitleRow}>
                <h4 title={candidate.name}>{candidate.name}</h4>
                <span className={`${styles.badge} ${valid ? "" : styles.badgeDanger}`}>
                  {valid ? <Check size={12} /> : <AlertTriangle size={12} />}
                  {valid ? "검증 통과" : "가져오기 차단"}
                </span>
              </div>
              <div className={styles.metricRow}>
                <div className={styles.metric}><strong>{candidate.item_count}</strong><span>문항</span></div>
                <div className={styles.metric}><strong>{candidate.document_count}</strong><span>문서</span></div>
                <div className={styles.metric}><strong>{candidate.anchor_count}</strong><span>근거</span></div>
              </div>
              <div className={styles.gateList} aria-label="package validation gates">
                {[
                  ["Schema", candidate.schema_status],
                  ["Checksum", candidate.checksum_status],
                  ["PDF hash", candidate.pdf_hash_status],
                ].map(([label, state]) => (
                  <span
                    className={`${styles.gate} ${state === "pass" ? styles.gatePass : state === "fail" ? styles.gateFail : ""}`}
                    key={label}
                  >
                    {label} · {state}
                  </span>
                ))}
              </div>
              {candidate.blocking_reason ? (
                <p className={styles.errorBanner} role="alert">{candidate.blocking_reason}</p>
              ) : null}
              <div className={styles.actionRow}>
                <button
                  className={styles.button}
                  data-testid={`import-package-${candidate.package_id}`}
                  disabled={!valid || busyPackageId !== null}
                  onClick={() => void onImport(candidate.package_id)}
                  type="button"
                >
                  <Play size={14} />
                  {busyPackageId === candidate.package_id ? "가져오는 중…" : "검수 시작"}
                </button>
                <span className={styles.topMeta}>
                  <ShieldCheck size={14} /> 로컬 전용
                </span>
              </div>
            </div>
          </article>
        );
      })}
    </div>
  );
}