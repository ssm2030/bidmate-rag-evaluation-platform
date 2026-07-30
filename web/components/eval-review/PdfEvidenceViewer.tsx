"use client";

import { Check, ChevronLeft, ChevronRight, MousePointer2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { NormalizedBBox, ReviewAnchor } from "@/lib/eval-review/types";
import { viewportBox } from "@/lib/eval-review/pdfTextResolver";

import styles from "./eval-review.module.css";

type Selection = { bbox: NormalizedBBox; selectedQuote: string };

export function PdfEvidenceViewer({
  anchor,
  pdfUrl,
  onManualSelect,
}: {
  anchor: ReviewAnchor | undefined;
  pdfUrl?: string;
  onManualSelect?: (selection: Selection) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const textLayerRef = useRef<HTMLDivElement>(null);
  const [page, setPage] = useState(anchor?.pdf_page_number ?? 1);
  const [pageCount, setPageCount] = useState(1);
  const [size, setSize] = useState({ width: 595, height: 842 });
  const [message, setMessage] = useState("근거 PDF를 준비하고 있습니다.");
  const [pending, setPending] = useState<Selection | null>(null);

  useEffect(() => {
    setPage(anchor?.pdf_page_number ?? 1);
    setPending(null);
  }, [anchor?.anchor_id, anchor?.pdf_page_number]);

  useEffect(() => {
    if (!pdfUrl || !canvasRef.current || !textLayerRef.current) return;
    let cancelled = false;
    let phase = "문서 열기";
    setMessage("근거 PDF를 불러오는 중…");
    // @ts-expect-error pdfjs-dist does not ship a declaration for its minified browser entry.
    void import("pdfjs-dist/build/pdf.min.mjs")
      .then(async ({ getDocument, GlobalWorkerOptions, TextLayer }) => {
        GlobalWorkerOptions.workerSrc ||= new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
        const pdf = await getDocument(pdfUrl).promise;
        phase = "페이지 열기";
        const currentPage = await pdf.getPage(Math.min(page, pdf.numPages));
        const base = currentPage.getViewport({ scale: 1 });
        const scale = Math.min(1, 720 / base.width);
        const viewport = currentPage.getViewport({ scale });
        if (cancelled) return;
        const canvas = canvasRef.current!;
        const textLayer = textLayerRef.current!;
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        textLayer.replaceChildren();
        setPageCount(pdf.numPages);
        setSize({ width: viewport.width, height: viewport.height });
        phase = "PDF 렌더링";
        await currentPage.render({ canvas, canvasContext: canvas.getContext("2d")!, viewport }).promise;
        phase = "텍스트 레이어 생성";
        const textContent = await currentPage.getTextContent();
        await new TextLayer({ textContentSource: textContent, container: textLayer, viewport }).render();
        if (!cancelled) setMessage("텍스트를 드래그한 뒤 ‘선택 근거 확정’을 누르세요.");
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const detail = error instanceof Error ? error.message : "로컬 PDF를 표시하지 못했습니다.";
          setMessage(`${phase}: ${detail}`);
        }
      });
    return () => { cancelled = true; };
  }, [page, pdfUrl]);

  const captureSelection = () => {
    const selection = window.getSelection();
    const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
    const textLayer = textLayerRef.current;
    const pageNode = pageRef.current;
    if (!range || !selection || !textLayer || !pageNode || !textLayer.contains(range.commonAncestorContainer)) return;
    const selectedQuote = selection.toString().trim();
    const rects = [...range.getClientRects()];
    if (!selectedQuote || !rects.length) return;
    const pageRect = pageNode.getBoundingClientRect();
    const left = Math.min(...rects.map((rect) => rect.left));
    const top = Math.min(...rects.map((rect) => rect.top));
    const right = Math.max(...rects.map((rect) => rect.right));
    const bottom = Math.max(...rects.map((rect) => rect.bottom));
    setPending({
      selectedQuote,
      bbox: {
        x0: Math.max(0, (left - pageRect.left) / pageRect.width),
        y0: Math.max(0, (top - pageRect.top) / pageRect.height),
        x1: Math.min(1, (right - pageRect.left) / pageRect.width),
        y1: Math.min(1, (bottom - pageRect.top) / pageRect.height),
        coordinate_space: "normalized_top_left",
        page_width: size.width,
        page_height: size.height,
        rotation: 0,
      },
    });
  };

  const highlight = anchor?.bbox ? viewportBox(anchor.bbox, size.width, size.height, 1) : null;
  if (!anchor || !pdfUrl) {
    return <div className={styles.empty}>이 문항에는 표시할 PDF anchor가 없습니다.</div>;
  }

  return (
    <section aria-label="로컬 PDF 근거 뷰어">
      <div className={styles.pdfToolbar}>
        <div className={styles.actionRow}>
          <button aria-label="이전 PDF 페이지" className={styles.iconButton} disabled={page <= 1} onClick={() => setPage((value) => value - 1)} type="button"><ChevronLeft size={15} /></button>
          <strong>p. {page} / {pageCount}</strong>
          <button aria-label="다음 PDF 페이지" className={styles.iconButton} disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)} type="button"><ChevronRight size={15} /></button>
        </div>
        <span className={styles.topMeta}>{message}</span>
      </div>
      <div className={styles.pdfViewport}>
        <div
          className={styles.pdfPage}
          data-testid="pdf-page"
          onMouseUp={captureSelection}
          ref={pageRef}
          style={{ height: size.height, width: size.width }}
        >
          <canvas ref={canvasRef} style={{ display: "block", height: size.height, width: size.width }} />
          <div className={`${styles.textLayer} textLayer`} data-testid="pdf-text-layer" ref={textLayerRef} />
          {highlight ? <mark className={styles.highlight} data-testid="pdf-highlight" style={{ height: highlight.height, left: highlight.left, top: highlight.top, width: highlight.width }} /> : null}
        </div>
      </div>
      {pending ? (
        <div className={styles.quote}>
          <p><MousePointer2 size={14} /> 선택됨: {pending.selectedQuote}</p>
          <button className={styles.button} data-testid="confirm-manual-selection" onClick={() => onManualSelect?.(pending)} type="button"><Check size={14} /> 선택 근거 확정</button>
        </div>
      ) : null}
    </section>
  );
}