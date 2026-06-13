import React from "react";
import { useApp } from "../context/AppContext";
import { useNavigate } from "react-router-dom";
import { Wallet, TrendingUp, TrendingDown, Activity, AlertCircle, BarChart3 } from "lucide-react";
import clsx from "clsx";

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

export default function Dashboard() {
  const { account, positions, botStatus, ticker, connected } = useApp();
  const navigate = useNavigate();

  if (!connected) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4">
        <AlertCircle size={48} className="text-muted" />
        <p className="text-muted text-lg">未连接到 Binance</p>
        <button onClick={() => navigate("/settings")}
          className="bg-accent text-black px-6 py-2 rounded-lg font-medium hover:bg-accent/90">
          前往配置 API Key
        </button>
      </div>
    );
  }

  const totalBalance = parseFloat(account?.totalWalletBalance || 0);
  const unrealized   = parseFloat(account?.totalUnrealizedProfit || 0);
  const margin       = parseFloat(account?.totalMarginBalance || 0);
  const available    = parseFloat(account?.availableBalance || 0);

  return (
    <div className="space-y-4">
      {/* 账户总览 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard icon={Wallet} label="账户净值" value={`$${totalBalance.toFixed(2)}`} />
        <StatCard icon={BarChart3} label="可用余额" value={`$${available.toFixed(2)}`} sub="USDT" />
        <StatCard icon={TrendingUp} label="未实现盈亏"
          value={`${unrealized >= 0 ? "+" : ""}$${unrealized.toFixed(2)}`}
          color={unrealized >= 0 ? "text-green" : "text-red"} />
        <StatCard icon={Activity} label="保证金余额" value={`$${margin.toFixed(2)}`} />
      </div>

      {/* 机器人状态 */}
      <div className="bg-card border border-border rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <span className="font-semibold text-sm">机器人状态</span>
          <span className={clsx("text-xs px-2 py-0.5 rounded-full",
            botStatus.running ? "bg-green/10 text-green" : "bg-surface text-muted")}>
            {botStatus.running ? "● 运行中" : "○ 已停止"}
          </span>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <div className="text-muted text-xs mb-1">最新信号</div>
            <div className={clsx("font-medium",
              botStatus.last_signal === "LONG"  ? "text-green" :
              botStatus.last_signal === "SHORT" ? "text-red" : "text-muted")}>
              {botStatus.last_signal === "NONE" ? "暂无" : botStatus.last_signal}
            </div>
          </div>
          <div>
            <div className="text-muted text-xs mb-1">已成交</div>
            <div className="text-accent font-bold">{botStatus.trade_count} 笔</div>
          </div>
          <div className="col-span-2">
            <div className="text-muted text-xs mb-1">上次操作</div>
            <div className="text-xs font-mono text-white truncate">
              {botStatus.last_action || "—"}
            </div>
          </div>
        </div>
        {botStatus.error && (
          <div className="mt-3 text-xs text-red bg-red/5 border border-red/20 rounded px-3 py-2">
            错误: {botStatus.error}
          </div>
        )}
      </div>

      {/* 当前持仓 */}
      <div className="bg-card border border-border rounded-xl p-4">
        <h3 className="font-semibold text-sm mb-3">当前持仓</h3>
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
