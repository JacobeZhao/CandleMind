import React, { Suspense } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { LoaderCircle } from "lucide-react";
import Sidebar from "./components/Sidebar";
import Header from "./components/Header";

const Dashboard = React.lazy(() => import("./pages/Dashboard"));
const Markets = React.lazy(() => import("./pages/Markets"));
const Orders = React.lazy(() => import("./pages/Orders"));
const Backtest = React.lazy(() => import("./pages/Backtest"));
const Research = React.lazy(() => import("./pages/Research"));
const Strategy = React.lazy(() => import("./pages/Strategy"));
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

export default function App() {
  return (
    <div className="flex h-screen bg-bg text-gray-100 overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 overflow-hidden">
        <Header />
        <main className="flex-1 overflow-y-auto p-4">
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/"         element={<Dashboard />} />
              <Route path="/markets"  element={<Markets />} />
              <Route path="/orders"   element={<Orders />} />
              <Route path="/backtest" element={<Backtest />} />
              <Route path="/research" element={<Research />} />
              <Route path="/strategy" element={<Strategy />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*"         element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  );
}
