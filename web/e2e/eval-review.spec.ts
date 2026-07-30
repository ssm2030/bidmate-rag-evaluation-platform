import { expect, test, type Page } from "@playwright/test";

async function selectedItemId(page: Page): Promise<string> {
  const testId = await page.locator('button[data-testid^="item-"][aria-current="true"]').getAttribute("data-testid");
  if (!testId) throw new Error("selected item id is unavailable");
  return testId.slice("item-".length);
}

async function selectSopType(page: Page, sopType: string): Promise<string> {
  await page.getByTestId("sop-type-filter").selectOption(sopType);
  const item = page.locator('button[data-testid^="item-"]').first();
  await expect(item).toBeVisible();
  await item.click();
  await expect(page.getByTestId("review-editor-pane")).toHaveAttribute("data-sop-type", sopType);
  return selectedItemId(page);
}
async function reloadWorkspace(page: Page) {
  await Promise.all([
    page.waitForResponse((response) => response.url().endsWith("/api/session") && response.status() === 201),
    page.reload(),
  ]);
  await expect(page.getByTestId("eval-review-workspace")).toBeVisible();
  await expect(page.getByTestId("review-editor-pane")).toBeVisible();
  await expect(page.getByTestId("review-error")).toHaveCount(0);
}

async function mutateWithCsrf(page: Page, path: string, method: string, body: unknown) {
  return page.evaluate(async ({ path, method, body }) => {
    const csrfEntry = document.cookie.split("; ").find((value) => value.startsWith("bidmate_review_csrf="));
    if (!csrfEntry) throw new Error("CSRF cookie is unavailable");
    const csrf = decodeURIComponent(csrfEntry.slice(csrfEntry.indexOf("=") + 1));
    const response = await fetch(`/review-api${path}`, {
      method,
      credentials: "include",
      headers: { "content-type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(`${response.status}: ${JSON.stringify(payload)}`);
    return payload;
  }, { path, method, body });
}

test("real 30-item n8n package supports the complete local review flow", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const externalRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.protocol.startsWith("http") && !["127.0.0.1", "localhost"].includes(url.hostname)) {
      externalRequests.push(request.url());
    }
  });

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/eval-review");
  await expect(page.getByTestId("eval-review-dashboard")).toBeVisible();
  await expect(page.getByTestId("package-grid")).toBeVisible();
  await expect(page.getByLabel("Local package path")).toHaveCount(0);
  const packageCard = page.locator('article[data-testid^="package-card-"]').first();
  await expect(packageCard).toContainText("30");
  await packageCard.locator('button[data-testid^="import-package-"]').click();

  await expect(page.getByTestId("eval-review-workspace")).toBeVisible();
  await expect(page.getByTestId("review-editor-pane")).toBeVisible();
  await expect(page.getByTestId("review-error")).toHaveCount(0);
  const queue = page.getByTestId("review-queue-pane");
  const editor = page.getByTestId("review-editor-pane");
  const evidence = page.getByTestId("review-evidence-pane");
  const [queueBox, editorBox, evidenceBox] = await Promise.all([
    queue.boundingBox(), editor.boundingBox(), evidence.boundingBox(),
  ]);
  expect(queueBox && editorBox && evidenceBox).toBeTruthy();
  expect(queueBox!.x + queueBox!.width).toBeLessThanOrEqual(editorBox!.x + 1);
  expect(editorBox!.x + editorBox!.width).toBeLessThanOrEqual(evidenceBox!.x + 1);
  expect(evidenceBox!.x + evidenceBox!.width).toBeLessThanOrEqual(1281);
  await expect(page.getByTestId("review-question")).not.toHaveValue("");
  await expect(page.getByTestId("pdf-text-layer").locator("span").first()).toBeVisible({ timeout: 30_000 });
  await page.screenshot({ path: testInfo.outputPath("review-workspace.png"), fullPage: true });

  const resumeItem = await selectedItemId(page);
  await reloadWorkspace(page);
  await expect(page.getByTestId(`item-${resumeItem}`)).toHaveAttribute("aria-current", "true");

  for (const sopType of ["A", "B", "C", "D", "E"]) {
    await selectSopType(page, sopType);
  }

  await selectSopType(page, "B");
  await expect(page.getByTestId("review-editor-pane")).toContainText("다중 문서");
  await expect(page.locator('button[data-testid^="anchor-"]')).toHaveCount(2);
  await expect(page.locator('button[data-testid^="anchor-"]').first()).toHaveAttribute("aria-pressed", "true");

  await selectSopType(page, "C");
  const historyInput = page.getByLabel("대화 내용 1");
  await expect(historyInput).not.toHaveValue("");
  await historyInput.fill(`${await historyInput.inputValue()} · 검수 확인`);
  await page.keyboard.press("Control+s");
  await expect(page.getByText("저장됨")).toBeVisible();

  await selectSopType(page, "D");
  await expect(page.getByTestId("zero-anchor-state")).toBeVisible();
  await page.keyboard.press("Control+Enter");
  await expect(page.getByTestId("fork-approved")).toBeVisible();

  const aItemId = await selectSopType(page, "A");
  const aItem = await page.evaluate(async (itemId) => (await fetch(`/review-api/api/items/${itemId}`)).json(), aItemId);
  const unresolvedAnchors = aItem.evidence_anchors.map((anchor: Record<string, unknown>, index: number) => index === 0 ? {
    ...anchor,
    resolution_status: "unresolved",
    resolution_method: null,
    bbox: null,
  } : anchor);
  await mutateWithCsrf(page, `/api/items/${aItemId}/draft`, "PUT", {
    base_revision: aItem.revision,
    patch: { evidence_anchors: unresolvedAnchors },
  });
  await reloadWorkspace(page);
  await page.getByTestId("sop-type-filter").selectOption("A");
  await page.getByTestId(`item-${aItemId}`).click();
  await expect(page.getByTestId("anchor-quote")).toBeVisible();
  await expect(page.getByTestId("pdf-text-layer").locator("span").first()).toBeVisible({ timeout: 30_000 });
  const exactQuote = (await page.getByTestId("anchor-quote").textContent())!.trim();
  const selectionCreated = await page.evaluate((quote) => {
    const root = document.querySelector('[data-testid="pdf-text-layer"]')!;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes: Text[] = [];
    let joined = "";
    while (walker.nextNode()) {
      const node = walker.currentNode as Text;
      nodes.push(node);
      joined += node.data;
    }
    const start = joined.indexOf(quote);
    if (start < 0) return false;
    const end = start + quote.length;
    let offset = 0;
    let startNode: Text | null = null;
    let endNode: Text | null = null;
    let startOffset = 0;
    let endOffset = 0;
    for (const node of nodes) {
      const next = offset + node.data.length;
      if (!startNode && start >= offset && start <= next) {
        startNode = node;
        startOffset = start - offset;
      }
      if (endNode === null && end >= offset && end <= next) {
        endNode = node;
        endOffset = end - offset;
        break;
      }
      offset = next;
    }
    if (!startNode || !endNode) return false;
    const range = document.createRange();
    range.setStart(startNode, startOffset);
    range.setEnd(endNode, endOffset);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    root.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    return true;
  }, exactQuote);
  expect(selectionCreated).toBe(true);
  await page.getByTestId("confirm-manual-selection").click();
  await expect(page.getByText(/resolved · manual/)).toBeVisible();

  await page.getByTestId("review-question").fill(`${await page.getByTestId("review-question").inputValue()} · 승인 확인`);
  await page.keyboard.press("Control+s");
  await expect(page.getByText("저장됨")).toBeVisible();
  await page.keyboard.press("Control+Enter");
  await expect(page.getByTestId("fork-approved")).toBeVisible();
  await page.getByTestId("fork-approved").click();
  await expect(page.getByTestId("reject-item")).toBeDisabled();
  await page.getByTestId("reject-reason").fill("E2E fork 검증용 반려");
  await page.getByTestId("reject-item").click();
  await expect(page.getByTestId("reject-item")).toHaveCount(0);

  const cItemId = await selectSopType(page, "C");
  const cItem = await page.evaluate(async (itemId) => (await fetch(`/review-api/api/items/${itemId}`)).json(), cItemId);
  await mutateWithCsrf(page, `/api/items/${cItemId}/draft`, "PUT", {
    base_revision: cItem.revision,
    patch: { question: `${cItem.question} · 외부 revision` },
  });
  await page.getByTestId("review-question").fill(`${await page.getByTestId("review-question").inputValue()} · stale save`);
  await page.keyboard.press("Control+s");
  await expect(page.getByTestId("review-error")).toContainText("stale revision");
  await reloadWorkspace(page);


  await page.evaluate(async () => {
    const currentCsrf = () => {
      const entry = document.cookie.split("; ").find((value) => value.startsWith("bidmate_review_csrf="));
      if (!entry) throw new Error("CSRF cookie is unavailable");
      return decodeURIComponent(entry.slice(entry.indexOf("=") + 1));
    };
    const pageResult = await (await fetch("/review-api/api/datasets/" + location.pathname.split("/").pop() + "/items?page_size=100")).json();
    for (const summary of pageResult.items) {
      if (["approved", "rejected"].includes(summary.status)) continue;
      const item = await (await fetch(`/review-api/api/items/${summary.item_id}`)).json();
      const response = await fetch(`/review-api/api/items/${summary.item_id}/approve`, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json", "X-CSRF-Token": currentCsrf() },
        body: JSON.stringify({ base_revision: item.revision }),
      });
      if (!response.ok) throw new Error(`bulk approval failed: ${response.status} ${await response.text()}`);
    }
  });
  await reloadWorkspace(page);
  await page.getByTestId("export-legacy").click();
  await expect(page.getByTestId("export-status")).toBeVisible();
  expect(externalRequests).toEqual([]);
});