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
let strategyConfiguration;
let openOrderMode;
let openOrderDelayMs;
let exchangeProvider;
let binanceBusinessRequestCount;

const strategyDefaults = {
  sar_adx_trend: {
    execution_interval: "5m", sar_step: 0.02, sar_max: 0.2, max_layers: 5,
    adx_timeframe: "1h", adx_period: 14, adx_threshold: 45,
    adx_rising_periods: 2, entry_confirmation_bars: 6,
    recapture_buffer_fraction: 0.0024, max_entries_per_adx_regime: 2,
  },
  sar_martingale: {
    execution_interval: "5m", sar_step: 0.02, sar_max: 0.2, max_layers: 4,
    layer_multiplier: 1.5, add_trigger_fraction: 0.005,
  },
  sar_anti_martingale: {
    execution_interval: "5m", sar_step: 0.02, sar_max: 0.2, max_layers: 4,
    layer_multiplier: 1.5, add_trigger_fraction: 0.005,
  },
};

const strategyVersions = {
  sar_adx_trend: "sar_adx_trend_v1",
  sar_martingale: "sar_martingale_v1",
  sar_anti_martingale: "sar_anti_martingale_v1",
};

const configuredStrategy = (strategyType, parameters = strategyDefaults[strategyType]) => ({
  strategy_type: strategyType,
  config_version: strategyVersions[strategyType],
  config_hash: strategyType === "sar_adx_trend" ? "a".repeat(64) : "b".repeat(64),
  parameters,
});

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
    atr14: 1.2 + (index % 8) * 0.05,
    rsi14: 42 + (index % 20),
    pdi: 20 + (index % 12),
    ndi: 28 - (index % 12),
    ema20: open - 0.3,
    ema100: open - 0.9,
    supertrend: index % 20 < 12 ? open - 1.2 : open + 1.2,
    supertrend_direction: index % 20 < 12 ? 1 : -1,
    bb_upper: open + 1.4,
    bb_middle: open,
    bb_lower: open - 1.4,
  };
});

test.beforeEach(async ({ page }) => {
  agentRunning = false;
  networkTestnet = true;
  strategyRunning = false;
  strategyStartCount = 0;
  strategyConfiguration = configuredStrategy("sar_adx_trend");
  openOrderMode = "complete";
  openOrderDelayMs = 0;
  exchangeProvider = "binance";
  binanceBusinessRequestCount = 0;
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      return route.abort("blockedbyclient");
    }
    if (!url.pathname.startsWith("/api/")) return route.continue();
    if (url.pathname === "/api/settings") {
      if (route.request().method() === "POST") {
        const settings = route.request().postDataJSON();
        if (typeof settings.testnet === "boolean") networkTestnet = settings.testnet;
        if (settings.exchange_provider) exchangeProvider = settings.exchange_provider;
      }
      return json(route, {
        symbol: "SOLUSDT",
        testnet: networkTestnet,
        exchange_provider: exchangeProvider,
        exchange_supported: exchangeProvider === "binance",
        exchange_connected: exchangeProvider === "binance",
        test_key_set: false,
        main_key_set: false,
      });
    }
    if (url.pathname.startsWith("/api/backtest/")) {
      return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"Not Found"}' });
    }
    if (url.pathname === "/api/strategy/catalog") {
      return json(route, { strategies: [
        { strategy_type: "sar_adx_trend", config_version: strategyVersions.sar_adx_trend, name: "CandleMind Trend Strategy", default_parameters: strategyDefaults.sar_adx_trend },
        { strategy_type: "sar_martingale", config_version: strategyVersions.sar_martingale, name: "SAR Martingale", default_parameters: strategyDefaults.sar_martingale },
        { strategy_type: "sar_anti_martingale", config_version: strategyVersions.sar_anti_martingale, name: "SAR Anti-Martingale", default_parameters: strategyDefaults.sar_anti_martingale },
      ] });
    }
    if (url.pathname === "/api/strategy/config") {
      if (route.request().method() === "PUT") {
        const request = route.request().postDataJSON();
        strategyConfiguration = configuredStrategy(request.strategy_type, request.parameters);
      }
      return json(route, strategyConfiguration);
    }
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
    if (url.pathname === "/api/market/symbols") {
      binanceBusinessRequestCount += 1;
      return json(route, ["SOLUSDT"]);
    }
    if (url.pathname.includes("/api/market/klines/")) {
      binanceBusinessRequestCount += 1;
      return json(route, marketRows);
    }
    if (url.pathname === "/api/account/balance") {
      binanceBusinessRequestCount += 1;
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
    if (url.pathname === "/api/orders/open/combined") {
      binanceBusinessRequestCount += 1;
      if (openOrderDelayMs) await new Promise((resolve) => setTimeout(resolve, openOrderDelayMs));
      if (openOrderMode === "transient") {
        return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Binance 暂时不可用" }) });
      }
      if (openOrderMode === "auth") {
        return route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ code: -2015, msg: "Invalid API-key, IP, or permissions" }) });
      }
      return json(route, {
        scope: { network: networkTestnet ? "testnet" : "mainnet", symbol: "SOLUSDT" },
        as_of: "2026-08-20T08:00:00Z",
        status: "complete",
        warnings: [],
        orders: [{ id: "regular-1", source: "regular", time: 1_700_000_000_000, symbol: "SOLUSDT", side: "BUY", type: "LIMIT", origQty: "1", price: "123.45", status: "NEW" }],
      });
    }
    if (url.pathname === "/api/orders/analytics") {
      binanceBusinessRequestCount += 1;
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

test("redirects the retired backtest path to three strategy configuration cards", async ({ page }) => {
  await page.goto("/backtest");
  await expect(page).toHaveURL(/\/strategies$/);
  await expect(page.getByRole("heading", { name: "自动化交易策略" })).toBeVisible();
  const cards = page.getByLabel("策略选择").getByRole("button");
  await expect(cards).toHaveCount(3);
  await page.getByRole("button", { name: /SAR反马丁/ }).click();
  await page.getByRole("button", { name: "保存策略配置" }).click();
  await expect(page.getByRole("status")).toContainText("策略配置已保存");
  expect(strategyConfiguration.strategy_type).toBe("sar_anti_martingale");
  await expect(page.getByText("运行回测")).toHaveCount(0);
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

  const assistant = page.getByRole("region", { name: /实时行情助手/ });
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
  const assistant = page.getByRole("region", { name: /实时行情助手/ });
  const divider = page.getByRole("separator", { name: "调整行情图与 AI 助手的大小" });
  await expect(divider).toHaveAttribute("aria-orientation", "horizontal");
  const [chartBox, assistantBox] = await Promise.all([
    chart.boundingBox(),
    assistant.boundingBox(),
  ]);
  expect(assistantBox.y).toBeGreaterThanOrEqual(chartBox.y + chartBox.height);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("persists the assistant only until the user closes it", async ({ page }) => {
  await page.goto("/markets");
  const openButton = page.getByRole("button", { name: "打开 AI 行情分析" });
  await expect(openButton).toContainText("AI行情分析");
  await openButton.click();
  await expect(page.getByRole("region", { name: /实时行情助手/ })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("region", { name: /实时行情助手/ })).toBeVisible();
  await page.goto("/orders");
  await page.goto("/markets");
  await expect(page.getByRole("region", { name: /实时行情助手/ })).toBeVisible();

  await page.getByRole("button", { name: "收起实时助手" }).click();
  await expect(page.getByRole("region", { name: /实时行情助手/ })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("region", { name: /实时行情助手/ })).toHaveCount(0);
});

test("offers five main-chart indicators and compact market metrics", async ({ page }) => {
  await page.goto("/markets");
  const indicator = page.getByRole("combobox", { name: "主图指标" });
  await expect(indicator).toHaveValue("psar");
  await expect(indicator.locator("option")).toHaveText([
    "SAR",
    "EMA20",
    "EMA100",
    "超级趋势",
    "布林带",
  ]);
  await indicator.selectOption("supertrend");
  await expect(indicator).toHaveValue("supertrend");
  await expect(page.getByText("ADX(14)", { exact: true })).toBeVisible();
  await expect(page.getByText("ATR(14)", { exact: true })).toBeVisible();
  await expect(page.getByText("RSI(14)", { exact: true })).toBeVisible();
  await expect(page.getByText("ADX / DI")).toHaveCount(0);
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
    const assistant = page.getByRole("region", { name: /实时行情助手/ });
    const refresh = page.getByRole("button", { name: "刷新当前数据" });
    await expect(chart).toBeVisible();
    await expect(assistant).toBeVisible();
    await expect(refresh).toContainText("刷新");
    const [chartBox, assistantBox] = await Promise.all([chart.boundingBox(), assistant.boundingBox()]);
    if (viewport.width >= 900) {
      expect(chartBox.x + chartBox.width).toBeLessThanOrEqual(assistantBox.x);
    } else {
      expect(chartBox.y + chartBox.height).toBeLessThanOrEqual(assistantBox.y);
    }

    const layout = await page.evaluate(() => {
      const main = document.querySelector("main");
      const header = document.querySelector("header");
      const workspace = document.querySelector('[data-testid="markets-workspace"]');
      const bounds = workspace?.getBoundingClientRect();
      return {
        documentOverflow: document.documentElement.scrollHeight - window.innerHeight,
        mainOverflow: main ? main.scrollHeight - main.clientHeight : null,
        headerOverflow: header ? header.scrollWidth - header.clientWidth : null,
        workspaceBottom: bounds?.bottom ?? null,
      };
    });
    expect(layout.documentOverflow).toBeLessThanOrEqual(1);
    expect(layout.mainOverflow).toBeLessThanOrEqual(1);
    expect(layout.headerOverflow).toBeLessThanOrEqual(1);
    expect(layout.workspaceBottom).toBeLessThanOrEqual(viewport.height + 1);

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

test("serves the project logo and favicon as optimized frontend assets", async ({ page }) => {
  const [logoResponse, faviconResponse] = await Promise.all([
    page.request.get("/candlemind-logo.png"),
    page.request.get("/favicon.png"),
  ]);
  expect(logoResponse.ok()).toBe(true);
  expect(faviconResponse.ok()).toBe(true);
  expect(logoResponse.headers()["content-type"]).toContain("image/png");
  expect(faviconResponse.headers()["content-type"]).toContain("image/png");

  await page.goto("/");
  const logo = page.getByRole("img", { name: "CandleMind" });
  await expect(logo).toBeVisible();
  expect(await logo.evaluate((image) => ({ width: image.naturalWidth, height: image.naturalHeight })))
    .toEqual({ width: 128, height: 128 });
});

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

test("keeps unavailable exchanges disconnected across Binance business pages", async ({ page }) => {
  exchangeProvider = "okx";

  await page.goto("/markets");
  await expect(page.getByRole("heading", { name: "OKX 未连接" })).toBeVisible();
  await expect(page.getByText("未来会接入，敬请期待")).toBeVisible();
  await expect(page.getByRole("button", { name: "启动策略" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "刷新当前数据" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "测试网" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "真实网" })).toHaveCount(0);

  await page.goto("/orders");
  await expect(page.getByRole("heading", { name: "OKX 未连接" })).toBeVisible();
  await expect(page.getByLabel("订单明细")).toHaveCount(0);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "OKX 未连接" })).toBeVisible();
  await expect(page.getByText("账户净值")).toHaveCount(0);
  expect(binanceBusinessRequestCount).toBe(0);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`keeps exchange settings controls accessible at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/settings");

    const save = page.getByRole("button", { name: "保存配置" });
    const detectIp = page.getByRole("button", { name: "检测出口 IP" });
    const tabs = page.getByRole("tablist", { name: "交易所" });
    await expect(save).toBeVisible();
    await expect(detectIp).toBeVisible();
    await expect(tabs).toBeVisible();
    await expect(page.getByRole("tab")).toHaveCount(5);

    const [saveBox, tabsBox] = await Promise.all([save.boundingBox(), tabs.boundingBox()]);
    expect(saveBox).not.toBeNull();
    expect(tabsBox).not.toBeNull();
    if (viewport.width >= 1024) {
      expect(tabsBox.x).toBeGreaterThan(saveBox.x + saveBox.width);
      expect(Math.abs(tabsBox.y - saveBox.y)).toBeLessThan(16);
    } else {
      expect(tabsBox.y).toBeGreaterThan(saveBox.y + saveBox.height);
    }

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
}

test("keeps scoped order data stale for transient failures and clears it for -2015", async ({ page }) => {
  await page.goto("/orders");
  await expect(page.getByText("123.45")).toBeVisible();

  openOrderDelayMs = 200;
  const globalRefresh = page.getByRole("button", { name: "刷新当前数据" });
  await globalRefresh.click();
  await expect(globalRefresh).toBeDisabled();
  await expect(globalRefresh).toBeEnabled();

  openOrderDelayMs = 0;
  openOrderMode = "transient";
  await page.getByRole("button", { name: "刷新订单数据" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "数据可能已过期" })).toBeVisible();
  await expect(page.getByText("123.45")).toBeVisible();

  openOrderMode = "auth";
  await page.getByRole("button", { name: "刷新订单数据" }).click();
  const authError = page.getByRole("alert").filter({ hasText: "-2015" });
  await expect(authError).toContainText("API Key 无效");
  await expect(authError).toContainText("合约交易权限");
  await expect(authError).toContainText("后端服务器出口 IP");
  await expect(page.getByText("123.45")).toHaveCount(0);
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

    await expect(page.getByLabel("账户交易统计").locator("article")).toHaveCount(8);
    await expect(page.getByText("资金曲线")).toHaveCount(0);
    await expect(page.getByText("+1.25%")).toBeVisible();
    await expect(page.getByRole("button", { name: "启动策略" })).toBeVisible();
    await expect(page.getByRole("button", { name: "刷新当前数据" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath(`orders-${viewport.width}x${viewport.height}.png`), fullPage: true });
  });
}
