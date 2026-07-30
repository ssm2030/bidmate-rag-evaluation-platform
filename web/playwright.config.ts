import { defineConfig } from "@playwright/test";

const port = Number(process.env.EVAL_REVIEW_E2E_PORT ?? "3011");
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "../artifacts/eval_dataset/test/review-e2e",
  use: {
    baseURL,
    browserName: "chromium",
    headless: true,
    trace: "on",
  },
  webServer: {
    command: `npm run start -- --port ${port}`,
    url: `${baseURL}/eval-review`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
