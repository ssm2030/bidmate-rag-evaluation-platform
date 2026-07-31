# Evidence selector v1

Select only an exact local document quote, its 1-based physical page, and short surrounding context. Return no answer when an exact match is unavailable.

Choose only `window_id` values supplied in the input. Prefer windows that directly support a verifiable procurement question.
Do not infer a page number, document ID, quote offset, or bounding box. Return only the required strict JSON object.
Do not copy contact details or personal names into the response.
