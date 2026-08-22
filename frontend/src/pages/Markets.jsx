import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../context/AppContext";
import MarketSummary from "../components/MarketSummary";
import MarketAiPanel from "../components/MarketAiPanel";
import PriceChart from "../components/PriceChart";
import WorkspaceDivider from "../components/WorkspaceDivider";
import ExchangeUnavailableState from "../components/ExchangeUnavailableState";

const PANEL_WIDTH_KEY = "candlemind.marketAssistant.width";
const PANEL_OPEN_KEY = "candlemind.marketAssistant.open";
const DEFAULT_PANEL_WIDTH = 420;
const DEFAULT_PANEL_HEIGHT = 320;
const DESKTOP_BREAKPOINT = 900;
const MIN_PANEL_WIDTH = 320;
const MIN_CHART_WIDTH = 480;
const MIN_MOBILE_PANEL_HEIGHT = 240;
const MIN_MOBILE_CHART_HEIGHT = 320;
const DIVIDER_SIZE = 8;

function storedPanelWidth() {
  const value = Number(window.localStorage.getItem(PANEL_WIDTH_KEY));
  return Number.isFinite(value) && value >= MIN_PANEL_WIDTH ? value : DEFAULT_PANEL_WIDTH;
}

function storedPanelOpen() {
  try {
    const value = window.localStorage.getItem(PANEL_OPEN_KEY);
    if (value === "true") return true;
    if (value === "false") return false;
  } catch {
    // Storage can be unavailable in privacy-restricted browser contexts.
  }
  return false;
}

function persistPanelOpen(open) {
  try {
    window.localStorage.setItem(PANEL_OPEN_KEY, String(open));
  } catch {
    // The in-memory UI state remains usable when storage is unavailable.
  }
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function splitBounds(totalSize, preferredPanelMin, preferredChartMin, maxPanelFraction = 1) {
  const available = Math.max(0, totalSize - DIVIDER_SIZE);
  if (available === 0) return { min: 0, max: 0 };

  const min = Math.min(preferredPanelMin, available * 0.32);
  const chartMin = Math.min(preferredChartMin, available * 0.45);
  const max = Math.max(min, Math.min(available - chartMin, available * maxPanelFraction));
  return { min, max };
}

function MarketsWorkspace() {
  const { refreshRevision, symbol } = useApp();
  const workspaceRef = useRef(null);
  const [interval, setInterval] = useState("5m");
  const [assistantOpen, setAssistantOpen] = useState(storedPanelOpen);
  const [indicatorSnapshot, setIndicatorSnapshot] = useState(null);
  const [panelWidth, setPanelWidth] = useState(storedPanelWidth);
  const [panelHeight, setPanelHeight] = useState(DEFAULT_PANEL_HEIGHT);
  const [desktop, setDesktop] = useState(() => window.innerWidth >= DESKTOP_BREAKPOINT);
  const [workspaceSize, setWorkspaceSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const onResize = () => setDesktop(window.innerWidth >= DESKTOP_BREAKPOINT);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    const node = workspaceRef.current;
    if (!node) return undefined;
    const updateSize = () => setWorkspaceSize((current) => {
      const next = { width: node.clientWidth, height: node.clientHeight };
      return next.width === current.width && next.height === current.height ? current : next;
    });
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const desktopBounds = workspaceSize.width > 0
    ? splitBounds(workspaceSize.width, MIN_PANEL_WIDTH, MIN_CHART_WIDTH, 0.55)
    : { min: MIN_PANEL_WIDTH, max: DEFAULT_PANEL_WIDTH };
  const mobileBounds = workspaceSize.height > 0
    ? splitBounds(workspaceSize.height, MIN_MOBILE_PANEL_HEIGHT, MIN_MOBILE_CHART_HEIGHT)
    : { min: MIN_MOBILE_PANEL_HEIGHT, max: DEFAULT_PANEL_HEIGHT };
  const activeBounds = desktop ? desktopBounds : mobileBounds;
  const activeValue = desktop
    ? clamp(panelWidth, activeBounds.min, activeBounds.max)
    : clamp(panelHeight, activeBounds.min, activeBounds.max);

  useEffect(() => {
    if (desktop) setPanelWidth((value) => clamp(value, desktopBounds.min, desktopBounds.max));
    else setPanelHeight((value) => clamp(value, mobileBounds.min, mobileBounds.max));
  }, [desktop, desktopBounds.min, desktopBounds.max, mobileBounds.min, mobileBounds.max]);

  const resizePanel = useCallback((value) => {
    if (desktop) setPanelWidth(value);
    else setPanelHeight(value);
  }, [desktop]);

  const commitPanelSize = useCallback((value) => {
    if (desktop) window.localStorage.setItem(PANEL_WIDTH_KEY, String(Math.round(value)));
  }, [desktop]);

  const toggleAssistant = useCallback(() => {
    setAssistantOpen((open) => {
      const next = !open;
      persistPanelOpen(next);
      return next;
    });
  }, []);

  const closeAssistant = useCallback(() => {
    persistPanelOpen(false);
    setAssistantOpen(false);
  }, []);

  return (
    <div
      ref={workspaceRef}
      data-testid="markets-workspace"
      className={`flex h-full min-h-0 min-w-0 overflow-hidden ${desktop ? "flex-row" : "flex-col"}`}
    >
      <div className="min-h-0 min-w-0 flex-1">
        <PriceChart
          symbol={symbol}
          interval={interval}
          onIntervalChange={setInterval}
          onOpenAssistant={toggleAssistant}
          assistantOpen={assistantOpen}
          refreshRevision={refreshRevision}
          onIndicatorSnapshot={setIndicatorSnapshot}
          headerLeading={(
            <MarketSummary
              symbol={symbol}
              refreshRevision={refreshRevision}
              indicators={indicatorSnapshot}
            />
          )}
        />
      </div>
      {assistantOpen && (
        <>
          <WorkspaceDivider
            orientation={desktop ? "vertical" : "horizontal"}
            value={activeValue}
            min={activeBounds.min}
            max={activeBounds.max}
            onChange={resizePanel}
            onCommit={commitPanelSize}
          />
          <div
            className="min-h-0 min-w-0 shrink-0"
            style={desktop ? { width: activeValue } : { height: activeValue }}
          >
            <MarketAiPanel
              symbol={symbol}
              onClose={closeAssistant}
            />
          </div>
        </>
      )}
    </div>
  );
}

export default function Markets() {
  const { exchangeProvider, exchangeSupported, exchangeSwitching, settingsLoaded } = useApp();
  const isExchangeSupported = settingsLoaded !== false
    && exchangeSupported;

  if (!isExchangeSupported || exchangeSwitching) {
    return <ExchangeUnavailableState exchangeProvider={exchangeProvider} />;
  }

  return <MarketsWorkspace />;
}
