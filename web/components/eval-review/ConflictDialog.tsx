import { AlertCircle, X } from "lucide-react";

import styles from "./eval-review.module.css";

export function ConflictDialog({ message, onDismiss }: { message: string | null; onDismiss?: () => void }) {
  if (!message) return null;
  return (
    <div className={styles.errorBanner} data-testid="review-error" role="alert">
      <span className={styles.inlineRow} style={{ gap: 8 }}><AlertCircle size={16} />{message}</span>
      {onDismiss ? <button aria-label="오류 닫기" className={styles.iconButton} onClick={onDismiss} type="button"><X size={14} /></button> : null}
    </div>
  );
}