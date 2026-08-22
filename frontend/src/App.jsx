import React, { Suspense } from "react";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { LoaderCircle } from "lucide-react";
import Sidebar from "./components/Sidebar";
import Header from "./components/Header";

const Dashboard = React.lazy(() => import("./pages/Dashboard"));
const Markets = React.lazy(() => import("./pages/Markets"));
const Orders = React.lazy(() => import("./pages/Orders"));
const Strategies = React.lazy(() => import("./pages/Strategies"));
const Settings = React.lazy(() => import("./pages/Settings"));

function PageFallback() {
  return (
    <div
      className="flex min-h-[40vh] items-center justify-center text-muted"
      role="status"
      aria-label="Loading page"
    >
      <LoaderCircle className="animate-spin" size={22} aria-hidden="true" />
    </div>
  );
}

export function mainOverflowClass(pathname) {
  return pathname === "/markets" ? "min-h-0 overflow-y-hidden" : "overflow-y-auto";
}

export default function App() {
  const { pathname } = useLocation();
  const marketOverflowClass = mainOverflowClass(pathname);

  return (
    <div className="flex h-screen bg-bg text-gray-100 overflow-hidden">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <Header />
        <main className={`min-w-0 flex-1 overflow-x-hidden p-2 sm:p-4 ${marketOverflowClass}`}>
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/"         element={<Dashboard />} />
              <Route path="/markets"  element={<Markets />} />
              <Route path="/orders"   element={<Orders />} />
              <Route path="/strategies" element={<Strategies />} />
              <Route path="/backtest" element={<Navigate to="/strategies" replace />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*"         element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  );
}
