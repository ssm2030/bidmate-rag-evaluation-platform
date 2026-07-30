"use client";

import { create } from "zustand";

import { reviewApi } from "@/lib/eval-review/api";
import type { NormalizedBBox, ReviewDataset, ReviewItem } from "@/lib/eval-review/types";

type ReviewState = {
  datasets: ReviewDataset[];
  items: ReviewItem[];
  activeDatasetId: string | null;
  activeItemId: string | null;
  error: string | null;
  loadDatasets: () => Promise<void>;
  importPackage: (packagePath: string) => Promise<void>;
  openDataset: (datasetId: string) => Promise<void>;
  selectItem: (itemId: string) => void;
  saveQuestion: (itemId: string, patch: Partial<ReviewItem> | string) => Promise<void>;
  resolveManually: (item: ReviewItem, anchorId: string, bbox: NormalizedBBox, selectedQuote: string) => Promise<void>;
  approve: (item: ReviewItem) => Promise<void>;
  fork: (item: ReviewItem) => Promise<void>;
  reject: (item: ReviewItem, reason: string) => Promise<void>;
};

const replaceItem = (items: ReviewItem[], saved: ReviewItem) =>
  items.map((item) => (item.item_id === saved.item_id ? saved : item));

export const useEvalReviewStore = create<ReviewState>((set, get) => ({
  datasets: [],
  items: [],
  activeDatasetId: null,
  activeItemId: null,
  error: null,
  loadDatasets: async () => {
    try {
      set({ datasets: await reviewApi.listDatasets(), error: null });
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Could not load local datasets" });
    }
  },
  importPackage: async (packagePath) => {
    try {
      const dataset = await reviewApi.importPackage(packagePath);
      const items = await reviewApi.listItems(dataset.dataset_id);
      set((state) => ({
        datasets: [...state.datasets.filter((entry) => entry.dataset_id !== dataset.dataset_id), dataset],
        items,
        activeDatasetId: dataset.dataset_id,
        activeItemId: items[0]?.item_id ?? null,
        error: null,
      }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Could not import package" });
    }
  },
  openDataset: async (datasetId) => {
    try {
      const items = await reviewApi.listItems(datasetId);
      set({ items, activeDatasetId: datasetId, activeItemId: items[0]?.item_id ?? null, error: null });
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Could not open dataset" });
    }
  },
  selectItem: (itemId) => set({ activeItemId: itemId }),
  saveQuestion: async (itemId, patch) => {
    const item = get().items.find((entry) => entry.item_id === itemId);
    if (!item) return;
    try {
      const saved = await reviewApi.saveDraft(itemId, item.revision, typeof patch === "string" ? { question: patch } : patch);
      set((state) => ({ items: replaceItem(state.items, saved), error: null }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Could not save draft" });
    }
  },
  resolveManually: async (item, anchorId, bbox, selectedQuote) => {
    const anchor = item.evidence_anchors.find((entry) => entry.anchor_id === anchorId);
    if (!anchor) return;
    try {
      const saved = await reviewApi.resolveAnchor(item.item_id, anchorId, item.revision, bbox, selectedQuote, anchor.pdf_page_number);
      set((state) => ({ items: replaceItem(state.items, saved), error: null }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Could not resolve evidence" });
    }
  },
  approve: async (item) => {
    try {
      const saved = await reviewApi.approve(item.item_id, item.revision);
      set((state) => ({ items: replaceItem(state.items, saved), error: null }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Approval blocked" });
    }
  },
  fork: async (item) => {
    try {
      const saved = await reviewApi.fork(item.item_id, item.revision);
      set((state) => ({ items: [...state.items, saved], activeItemId: saved.item_id, error: null }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Could not fork snapshot" });
    }
  },
  reject: async (item, reason) => {
    try {
      const saved = await reviewApi.reject(item.item_id, item.revision, reason);
      set((state) => ({ items: replaceItem(state.items, saved), error: null }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Could not reject item" });
    }
  },
}));
