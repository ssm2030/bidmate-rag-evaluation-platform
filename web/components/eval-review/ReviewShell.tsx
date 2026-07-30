"use client";

import { ArrowLeft, CheckCircle2, Database, Download, LockKeyhole, RotateCcw, Save, XCircle } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { reviewApi } from "@/lib/eval-review/api";
import type { PackageCandidate, ReviewDataset, ReviewItem, ReviewItemSummary } from "@/lib/eval-review/types";

import { AnchorPanel } from "./AnchorPanel";
import { ConflictDialog } from "./ConflictDialog";
import { ExportDialog } from "./ExportDialog";
import { ItemEditor, type ReviewDraft } from "./ItemEditor";
import { ItemList } from "./ItemList";
import { PackageImportPanel } from "./PackageImportPanel";
import { PdfEvidenceViewer } from "./PdfEvidenceViewer";
import styles from "./eval-review.module.css";

const initialFilters = { status: "all", sopType: "all", difficulty: "all" };

function draftFrom(item: ReviewItem): ReviewDraft {
  return {
    question: item.question,
    ground_truth_answer: item.ground_truth_answer,
    task_kind: item.task_kind,
    difficulty: item.difficulty,
    answerability: item.answerability,
    evidence_mode: item.evidence_mode,
    perturbation: item.perturbation,
    metadata_filter: item.metadata_filter,
    history: item.history,
    verification_notes: item.verification_notes,
  };
}

function datasetLabel(datasetId: string): string {
  return datasetId.length > 12 ? `${datasetId.slice(0, 8)}…${datasetId.slice(-4)}` : datasetId;
}

export function ReviewShell({ initialDatasetId }: { initialDatasetId?: string }) {
  const router = useRouter();
  const [packages, setPackages] = useState<PackageCandidate[]>([]);
  const [datasets, setDatasets] = useState<ReviewDataset[]>([]);
  const [summaries, setSummaries] = useState<ReviewItemSummary[]>([]);
  const [item, setItem] = useState<ReviewItem | null>(null);
  const [draft, setDraft] = useState<ReviewDraft | null>(null);
  const [activeAnchorId, setActiveAnchorId] = useState<string | null>(null);
  const [filters, setFilters] = useState(initialFilters);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  const [rejectReason, setRejectReason] = useState("");

  const activeDataset = datasets.find((dataset) => dataset.dataset_id === initialDatasetId) ?? null;
  const activeAnchor = item?.evidence_anchors.find((anchor) => anchor.anchor_id === activeAnchorId)
    ?? item?.evidence_anchors[0];

  const selectItem = useCallback(async (itemId: string, preferredAnchorId?: string | null) => {
    if (!initialDatasetId) return;
    if (dirty && item?.item_id !== itemId) {
      setError("저장되지 않은 변경이 있습니다. 먼저 저장한 뒤 다른 문항으로 이동하세요.");
      return;
    }
    setBusy("item");
    try {
      const loaded = await reviewApi.getItem(itemId);
      const anchorId = preferredAnchorId && loaded.evidence_anchors.some((anchor) => anchor.anchor_id === preferredAnchorId)
        ? preferredAnchorId
        : loaded.evidence_anchors[0]?.anchor_id ?? null;
      setItem(loaded);
      setDraft(draftFrom(loaded));
      setDirty(false);
      setSaveState("idle");
      setActiveAnchorId(anchorId);
      setRejectReason("");
      await reviewApi.setResume(initialDatasetId, itemId, anchorId);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "문항을 불러오지 못했습니다.");
    } finally {
      setBusy(null);
    }
  }, [dirty, initialDatasetId, item?.item_id]);

  const refreshSummaries = useCallback(async () => {
    if (!initialDatasetId) return;
    const page = await reviewApi.listItemSummaries(initialDatasetId);
    setSummaries(page.items);
    setDatasets(await reviewApi.listDatasets());
  }, [initialDatasetId]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoading(true);
      try {
        await reviewApi.startSession();
        const [candidatePackages, reviewDatasets] = await Promise.all([
          reviewApi.listPackages(),
          reviewApi.listDatasets(),
        ]);
        if (cancelled) return;
        setPackages(candidatePackages);
        setDatasets(reviewDatasets);
        if (initialDatasetId) {
          const [page, resume] = await Promise.all([
            reviewApi.listItemSummaries(initialDatasetId),
            reviewApi.getResume(initialDatasetId),
          ]);
          if (cancelled) return;
          setSummaries(page.items);
          const loaded = await reviewApi.getItem(resume.item_id);
          if (cancelled) return;
          setItem(loaded);
          setDraft(draftFrom(loaded));
          setActiveAnchorId(resume.anchor_id ?? loaded.evidence_anchors[0]?.anchor_id ?? null);
          await reviewApi.setResume(initialDatasetId, loaded.item_id, resume.anchor_id);
        }
        setError(null);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "검수 도구를 시작하지 못했습니다.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [initialDatasetId]);

  const importPackage = async (packageId: string) => {
    setBusy(packageId);
    try {
      const imported = await reviewApi.importPackage(packageId);
      router.push(`/eval-review/${imported.dataset_id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "package를 가져오지 못했습니다.");
      setBusy(null);
    }
  };

  const saveDraft = useCallback(async () => {
    if (!item || !draft || item.status === "approved" || item.status === "rejected") return;
    setSaveState("saving");
    try {
      const saved = await reviewApi.saveDraft(item.item_id, item.revision, draft);
      setItem(saved);
      setDraft(draftFrom(saved));
      setDirty(false);
      setSaveState("saved");
      await refreshSummaries();
      setError(null);
    } catch (cause) {
      setSaveState("idle");
      setError(cause instanceof Error ? cause.message : "초안을 저장하지 못했습니다.");
    }
  }, [draft, item, refreshSummaries]);

  const approve = useCallback(async () => {
    if (!item || dirty) {
      if (dirty) setError("승인 전에 변경사항을 저장하세요.");
      return;
    }
    setBusy("approve");
    try {
      const saved = await reviewApi.approve(item.item_id, item.revision);
      setItem(saved);
      setDraft(draftFrom(saved));
      await refreshSummaries();
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "승인 gate를 통과하지 못했습니다.");
    } finally {
      setBusy(null);
    }
  }, [dirty, item, refreshSummaries]);

  const navigateItem = useCallback((offset: number) => {
    if (!item || !summaries.length) return;
    const index = summaries.findIndex((summary) => summary.item_id === item.item_id);
    const next = summaries[Math.min(summaries.length - 1, Math.max(0, index + offset))];
    if (next && next.item_id !== item.item_id) void selectItem(next.item_id);
  }, [item, selectItem, summaries]);

  const navigateAnchor = useCallback((offset: number) => {
    if (!item?.evidence_anchors.length) return;
    const index = item.evidence_anchors.findIndex((anchor) => anchor.anchor_id === activeAnchorId);
    const next = item.evidence_anchors[Math.min(item.evidence_anchors.length - 1, Math.max(0, index + offset))];
    if (next) setActiveAnchorId(next.anchor_id);
  }, [activeAnchorId, item]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.ctrlKey && event.key.toLowerCase() === "s") { event.preventDefault(); void saveDraft(); }
      if (event.ctrlKey && event.key === "Enter") { event.preventDefault(); void approve(); }
      if (event.altKey && event.key === "ArrowLeft") { event.preventDefault(); navigateItem(-1); }
      if (event.altKey && event.key === "ArrowRight") { event.preventDefault(); navigateItem(1); }
      if (event.altKey && event.key === "ArrowUp") { event.preventDefault(); navigateAnchor(-1); }
      if (event.altKey && event.key === "ArrowDown") { event.preventDefault(); navigateAnchor(1); }
      if (event.key === "Escape") setError(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [approve, navigateAnchor, navigateItem, saveDraft]);

  const progressText = activeDataset
    ? `${activeDataset.counts.approved + activeDataset.counts.rejected} / ${activeDataset.counts.total} 완료`
    : "";
  const filteredCount = useMemo(() => summaries.filter((summary) =>
    (filters.status === "all" || summary.status === filters.status)
    && (filters.sopType === "all" || summary.sop_type === filters.sopType)
    && (filters.difficulty === "all" || summary.difficulty === filters.difficulty)).length, [filters, summaries]);

  if (loading) {
    return <main className={`${styles.app} ${styles.loading}`} data-testid="eval-review-loading"><p>로컬 검수 환경을 준비하고 있습니다…</p></main>;
  }

  if (!initialDatasetId) {
    return (
      <main className={styles.app} data-testid="eval-review-dashboard">
        <header className={styles.topbar}>
          <div className={styles.brand}><span className={styles.brandMark}><Database size={19} /></span><div><p className={styles.eyebrow}>BidMate QA Studio</p><h1 className={styles.title}>평가셋 검수 워크벤치</h1></div></div>
          <div className={styles.topMeta}><span className={styles.localDot} /> 127.0.0.1 · 외부 전송 없음</div>
        </header>
        <ConflictDialog message={error} onDismiss={() => setError(null)} />
        <div className={styles.dashboard}>
          <section className={styles.hero}>
            <h2>근거를 보고,<br />문항을 확정합니다.</h2>
            <p>n8n 생성기가 만든 Schema v2 package를 자동으로 찾습니다. 경로를 입력할 필요 없이 검증 상태를 확인하고 바로 검수를 시작하세요.</p>
          </section>
          <div className={styles.sectionHeader}><div><h3>생성 완료 package</h3><p>Schema · checksum · PDF hash가 모두 통과한 package만 가져올 수 있습니다.</p></div><span className={styles.badge}>{packages.filter((entry) => entry.status === "valid").length}개 준비됨</span></div>
          <PackageImportPanel busyPackageId={busy} onImport={importPackage} packages={packages} />
          <div className={styles.sectionHeader}><div><h3>검수 중 데이터셋</h3><p>브라우저를 닫아도 마지막 문항과 anchor 위치가 복원됩니다.</p></div></div>
          {datasets.length ? <div className={styles.cardGrid} data-testid="dataset-grid">{datasets.map((dataset) => (
            <article className={styles.card} key={dataset.dataset_id}>
              <div className={styles.cardBar} /><div className={styles.cardBody}>
                <div className={styles.cardTitleRow}><h4>{datasetLabel(dataset.dataset_id)}</h4><span className={styles.badge}>{dataset.progress_percent}%</span></div>
                <div className={styles.progressLabel}><span>승인 {dataset.counts.approved} · 검수 필요 {dataset.counts.needs_review + dataset.counts.needs_anchor_fix}</span><span>{dataset.counts.total}문항</span></div>
                <div className={styles.progressTrack}><div className={styles.progressBar} style={{ width: `${dataset.progress_percent}%` }} /></div>
                <div className={styles.actionRow} style={{ marginTop: 18 }}><button className={styles.button} data-testid={`resume-dataset-${dataset.dataset_id}`} onClick={() => router.push(`/eval-review/${dataset.dataset_id}`)} type="button"><RotateCcw size={14} /> 이어서 검수</button></div>
              </div>
            </article>
          ))}</div> : <div className={styles.empty}>아직 가져온 데이터셋이 없습니다.</div>}
        </div>
      </main>
    );
  }

  return (
    <main className={`${styles.app} ${styles.workspace}`} data-testid="eval-review-workspace">
      <header className={styles.topbar}>
        <div className={styles.brand}>
          <Link aria-label="대시보드로 돌아가기" className={styles.iconButton} href="/eval-review"><ArrowLeft size={16} /></Link>
          <div><p className={styles.eyebrow}>Review workspace</p><h1 className={styles.title}>{activeDataset ? datasetLabel(activeDataset.dataset_id) : "평가셋"}</h1></div>
        </div>
        <div className={styles.topMeta}><LockKeyhole size={14} /> local session · {progressText} · 필터 {filteredCount}개</div>
      </header>
      <ConflictDialog message={error} onDismiss={() => setError(null)} />
      <div className={styles.workspaceBody}>
        <ItemList filters={filters} items={summaries} onFiltersChange={setFilters} onSelect={(itemId) => void selectItem(itemId)} selectedItemId={item?.item_id ?? null} />
        <ItemEditor item={item} draft={draft} onChange={(value) => { setDraft(value); setDirty(true); setSaveState("idle"); }} />
        <aside aria-label="PDF 근거 검수" className={styles.pane} data-testid="review-evidence-pane">
          <div className={styles.paneHeader}><div><h2>PDF 근거</h2><p>{activeAnchor ? `문서 ${activeAnchor.document_id.slice(0, 8)} · p.${activeAnchor.pdf_page_number}` : "0-anchor 문항"}</p></div></div>
          <div className={styles.evidenceScroll}>
            {item ? <AnchorPanel activeAnchorId={activeAnchor?.anchor_id ?? null} busy={busy === "anchor"} item={item} onAutoResolve={async (anchorId) => {
              setBusy("anchor");
              try {
                const saved = await reviewApi.autoResolveAnchor(item.item_id, anchorId, item.revision);
                setItem(saved); setDraft(draftFrom(saved)); setDirty(false); await refreshSummaries(); setError(null);
              } catch (cause) { setError(cause instanceof Error ? cause.message : "근거를 재탐색하지 못했습니다."); }
              finally { setBusy(null); }
            }} onSelect={(anchorId) => { setActiveAnchorId(anchorId); void reviewApi.setResume(initialDatasetId, item.item_id, anchorId); }} /> : null}
            <PdfEvidenceViewer anchor={activeAnchor} onManualSelect={async (selection) => {
              if (!item || !activeAnchor) return;
              if (selection.selectedQuote.replace(/\s+/g, " ").trim() !== activeAnchor.exact_quote.replace(/\s+/g, " ").trim()) {
                setError("활성 anchor의 exact quote 전체를 선택해야 합니다."); return;
              }
              setBusy("anchor");
              try {
                const saved = await reviewApi.resolveAnchor(item.item_id, activeAnchor.anchor_id, item.revision, selection.bbox, selection.selectedQuote, activeAnchor.pdf_page_number);
                setItem(saved); setDraft(draftFrom(saved)); setDirty(false); await refreshSummaries(); setError(null);
              } catch (cause) { setError(cause instanceof Error ? cause.message : "수동 근거를 확정하지 못했습니다."); }
              finally { setBusy(null); }
            }} pdfUrl={activeAnchor ? reviewApi.documentPdfUrl(initialDatasetId, activeAnchor.document_id) : undefined} />
          </div>
        </aside>
      </div>
      <footer className={styles.actionBar}>
        <div><p>{dirty ? <span className={styles.unsaved}>저장되지 않은 변경</span> : saveState === "saved" ? <span className={styles.saved}>저장됨</span> : `Revision ${item?.revision ?? "—"}`}</p><p>Ctrl+S 저장 · Ctrl+Enter 승인 · Alt+←/→ 문항 · Alt+↑/↓ 근거</p></div>
        <div className={styles.actionRow}>
          {item?.status === "approved" ? <button className={styles.buttonSecondary} data-testid="fork-approved" disabled={busy !== null} onClick={async () => { setBusy("fork"); try { const saved = await reviewApi.fork(item.item_id, item.revision); await refreshSummaries(); await selectItem(saved.item_id); } catch (cause) { setError(cause instanceof Error ? cause.message : "fork에 실패했습니다."); } finally { setBusy(null); } }} type="button"><RotateCcw size={14} /> 승인본 fork</button> : null}
          {item && item.status !== "approved" && item.status !== "rejected" ? <>
            <div className={styles.rejectBox}><label className={styles.field}>반려 사유<input aria-label="반려 사유" data-testid="reject-reason" onChange={(event) => setRejectReason(event.target.value)} placeholder="필수" value={rejectReason} /></label><button className={styles.buttonDanger} data-testid="reject-item" disabled={!rejectReason.trim() || busy !== null} onClick={async () => { setBusy("reject"); try { const saved = await reviewApi.reject(item.item_id, item.revision, rejectReason); setItem(saved); setDraft(draftFrom(saved)); setDirty(false); await refreshSummaries(); setError(null); } catch (cause) { setError(cause instanceof Error ? cause.message : "반려하지 못했습니다."); } finally { setBusy(null); } }} type="button"><XCircle size={14} /> 반려</button></div>
            <button className={styles.buttonSecondary} data-testid="save-draft" disabled={!dirty || saveState === "saving" || busy !== null} onClick={() => void saveDraft()} type="button"><Save size={14} /> {saveState === "saving" ? "저장 중…" : "초안 저장"}</button>
            <button className={styles.button} data-testid="approve-item" disabled={dirty || busy !== null} onClick={() => void approve()} type="button"><CheckCircle2 size={14} /> 승인</button>
          </> : null}
          <ExportDialog
            datasetId={initialDatasetId}
            exportState={activeDataset?.export_state ?? null}
            onExport={async () => {
              try {
                const exported = await reviewApi.exportLegacy(initialDatasetId);
                setDatasets(await reviewApi.listDatasets());
                setError(null);
                return exported;
              } catch (cause) {
                const message = cause instanceof Error ? cause.message : "export가 차단되었습니다.";
                setError(message);
                throw cause;
              }
            }}
          />
          <Download aria-hidden="true" size={15} />
        </div>
      </footer>
    </main>
  );
}