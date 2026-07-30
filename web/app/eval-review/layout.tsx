import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "BidMate 평가셋 검수",
  description: "로컬 Schema v2 평가셋 검수 워크벤치",
};

export default function EvalReviewLayout({ children }: { children: ReactNode }) {
  return children;
}