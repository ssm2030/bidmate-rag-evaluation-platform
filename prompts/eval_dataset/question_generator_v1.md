# Question generator v1

Generate at most five Schema v2 candidate questions from locked evidence. Do not invent facts, pages, document identifiers, or reasoning traces.

Create exactly one question matching the requested type and difficulty. Every material answer claim must cite a supplied `window_id` and an exact quote copied from that window's text.
Do not invent a page, document identifier, quote offset, or bounding box. Return only the required strict JSON object.
Do not include contact details or personal names from the context.
