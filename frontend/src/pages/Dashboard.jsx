import React from "react";
import { useApp } from "../context/AppContext";
import { useTicker } from "../context/MarketTickerContext";
import { useNavigate } from "react-router-dom";
import { Wallet, TrendingUp, Activity, AlertCircle, BarChart3, RefreshCw } from "lucide-react";
import clsx from "clsx";
import ExchangeUnavailableState from "../components/ExchangeUnavailableState";

function StatCard({ icon: Icon, label, value, sub, color = "text-white" }) {
  return (
    <div className="bg-card border border-border rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs text-muted">{label}</span>
        <Icon size={16} className="text-muted" />
      </div>
      <div className={clsx("text-xl font-bold font-mono", color)}>{value}</div>
      {sub && <div className="text-xs text-muted mt-1">{sub}</div>}
    </div>
  );
}

const ENGINE_STATES = {
  stopped: { label: "已停止", tone: "bg-surface text-muted" },
  running: { label: "运行中", tone: "bg-green/10 text-green" },
  retrying: { label: "重试中", tone: "bg-accent/10 text-accent" },
  network_halted: { label: "网络故障", tone: "bg-red/10 text-red" },
  halted: { label: "已安全停止", tone: "bg-red/10 text-red" },
  recovery_required: { label: "需要恢复", tone: "bg-red/10 text-red" },
};

const ERROR_MESSAGES = {
  retrying: "行情连接中断，正在自动重试。",
  network_halted: "网络故障，策略已停止。",
  halted: "策略已安全停止，请检查配置或运行日志。",
  recovery_required: "策略状态需要人工恢复。",
};

function isStrategyAction(value) {
  if (!value) return false;
  return !/(halted|recovery|required|network|connection|error|retry|连接|网络|重试|恢复)/i.test(value);
}

function DashboardContent() {
  const {
    account,
    accountError,
    accountPhase: accountPhaseValue,
    positions,
    positionsError,
    positionsPhase,
    botStatus,
    botStatusLoaded,
    connected,
    refreshAccount,
  } = useApp();
  const ticker = useTicker();
  const navigate = useNavigate();

  const totalBalance = parseFloat(account?.totalWalletBalance || 0);
  const unrealized   = parseFloat(account?.totalUnrealizedProfit || 0);
  const margin       = parseFloat(account?.totalMarginBalance || 0);
  const available    = parseFloat(account?.availableBalance || 0);
  const engineState = botStatus?.engine_state || (botStatus?.running ? "running" : "stopped");
  const stateView = ENGINE_STATES[engineState] || ENGINE_STATES.halted;
  const direction = botStatus && Object.hasOwn(botStatus, "position_direction")
    ? botStatus.position_direction
    : botStatus?.last_signal;
  const directionLabel = !botStatusLoaded
    ? "--"
    : direction == null ? "--"
      : direction === "LONG" ? "多头" : direction === "SHORT" ? "空头" : "空仓";
  const fillCount = botStatus?.filled_order_count ?? botStatus?.trade_count;
  const fillCountLabel = botStatusLoaded && fillCount != null && Number.isFinite(Number(fillCount))
    ? `${fillCount} 笔`
    : "--";
  const lastAction = isStrategyAction(botStatus?.last_action) ? botStatus.last_action : "—";
  const runtimeMessage = botStatusLoaded ? ERROR_MESSAGES[engineState] : null;
  const accountValue = (value, prefix = "$") => account
    ? `${prefix}${value.toFixed(2)}`
    : "--";
  const accountPhase = accountPhaseValue || (account ? "complete" : accountError ? "error" : "loading");

  return (
    <div className="space-y-4">
      {!connected && (
        <div className="flex flex-wrap items-center justify-between gap-3 border border-red/20 bg-red/5 px-4 py-3 text-sm text-red">
          <span className="flex items-center gap-2">
            <AlertCircle size={16} /> 行情连接已断开，策略状态仍可查看。
          </span>
          <button onClick={() => navigate("/settings")}
            className="text-xs font-medium text-accent hover:text-accent/80">
            检查连接配置
          </button>
        </div>
      )}
      {accountError && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 border border-red/20 bg-red/5 px-4 py-3 text-sm text-red">
          <span className="flex items-center gap-2">
            <AlertCircle size={16} />
            {accountPhase === "stale" && <strong className="font-semibold">显示上次成功数据。</strong>}
            {accountError}
          </span>
          <span className="flex items-center gap-3">
            {refreshAccount && (
              <button type="button" onClick={refreshAccount} disabled={accountPhase === "refreshing"}
                className="inline-flex items-center gap-1 text-xs font-medium text-accent hover:text-accent/80 disabled:opacity-50">
                <RefreshCw size={13} className={accountPhase === "refreshing" ? "animate-spin" : ""} />重试
              </button>
            )}
            <button onClick={() => navigate("/settings")}
              className="text-xs font-medium text-accent hover:text-accent/80">
              检查 API 配置
            </button>
          </span>
        </div>
      )}
      {accountPhase === "empty" && !accountError && (
        <div className="flex items-center justify-between gap-3 border border-border bg-surface/40 px-4 py-3 text-sm text-muted">
          <span>账户暂时没有可显示的余额数据。</span>
          {refreshAccount && <button type="button" onClick={refreshAccount} className="text-xs font-medium text-accent">重试</button>}
        </div>
      )}

      {/* 账户总览 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard icon={Wallet} label="账户净值" value={accountPhase === "loading" ? "加载中" : accountValue(totalBalance)} />
        <StatCard icon={BarChart3} label="可用余额" value={accountValue(available)} sub="USDT" />
        <StatCard icon={TrendingUp} label="未实现盈亏"
          value={account ? `${unrealized >= 0 ? "+" : ""}$${unrealized.toFixed(2)}` : "--"}
          color={unrealized >= 0 ? "text-green" : "text-red"} />
        <StatCard icon={Activity} label="保证金余额" value={accountValue(margin)} />
      </div>

      {/* 机器人状态 */}
      <div className="bg-card border border-border rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <span className="font-semibold text-sm">机器人状态</span>
          <span className={clsx("text-xs px-2 py-0.5 rounded-full",
            botStatusLoaded ? stateView.tone : "bg-surface text-muted")}>
            {botStatusLoaded ? stateView.label : "加载中"}
          </span>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <div className="text-muted text-xs mb-1">当前持仓方向</div>
            <div className={clsx("font-medium",
              direction === "LONG"  ? "text-green" :
              direction === "SHORT" ? "text-red" : "text-muted")}>
              {directionLabel}
            </div>
          </div>
          <div>
            <div className="text-muted text-xs mb-1">交易所成交</div>
            <div className="text-accent font-bold">{fillCountLabel}</div>
          </div>
          <div className="col-span-2">
            <div className="text-muted text-xs mb-1">上次操作</div>
            <div className="text-xs font-mono text-white truncate">
              {lastAction}
            </div>
          </div>
        </div>
        {runtimeMessage && (
          <div className="mt-3 text-xs text-red bg-red/5 border border-red/20 rounded px-3 py-2">
            {runtimeMessage}
          </div>
        )}
      </div>

      {/* 当前持仓 */}
      <div className="bg-card border border-border rounded-xl p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-semibold text-sm">当前持仓</h3>
          {positionsError && (
            <span role="alert" className={clsx("text-xs", positionsPhase === "stale" ? "text-accent" : "text-red")}>
              {positionsPhase === "stale" ? "显示上次成功持仓：" : ""}{positionsError}
            </span>
          )}
        </div>
        {positions.length === 0 ? (
          <p className="text-muted text-sm text-center py-4">暂无持仓</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border">
                  {["合约", "方向", "数量", "开仓价", "标记价", "未实现盈亏", "收益率"].map(h => (
                    <th key={h} className="text-left pb-2 pr-4">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => {
                  const pnl = parseFloat(p.unrealizedProfit);
                  const side = parseFloat(p.positionAmt) > 0 ? "多" : "空";
                  const pct = parseFloat(p.percentage || 0);
                  return (
                    <tr key={i} className="border-b border-border/50 hover:bg-surface/50">
                      <td className="py-2 pr-4 font-medium">{p.symbol}</td>
                      <td className={clsx("pr-4 font-bold", side === "多" ? "text-green" : "text-red")}>{side}</td>
                      <td className="pr-4 font-mono">{Math.abs(parseFloat(p.positionAmt))}</td>
                      <td className="pr-4 font-mono">{parseFloat(p.entryPrice).toFixed(2)}</td>
                      <td className="pr-4 font-mono">{parseFloat(p.markPrice).toFixed(2)}</td>
                      <td className={clsx("pr-4 font-mono font-bold", pnl >= 0 ? "text-green" : "text-red")}>
                        {pnl >= 0 ? "+" : ""}{pnl.toFixed(2)} USDT
                      </td>
                      <td className={clsx("font-mono", pct >= 0 ? "text-green" : "text-red")}>
                        {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 实时行情摘要 */}
      {ticker && (
        <div className="bg-card border border-border rounded-xl p-4">
          <h3 className="font-semibold text-sm mb-3">行情摘要 · {ticker.symbol}</h3>
          <div className="grid grid-cols-3 md:grid-cols-6 gap-3 text-xs">
            {[
              ["最新价", `$${parseFloat(ticker.price).toLocaleString("en", { minimumFractionDigits: 2 })}`],
              ["24H涨跌", `${parseFloat(ticker.change) >= 0 ? "+" : ""}${ticker.change}%`],
              ["24H高", `$${parseFloat(ticker.high).toLocaleString()}`],
              ["24H低", `$${parseFloat(ticker.low).toLocaleString()}`],
              ["24H成交额", `$${(parseFloat(ticker.volume) / 1e6).toFixed(1)}M`],
            ].map(([label, val], i) => (
              <div key={i}>
                <div className="text-muted mb-1">{label}</div>
                <div className={clsx("font-mono font-medium",
                  label === "24H涨跌" && parseFloat(ticker.change) >= 0 ? "text-green" :
                  label === "24H涨跌" ? "text-red" : "text-white")}>
                  {val}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function Dashboard() {
  const { exchangeProvider, exchangeSupported, exchangeSwitching, settingsLoaded } = useApp();
  const isExchangeSupported = settingsLoaded !== false
    && exchangeSupported;

  if (!isExchangeSupported || exchangeSwitching) {
    return <ExchangeUnavailableState exchangeProvider={exchangeProvider} />;
  }

  return <DashboardContent />;
}
