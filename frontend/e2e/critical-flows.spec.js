import { expect, test } from "@playwright/test";

const json = (route, body) => route.fulfill({
  status: 200,
  contentType: "application/json",
  body: JSON.stringify(body),
});

test.beforeEach(async ({ page }) => {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "127.0.0.1" || url.hostname === "localhost") {
      if (!url.pathname.startsWith("/api/")) return route.continue();
      if (url.pathname === "/api/settings") {
        return json(route, { symbol: "SOLUSDT", testnet: true, test_key_set: false, main_key_set: false });
      }
      if (url.pathname === "/api/backtest/sar-adx/capabilities") {
        return json(route, { symbols: ["SOLUSDT"], coverage: [] });
      }
      if (url.pathname === "/api/backtest/sar-adx") return json(route, {});
      if (url.pathname === "/api/ai/market-chat") return json(route, { answer: "合成行情分析结果" });
      if (url.pathname === "/api/market/symbols") return json(route, ["SOLUSDT"]);
      if (url.pathname.includes("/api/market/klines/")) return json(route, []);
      if (url.pathname === "/api/ai/providers" || url.pathname === "/api/ai/list") return json(route, []);
      return json(route, {});
    }
    return route.abort("blockedbyclient");
  });
});

test("runs a mocked SAR+ADX backtest", async ({ page }) => {
  await page.goto("/backtest");
  await expect(page.getByRole("heading", { name: "SAR + ADX 回测" })).toBeVisible();
  await page.getByRole("button", { name: "运行回测" }).click();
  await expect(page.getByRole("status")).toContainText("回测完成");
});

test("opens market AI and sends a quick question without external traffic", async ({ page }) => {
  await page.goto("/markets");
  await page.getByRole("button", { name: "打开 AI 行情分析" }).first().click();
  await page.getByRole("button", { name: "现在的市场周期是什么？" }).click();
  await expect(page.getByText("合成行情分析结果")).toBeVisible();
});
