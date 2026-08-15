import { Component, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { DashboardPage } from "./pages/DashboardPage";
import { StrategyPage } from "./pages/StrategyPage";
import { BacktestPage } from "./pages/BacktestPage";
import { PaperPage } from "./pages/PaperPage";
import { JournalPage } from "./pages/JournalPage";
import { LivePage } from "./pages/LivePage";
import { SettingsPage } from "./pages/SettingsPage";
import { LoginPage } from "./pages/LoginPage";
import { getToken } from "./lib/api";

class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen grid place-items-center p-6">
          <div className="max-w-md space-y-3 text-center">
            <div className="text-lg font-semibold">出错了</div>
            <div className="text-sm text-[var(--muted)] mono break-all">
              {this.state.error.message}
            </div>
            <button
              className="px-4 py-2 rounded-lg bg-[var(--teal)]/20 text-[var(--teal)] border border-[var(--teal)]/40 cursor-pointer"
              onClick={() => window.location.reload()}
            >
              重试
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  if (!getToken()) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <ErrorBoundary>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route path="/" element={<DashboardPage />} />
          <Route path="/strategy" element={<StrategyPage />} />
          <Route path="/backtest" element={<BacktestPage />} />
          <Route path="/paper" element={<PaperPage />} />
          <Route path="/journal" element={<JournalPage />} />
          <Route path="/live" element={<LivePage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </ErrorBoundary>
  );
}
