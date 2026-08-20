import { expect, test } from "@playwright/test";

const json = (route, body) => route.fulfill({
  status: 200,
  contentType: "application/json",
  body: JSON.stringify(body),
});

let agentRunning;
let networkTestnet;
let strategyRunning;
let strategyStartCount;

const marketRows = Array.from({ length: 120 }, (_, index) => {
  const open = 140 + index * 0.08;
  const close = open + (index % 3 === 0 ? -0.18 : 0.22);
  return {
    open_time: new Date(Date.UTC(2026, 7, 18, 0, index * 5)).toISOString(),
    open,
    high: Math.max(open, close) + 0.35,
    low: Math.min(open, close) - 0.35,
    close,
    volume: 1000 + index * 4,
    psar: index % 20 < 12 ? open - 0.8 : open + 0.8,
    psar_direction: index % 20 < 12 ? 1 : -1,
    adx14: 18 + (index % 24),
    pdi: 20 + (index % 12),
    ndi: 28 - (index % 12),
  };
});

test.beforeEach(async ({ page }) => {
  agentRunning = false;
  networkTestnet = true;
  strategyRunning = false;
  strategyStartCount = 0;
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      return route.abort("blockedbyclient");
    }
    if (!url.pathname.startsWith("/api/")) return route.continue();
    if (url.pathname === "/api/settings") {
      if (route.request().method() === "POST") {
        networkTestnet = route.request().postDataJSON().testnet;
      }
      return json(route, {
        symbol: "SOLUSDT",
        testnet: networkTestnet,
        test_key_set: false,
        main_key_set: false,
      });
    }
    if (url.pathname === "/api/backtest/sar-adx/capabilities") {
      return json(route, { symbols: ["SOLUSDT"], coverage: [] });
    }
    if (url.pathname === "/api/backtest/sar-adx") return json(route, {});
    if (url.pathname === "/api/ai/market-chat") {
      return json(route, { answer: "合成行情分析结果" });
    }
    if (url.pathname === "/api/ai/market-agent/status") {
      return json(route, agentRunning
        ? {
          state: "running",
          desired_enabled: true,
          agent_id: "agent-1",
          symbol: "SOLUSDT",
          trigger_interval: "5m",
          latest_sequence: 0,
        }
        : { state: "stopped", desired_enabled: false, latest_sequence: 0 });
    }
    if (url.pathname === "/api/ai/market-agent/events") {
      return json(route, { events: [], latest_sequence: 0 });
    }
    if (url.pathname === "/api/ai/market-agent/start") {
      expect(route.request().postDataJSON()).toEqual({ symbol: "SOLUSDT" });
      agentRunning = true;
      return json(route, {
        state: "running",
        desired_enabled: true,
        agent_id: "agent-1",
        symbol: "SOLUSDT",
        trigger_interval: "5m",
      });
    }
    if (url.pathname === "/api/market/symbols") return json(route, ["SOLUSDT"]);
    if (url.pathname.includes("/api/market/klines/")) return json(route, marketRows);
    if (url.pathname === "/api/account/balance") {
      return json(route, { totalWalletBalance: "1000", totalMarginBalance: "1000" });
    }
    if (url.pathname === "/api/strategy/engine/status") {
      return json(route, {
        running: strategyRunning,
        engine_state: strategyRunning ? "running" : "stopped",
        symbol: "SOLUSDT",
        network: networkTestnet ? "testnet" : "mainnet",
      });
    }
    if (url.pathname === "/api/strategy/engine/start") {
      strategyStartCount += 1;
      strategyRunning = true;
      return json(route, { ok: true, message: "策略已启动" });
    }
    if (url.pathname === "/api/strategy/engine/stop") {
      strategyRunning = false;
      return json(route, { ok: true, message: "策略已停止" });
    }
    if (url.pathname === "/api/strategy/analytics") {
      return json(route, {
        schema_version: 1,
        scope: { network: networkTestnet ? "testnet" : "mainnet", symbol: "SOLUSDT" },
        as_of: "2026-08-20T08:00:00Z",
        coverage: {
          status: "complete",
          from: "2026-08-01T00:00:00Z",
          through: "2026-08-20T08:00:00Z",
          sync_state: "synced",
          reasons: [],
        },
        counts: { status: "complete", completed_total: 12, long: 7, short: 5 },
        week: { status: "complete", net_pnl_usdt: "25.5", net_return_pct: "1.25" },
        month: {
          status: "complete",
          net_pnl_usdt: "40",
          net_return_pct: "4",
        },
        overall: {
          status: "complete",
          completed_count: 12,
          long: 7,
          short: 5,
          win_rate_pct: "60",
          payoff_ratio: "2.1",
        },
        costs: { complete: true, commission_usdt: "2", funding_net_usdt: "-0.5" },
        equity_curve: [
          { time: "2026-08-01T00:00:00Z", equity_usdt: "1000" },
          { time: "2026-08-20T08:00:00Z", equity_usdt: "1040" },
        ],
      });
    }
    if (url.pathname === "/api/ai/providers" || url.pathname === "/api/ai/list") {
      return json(route, []);
    }
    return json(route, {});
  });
});

test("runs a mocked CandleMind trend strategy backtest", async ({ page }) => {
  await page.goto("/backtest");
  await expect(page.getByRole("heading", { name: "CandleMind 趋势策略回测" })).toBeVisible();
  await expect(page.getByText(/SAR|ADX|V3/i)).toHaveCount(0);
  await page.getByRole("button", { name: "运行回测" }).click();
  await expect(page.getByRole("status")).toContainText("回测完成");
});

test("switches to mainnet from a mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/markets");

  await page.getByRole("button", { name: "真实网" }).click();

  await expect(page.getByRole("button", { name: "真实网" })).toHaveAttribute("aria-pressed", "true");
});

test("opens the inline assistant, narrows the chart, and resizes it", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/markets");
  const chart = page.locator(".price-chart-root");
  const before = await chart.boundingBox();

  await page.getByRole("button", { name: "打开 AI 行情分析" }).click();

  const assistant = page.getByRole("region", { name: /VibeTrading 实时助手/ });
  const divider = page.getByRole("separator", { name: "调整行情图与 AI 助手的大小" });
  await expect(assistant).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(divider).toHaveAttribute("aria-orientation", "vertical");
  const narrowed = await chart.boundingBox();
  expect(narrowed.width).toBeLessThan(before.width - 250);

  await divider.focus();
  await divider.press("ArrowLeft");
  await expect.poll(async () => (await chart.boundingBox()).width).toBeLessThan(narrowed.width);

  await page.getByRole("button", { name: "启动", exact: true }).click();
  await expect(page.getByText("运行中")).toBeVisible();
});

test("uses an in-page vertical split below 900px", async ({ page }) => {
  await page.setViewportSize({ width: 899, height: 900 });
  await page.goto("/markets");
  await page.getByRole("button", { name: "打开 AI 行情分析" }).click();

  const chart = page.locator(".price-chart-root");
  const assistant = page.getByRole("region", { name: /VibeTrading 实时助手/ });
  const divider = page.getByRole("separator", { name: "调整行情图与 AI 助手的大小" });
  await expect(divider).toHaveAttribute("aria-orientation", "horizontal");
  const [chartBox, assistantBox] = await Promise.all([
    chart.boundingBox(),
    assistant.boundingBox(),
  ]);
  expect(assistantBox.y).toBeGreaterThanOrEqual(chartBox.y + chartBox.height);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 899, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`renders a nonblank market workspace at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.goto("/markets");
    await page.getByRole("button", { name: "打开 AI 行情分析" }).click();

    const chart = page.locator(".price-chart-root");
    const assistant = page.getByRole("region", { name: /VibeTrading 实时助手/ });
    await expect(chart).toBeVisible();
    await expect(assistant).toBeVisible();
    const [chartBox, assistantBox] = await Promise.all([chart.boundingBox(), assistant.boundingBox()]);
    if (viewport.width >= 900) {
      expect(chartBox.x + chartBox.width).toBeLessThanOrEqual(assistantBox.x);
    } else {
      expect(chartBox.y + chartBox.height).toBeLessThanOrEqual(assistantBox.y);
    }

    const canvases = chart.locator("canvas");
    await expect(canvases.first()).toBeVisible();
    const hasVisiblePixels = await canvases.evaluateAll((items) => items.some((canvas) => {
      const context = canvas.getContext("2d");
      if (!context || canvas.width === 0 || canvas.height === 0) return false;
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      const first = [pixels[0], pixels[1], pixels[2], pixels[3]].join(",");
      for (let index = 4; index < pixels.length; index += 64) {
        if ([pixels[index], pixels[index + 1], pixels[index + 2], pixels[index + 3]].join(",") !== first) return true;
      }
      return false;
    }));
    expect(hasVisiblePixels).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`markets-${viewport.width}x${viewport.height}.png`), fullPage: true });
  });
}

test("keeps the strategy command global and enforces mainnet confirmation", async ({ page }) => {
  await page.goto("/markets");
  await expect(page.getByRole("button", { name: "启动策略" })).toBeVisible();
  await page.getByRole("button", { name: "真实网" }).click();
  await page.getByRole("button", { name: "启动策略" }).click();
  await expect(page.getByRole("dialog", { name: "确认启动策略" })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认真实网启动" })).toBeDisabled();
  expect(strategyStartCount).toBe(0);

  await page.getByLabel("真实网确认文本").fill("MAINNET:SOLUSDT");
  await page.getByRole("button", { name: "确认真实网启动" }).click();
  await expect.poll(() => strategyStartCount).toBe(1);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 899, height: 900 },
  { width: 768, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`renders Orders analytics without page overflow at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.goto("/orders");

    await expect(page.getByLabel("策略分析").locator("article")).toHaveCount(8);
    await expect(page.getByText("资金曲线")).toHaveCount(0);
    await expect(page.getByText("+1.25%")).toBeVisible();
    await expect(page.getByRole("button", { name: "启动策略" })).toBeVisible();
    await expect(page.getByRole("button", { name: "刷新当前数据" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath(`orders-${viewport.width}x${viewport.height}.png`), fullPage: true });
  });
}
