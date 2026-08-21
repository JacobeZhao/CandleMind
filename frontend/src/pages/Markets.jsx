import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../context/AppContext";
import MarketSummary from "../components/MarketSummary";
import MarketAiPanel from "../components/MarketAiPanel";
import PriceChart from "../components/PriceChart";
import WorkspaceDivider from "../components/WorkspaceDivider";

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

export default function Markets() {
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

  const desktopMax = workspaceSize.width > 0
    ? Math.max(
      MIN_PANEL_WIDTH,
      Math.min(workspaceSize.width * 0.55, workspaceSize.width - MIN_CHART_WIDTH - DIVIDER_SIZE),
    )
    : DEFAULT_PANEL_WIDTH;
  const mobileMax = workspaceSize.height > 0
    ? Math.max(
      MIN_MOBILE_PANEL_HEIGHT,
      workspaceSize.height - MIN_MOBILE_CHART_HEIGHT - DIVIDER_SIZE,
    )
    : DEFAULT_PANEL_HEIGHT;
  const activeValue = desktop
    ? clamp(panelWidth, MIN_PANEL_WIDTH, desktopMax)
    : clamp(panelHeight, MIN_MOBILE_PANEL_HEIGHT, mobileMax);

  useEffect(() => {
    if (desktop) setPanelWidth((value) => clamp(value, MIN_PANEL_WIDTH, desktopMax));
    else setPanelHeight((value) => clamp(value, MIN_MOBILE_PANEL_HEIGHT, mobileMax));
  }, [desktop, desktopMax, mobileMax]);

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
      className={`flex h-full min-h-[640px] min-w-0 overflow-hidden ${desktop ? "flex-row" : "flex-col"}`}
    >
      <div className={`min-h-0 min-w-0 flex-1 ${desktop ? "min-w-[480px]" : "min-h-[320px]"}`}>
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
            min={desktop ? MIN_PANEL_WIDTH : MIN_MOBILE_PANEL_HEIGHT}
            max={desktop ? desktopMax : mobileMax}
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
