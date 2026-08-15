import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Activity, BookOpen, CandlestickChart, FlaskConical, LayoutDashboard,
  LogOut, Settings, ShieldAlert, Workflow, Menu, X,
} from "lucide-react";
import { clearToken, usePolling, apiGet } from "../lib/api";
import { StatusDot } from "./ui";

const navItems = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/strategy", label: "策略实验室", icon: Workflow },
  { to: "/backtest", label: "回测", icon: FlaskConical },
  { to: "/paper", label: "模拟交易", icon: CandlestickChart },
  { to: "/journal", label: "交易日记", icon: BookOpen },
  { to: "/live", label: "实盘交易", icon: ShieldAlert },
  { to: "/settings", label: "设置", icon: Settings },
];

export function AppShell() {
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { data: health } = usePolling((signal) => apiGet("/api/health", signal), 30000);

  const logout = () => {
    clearToken();
    navigate("/login");
  };

  return (
    <div className={`min-h-screen flex text-[15px] ${sidebarOpen ? "sidebar-open" : ""}`}>
      {/* Mobile overlay — closes drawer on tap */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-30 md:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-hidden
        />
      )}

      {/* Sidebar: mobile show/hide driven by .sidebar-open in index.css
          (unlayered CSS beats Tailwind utilities, so class toggle is required) */}
      <aside
        className="w-[220px] shrink-0 border-r border-[var(--border)] bg-[#0a1018]/95 backdrop-blur px-4 py-5 flex flex-col z-40
          max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:w-[min(220px,85vw)]"
      >
        {/* Logo */}
        <div className="mb-8 px-2 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="h-8 w-8 rounded-lg bg-[var(--teal)]/15 border border-[var(--teal)]/40 grid place-items-center">
              <Activity className="h-4 w-4 text-[var(--teal)]" />
            </div>
            <div>
              <div className="font-semibold tracking-wide">QuantDesk</div>
              <div className="text-[11px] tracking-[0.08em] text-[var(--muted)]">美股量化</div>
            </div>
          </div>
          <button
            className="md:hidden text-[var(--muted)] hover:text-[var(--text)] cursor-pointer"
            onClick={() => setSidebarOpen(false)}
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Nav */}
        <nav className="space-y-1 flex-1">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={() => setSidebarOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2.5 transition ${
                  isActive
                    ? "bg-[var(--teal)]/10 text-white border-l-2 border-[var(--teal)]"
                    : "text-[var(--muted)] hover:bg-white/5 hover:text-white"
                }`
              }
            >
              <item.icon className="h-4 w-4" />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        {/* Footer */}
        <div className="mt-auto px-2 pt-4 text-xs text-[var(--muted)] space-y-2">
          <div className="rounded-lg border border-[var(--border)] p-3 bg-black/20">
            <div className="flex items-center gap-1.5 mb-1">
              <StatusDot status={health?.ok ? "online" : "offline"} />
              <span className="text-[var(--teal)] font-medium">
                {health?.mode === "paper" ? "模拟盘" : health?.mode || "..."}
              </span>
            </div>
            <div className="mono text-[10px]">{health?.domain || "quantdesk.example.com"}</div>
            <div className="mono text-[10px] mt-0.5">v{health?.version || "?"}</div>
          </div>
          <button
            onClick={logout}
            className="w-full flex items-center gap-2 rounded-lg px-3 py-2 text-[var(--muted)] hover:bg-white/5 hover:text-white cursor-pointer"
          >
            <LogOut className="h-4 w-4" />
            退出登录
          </button>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 min-w-0">
        {/* Mobile top bar */}
        <div className="md:hidden flex items-center gap-3 px-4 py-3 border-b border-[var(--border)]">
          <button
            type="button"
            aria-label="打开导航菜单"
            aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen(true)}
            className="text-[var(--muted)] hover:text-[var(--text)] cursor-pointer"
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-[var(--teal)]" />
            <span className="font-semibold text-sm">QuantDesk</span>
          </div>
        </div>

        <Outlet />
      </main>
    </div>
  );
}
