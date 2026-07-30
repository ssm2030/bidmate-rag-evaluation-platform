"use client";

import { Braces, MessageSquarePlus, Plus, Trash2 } from "lucide-react";

import type { HistoryTurn, ReviewItem } from "@/lib/eval-review/types";

import styles from "./eval-review.module.css";

type Draft = Pick<
  ReviewItem,
  | "question"
  | "ground_truth_answer"
  | "task_kind"
  | "difficulty"
  | "answerability"
  | "evidence_mode"
  | "perturbation"
  | "metadata_filter"
  | "history"
  | "verification_notes"
>;

export type ReviewDraft = Draft;

export function ItemEditor({
  item,
  draft,
  onChange,
}: {
  item: ReviewItem | null;
  draft: ReviewDraft | null;
  onChange: (draft: ReviewDraft) => void;
}) {
  if (!item || !draft) {
    return <div className={styles.empty}>좌측 검수 큐에서 문항을 선택하세요.</div>;
  }
  const immutable = item.status === "approved" || item.status === "rejected";
  const set = <K extends keyof ReviewDraft>(key: K, value: ReviewDraft[K]) =>
    onChange({ ...draft, [key]: value });
  const metadata = Object.entries(draft.metadata_filter);
  const updateMetadata = (index: number, key: string, value: string) => {
    const entries = [...metadata];
    entries[index] = [key, value];
    set("metadata_filter", Object.fromEntries(entries));
  };
  const updateHistory = (index: number, patch: Partial<HistoryTurn>) => {
    set("history", draft.history.map((turn, current) => current === index ? { ...turn, ...patch } : turn));
  };

  return (
    <section aria-label="구조화 문항 편집기" className={styles.pane} data-sop-type={String(item.provenance.sop_type ?? "")} data-testid="review-editor-pane">
      <div className={styles.paneHeader}>
        <div>
          <h2>문항 편집</h2>
          <p>Revision {item.revision} · {item.status} · {String(item.provenance.sop_type ?? "—")}형</p>
        </div>
        <span className={styles.badge}>{item.document_scope === "multi" ? "다중 문서" : "단일 문서"}</span>
      </div>
      <div className={styles.editorScroll}>
        <div className={styles.editorForm}>
          <label className={styles.field}>질문
            <textarea
              data-testid="review-question"
              onChange={(event) => set("question", event.target.value)}
              readOnly={immutable}
              value={draft.question}
            />
          </label>
          <label className={styles.field}>정답
            <textarea
              data-testid="review-answer"
              onChange={(event) => set("ground_truth_answer", event.target.value)}
              readOnly={immutable}
              value={draft.ground_truth_answer}
            />
          </label>
          <div className={styles.formGridThree}>
            <label className={styles.field}>작업 유형
              <select disabled={immutable} onChange={(event) => set("task_kind", event.target.value)} value={draft.task_kind}>
                {["extract", "compare", "summarize", "calculate", "follow_up"].map((value) => <option key={value}>{value}</option>)}
              </select>
            </label>
            <label className={styles.field}>난이도
              <select data-testid="review-difficulty" disabled={immutable} onChange={(event) => set("difficulty", event.target.value)} value={draft.difficulty}>
                {["low", "medium", "high"].map((value) => <option key={value}>{value}</option>)}
              </select>
            </label>
            <label className={styles.field}>답변 가능성
              <select disabled={immutable} onChange={(event) => set("answerability", event.target.value)} value={draft.answerability}>
                {["answerable", "contradiction", "unanswerable"].map((value) => <option key={value}>{value}</option>)}
              </select>
            </label>
            <label className={styles.field}>근거 방식
              <select disabled={immutable} onChange={(event) => set("evidence_mode", event.target.value)} value={draft.evidence_mode}>
                {["direct_quote", "table", "multi_evidence", "none"].map((value) => <option key={value}>{value}</option>)}
              </select>
            </label>
            <label className={styles.field}>질문 변형
              <select disabled={immutable} onChange={(event) => set("perturbation", event.target.value)} value={draft.perturbation}>
                {["none", "typo", "abbreviation", "fragmented"].map((value) => <option key={value}>{value}</option>)}
              </select>
            </label>
          </div>

          <section className={styles.subsection}>
            <h3 className={styles.inlineRow} style={{ gap: 7 }}><Braces size={15} /> 메타데이터 필터</h3>
            {metadata.map(([key, value], index) => (
              <div className={styles.rowCard} key={`${key}-${index}`}>
                <input aria-label={`메타데이터 키 ${index + 1}`} disabled={immutable} onChange={(event) => updateMetadata(index, event.target.value, String(value))} value={key} />
                <input aria-label={`메타데이터 값 ${index + 1}`} disabled={immutable} onChange={(event) => updateMetadata(index, key, event.target.value)} value={String(value)} />
                <button aria-label="메타데이터 삭제" className={styles.iconButton} disabled={immutable} onClick={() => set("metadata_filter", Object.fromEntries(metadata.filter((_, current) => current !== index)))} type="button"><Trash2 size={13} /></button>
              </div>
            ))}
            <button className={styles.buttonSecondary} disabled={immutable} onClick={() => set("metadata_filter", { ...draft.metadata_filter, [`field_${metadata.length + 1}`]: "" })} type="button"><Plus size={13} /> 필드 추가</button>
          </section>

          <section className={styles.subsection}>
            <h3 className={styles.inlineRow} style={{ gap: 7 }}><MessageSquarePlus size={15} /> 대화 이력</h3>
            {draft.history.length ? draft.history.map((turn, index) => (
              <div className={styles.rowCard} key={`${turn.role}-${index}`}>
                <select aria-label={`대화 역할 ${index + 1}`} disabled={immutable} onChange={(event) => updateHistory(index, { role: event.target.value })} value={turn.role}>
                  <option value="user">user</option><option value="assistant">assistant</option>
                </select>
                <input aria-label={`대화 내용 ${index + 1}`} disabled={immutable} onChange={(event) => updateHistory(index, { content: event.target.value })} value={turn.content} />
                <button aria-label="대화 삭제" className={styles.iconButton} disabled={immutable} onClick={() => set("history", draft.history.filter((_, current) => current !== index))} type="button"><Trash2 size={13} /></button>
              </div>
            )) : <p className={styles.topMeta}>이 문항에는 대화 이력이 없습니다.</p>}
            <button className={styles.buttonSecondary} disabled={immutable} onClick={() => set("history", [...draft.history, { role: "user", content: "" }])} type="button"><Plus size={13} /> 턴 추가</button>
          </section>

          <section className={styles.subsection}>
            <h3>검증 메모</h3>
            {draft.verification_notes.map((note, index) => (
              <div className={styles.noteRow} key={`${index}-${note}`}>
                <input aria-label={`검증 메모 ${index + 1}`} disabled={immutable} onChange={(event) => set("verification_notes", draft.verification_notes.map((value, current) => current === index ? event.target.value : value))} value={note} />
                <button aria-label="메모 삭제" className={styles.iconButton} disabled={immutable} onClick={() => set("verification_notes", draft.verification_notes.filter((_, current) => current !== index))} type="button"><Trash2 size={13} /></button>
              </div>
            ))}
            <button className={styles.buttonSecondary} disabled={immutable || draft.verification_notes.length >= 5} onClick={() => set("verification_notes", [...draft.verification_notes, ""])} type="button"><Plus size={13} /> 메모 추가</button>
          </section>

          <details className={styles.subsection}>
            <summary>진단용 provenance 보기</summary>
            <pre className={styles.diagnostic}>{JSON.stringify(item.provenance, null, 2)}</pre>
          </details>
        </div>
      </div>
    </section>
  );
}