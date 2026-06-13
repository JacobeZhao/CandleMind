import React from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import Header from "./components/Header";
import Dashboard from "./pages/Dashboard";
import Markets from "./pages/Markets";
import Orders from "./pages/Orders";
import Settings from "./pages/Settings";

export default function App() {
  return (
    <div className="flex h-screen bg-bg text-gray-100 overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 overflow-hidden">
        <Header />
        <main className="flex-1 overflow-y-auto p-4">
          <Routes>
            <Route path="/"         element={<Dashboard />} />
            <Route path="/markets"  element={<Markets />} />
            <Route path="/orders"   element={<Orders />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*"         element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
