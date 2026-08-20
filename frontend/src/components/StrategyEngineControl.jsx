import React, { useState } from "react";
import { AlertCircle, Loader, Play, Square } from "lucide-react";
import clsx from "clsx";
import { useApp } from "../context/AppContext";

export default function StrategyEngineControl() {
  const {
    botStatus,
    botStatusLoaded,
    networkTab,
    networkSwitching,
    strategyCommandPending,
    strategyCommandError,
    refreshPending,
    symbolSwitching,
    strategyStatusUncertain,
    startStrategy,
    stopStrategy,
    symbol,
    strategyCapitalLimit,
  } = useApp();
  const [showConfirmation, setShowConfirmation] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const running = Boolean(botStatus?.running);
  const disabled = (
    !botStatusLoaded
    || networkSwitching
    || strategyCommandPending
    || refreshPending
    || symbolSwitching
    || strategyStatusUncertain
    || !symbol
  );

  const requestCommand = () => {
    if (running) {
      stopStrategy();
      return;
    }
    setConfirmation("");
    setShowConfirmation(true);
  };

  const confirmStart = async () => {
    const accepted = await startStrategy(networkTab === "main" ? confirmation : undefined);
    if (accepted) setShowConfirmation(false);
  };

  const label = running ? "停止策略" : "启动策略";

  return (
    <div className="relative shrink-0">
      <button
        type="button"
        aria-label={label}
        title={label}
        onClick={requestCommand}
        disabled={disabled}
        className={clsx(
          "flex h-8 items-center justify-center gap-1.5 rounded-md border px-2 text-xs font-semibold transition-colors disabled:cursor-wait disabled:opacity-50 sm:px-3",
          running
            ? "border-red/40 bg-red/10 text-red hover:bg-red/20"
            : "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20",
        )}
      >
        {strategyCommandPending ? (
          <Loader size={13} className="animate-spin" />
        ) : running ? (
          <Square size={13} />
        ) : (
          <Play size={13} />
        )}
        <span className="hidden sm:inline">{running ? "停止" : "启动"}</span>
      </button>

      {strategyCommandError && (
        <div role="alert" className="absolute right-0 top-full z-50 mt-2 flex w-72 max-w-[calc(100vw-1rem)] items-start gap-2 rounded-md border border-red/40 bg-card px-3 py-2 text-xs text-red shadow-xl">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          <span>{strategyCommandError}</span>
        </div>
      )}

      {showConfirmation && (
        <div role="dialog" aria-modal="true" aria-labelledby="mainnet-confirm-title" className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="w-full max-w-md rounded-md border border-red/40 bg-card p-4 shadow-2xl">
            <h2 id="mainnet-confirm-title" className="text-base font-semibold text-white">
              确认启动策略
            </h2>
            <p className="mt-2 text-sm leading-5 text-muted">
              策略将在出现有效信号后下单，请核对本次运行参数。
            </p>
            <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 rounded-md border border-border bg-surface p-3 text-sm">
              <dt className="text-muted">品种</dt><dd className="text-right font-mono text-white">{symbol}</dd>
              <dt className="text-muted">网络</dt><dd className="text-right text-white">{networkTab === "main" ? "真实网" : "测试网"}</dd>
              <dt className="text-muted">资金上限</dt><dd className="text-right font-mono text-white">{strategyCapitalLimit} USDT</dd>
            </dl>
            {networkTab === "main" && (
              <>
                <p className="mt-3 text-sm leading-5 text-muted">
                  将使用真实资金下单。请输入{" "}
                  <strong className="font-mono text-red">MAINNET:{symbol}</strong> 继续。
                </p>
                <input
                  autoFocus
                  aria-label="真实网确认文本"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  className="mt-3 w-full rounded-md border border-border bg-surface px-3 py-2 font-mono text-sm text-white outline-none focus:border-red"
                />
              </>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" onClick={() => setShowConfirmation(false)} className="rounded-md border border-border px-3 py-1.5 text-xs text-muted hover:text-white">取消</button>
              <button
                type="button"
                onClick={confirmStart}
                disabled={(networkTab === "main" && confirmation !== `MAINNET:${symbol}`) || strategyCommandPending || networkSwitching || refreshPending || symbolSwitching || strategyStatusUncertain}
                className={clsx(
                  "rounded-md px-3 py-1.5 text-xs font-bold disabled:opacity-40",
                  networkTab === "main" ? "bg-red text-white" : "bg-accent text-black",
                )}
              >
                {networkTab === "main" ? "确认真实网启动" : "确认启动"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
