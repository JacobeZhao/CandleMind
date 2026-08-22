import React from "react";
import { NavLink } from "react-router-dom";
import {
  ClipboardList,
  SlidersHorizontal,
  LayoutDashboard,
  LineChart,
  Settings,
} from "lucide-react";
import clsx from "clsx";

const nav = [
  { to: "/", icon: LayoutDashboard, label: "首页" },
  { to: "/markets", icon: LineChart, label: "行情" },
  { to: "/orders", icon: ClipboardList, label: "订单" },
  { to: "/strategies", icon: SlidersHorizontal, label: "策略" },
  { to: "/settings", icon: Settings, label: "设置" },
];

export default function Sidebar() {
  return (
    <aside className="w-16 md:w-52 bg-card border-r border-border flex flex-col shrink-0">
      <div className="flex items-center gap-2 px-4 h-16 border-b border-border shrink-0">
        <img
          src="/candlemind-logo.png"
          alt="CandleMind"
          className="h-8 w-8 shrink-0 rounded-md object-cover"
        />
        <span className="hidden md:block font-bold text-accent text-lg">CandleMind</span>
      </div>
      <nav className="flex-1 py-4 space-y-1">
        {nav.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            aria-label={label}
            title={label}
            className={({ isActive }) =>
              clsx(
                "flex items-center gap-3 px-4 py-2.5 mx-2 rounded-lg text-sm transition-all",
                isActive
                  ? "bg-accent/10 text-accent font-semibold"
                  : "text-muted hover:bg-surface hover:text-white"
              )
            }
          >
            <Icon size={18} />
            <span className="hidden md:block">{label}</span>
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
