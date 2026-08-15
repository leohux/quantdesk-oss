import { useState, useEffect, useCallback, useRef } from "react";

const BASE = "";

/* ═══════════════════════════════════════════════════════
   TOKEN MANAGEMENT
   ═══════════════════════════════════════════════════════ */
const TOKEN_KEY = "quantdesk_token";
const REFRESH_KEY = "quantdesk_refresh";
const ROLE_KEY = "quantdesk_role";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(ROLE_KEY);
}
export function getRole(): string | null {
  return localStorage.getItem(ROLE_KEY);
}

/* ═══════════════════════════════════════════════════════
   FETCH WRAPPER
   ═══════════════════════════════════════════════════════ */
class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function formatApiDetail(detail: unknown): string {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (typeof d === "string") return d;
        if (d && typeof d === "object" && "msg" in d) {
          const loc = (d as any).loc;
          const where = Array.isArray(loc) ? loc.join(".") : "";
          return where ? `${where}: ${(d as any).msg}` : String((d as any).msg);
        }
        return JSON.stringify(d);
      })
      .join("; ");
  }
  if (typeof detail === "object") {
    const o = detail as Record<string, unknown>;
    if (typeof o.message === "string") return o.message;
    if (typeof o.error === "string") return o.error;
    try {
      return JSON.stringify(detail);
    } catch {
      return String(detail);
    }
  }
  return String(detail);
}


let _refreshInFlight: Promise<string | null> | null = null;

export function setRefreshToken(token: string) {
  localStorage.setItem(REFRESH_KEY, token);
}

async function refreshAccessToken(): Promise<string | null> {
  const refresh = localStorage.getItem(REFRESH_KEY);
  if (!refresh) return null;
  if (_refreshInFlight) return _refreshInFlight;
  _refreshInFlight = (async () => {
    try {
      const res = await fetch(`${BASE}/api/auth/jwt-refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!res.ok) return null;
      const data = await res.json();
      if (data.access_token) setToken(data.access_token);
      if (data.refresh_token) setRefreshToken(data.refresh_token);
      return data.access_token || null;
    } catch {
      return null;
    } finally {
      _refreshInFlight = null;
    }
  })();
  return _refreshInFlight;
}

async function request<T = any>(
  path: string,
  opts: RequestInit & { _retried?: boolean } = {}
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(token ? { "X-Access-Token": token } : {}),
    ...(opts.headers as Record<string, string> || {}),
  };

  const res = await fetch(`${BASE}${path}`, { ...opts, headers });

  if (res.status === 401) {
    if (!opts._retried) {
      const newToken = await refreshAccessToken();
      if (newToken) {
        return request<T>(path, { ...opts, _retried: true });
      }
    }
    clearToken();
    window.location.href = "/login";
    throw new ApiError("Unauthorized", 401);
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(
      formatApiDetail(body.detail) || `HTTP ${res.status}`,
      res.status
    );
  }

  return res.json();
}

export function apiGet<T = any>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

export function apiPost<T = any>(path: string, data?: any, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: data ? JSON.stringify(data) : undefined,
    signal,
  });
}

export function apiPatch<T = any>(path: string, data?: any): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    body: data ? JSON.stringify(data) : undefined,
  });
}

export function apiPut<T = any>(path: string, data?: any): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    body: data ? JSON.stringify(data) : undefined,
  });
}

export function apiDelete<T = any>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

/* ═══════════════════════════════════════════════════════
   TYPES
   ═══════════════════════════════════════════════════════ */
export interface Account {
  equity: string;
  cash: string;
  buying_power: string;
  last_equity: string;
  status: string;
  currency: string;
}

export interface Position {
  symbol: string;
  qty: string;
  avg_entry_price: string;
  current_price: string;
  market_value: string;
  unrealized_pl: string;
  unrealized_plpc: string;
  side: string;
  weight_pct?: number;
  change_today?: string;
  strategy_id?: string | null;
  strategy_name?: string | null;
}

export interface StrategyPnl {
  strategy_id: string;
  strategy_name: string;
  symbols: string[];
  qty_positions: number;
  market_value: number;
  unrealized_pnl: number;
  realized_pnl: number;
  today_realized_pnl: number;
  closed_trades: number;
  total_pnl: number;
}

export interface JournalTrade {
  trade_id: string;
  strategy_id: string;
  strategy_name: string;
  symbol: string;
  side: string;
  status: string;
  qty: number;
  entry_price: number | null;
  exit_price: number | null;
  realized_pnl: number | null;
  /** Broker mark for open lots (null when closed / mark unavailable). */
  current_price?: number | null;
  /** Floating P&L for open lots. */
  unrealized_pnl?: number | null;
  return_pct: number | null;
  holding_days: number | null;
  signal_reason: string;
  opened_at: string | null;
  closed_at: string | null;
}

/** Display P&L: floating for open, realized for closed. */
export function journalPnl(t: JournalTrade): number | null {
  if (t.status === "open") {
    return t.unrealized_pnl != null ? t.unrealized_pnl : null;
  }
  return t.realized_pnl != null ? t.realized_pnl : null;
}

/** /api/journal response — totals match dashboard portfolio_pnl. */
export interface JournalResponse {
  trades: JournalTrade[];
  count: number;
  realized_pnl: number;
  today_realized_pnl: number;
  closed_trades: number;
  open_trades: number;
}

export interface EquityPoint {
  ts: string | null;
  equity: number;
  cash: number | null;
}

export interface Order {
  id: string;
  symbol: string;
  qty: string;
  filled_qty: string;
  side: string;
  type: string;
  status: string;
  limit_price?: string;
  stop_price?: string;
  filled_avg_price?: string;
  submitted_at: string;
  filled_at?: string;
  canceled_at?: string;
  created_at?: string;
}

export interface Strategy {
  id: string;
  name: string;
  type: string;
  description: string;
  enabled: boolean;
  params: Record<string, any>;
  code?: string;
  metrics?: {
    sharpe?: number;
    total_return_pct?: number;
    max_drawdown_pct?: number;
  };
  updated_at?: string;
}

export interface DashboardData {
  account: Account;
  positions: Position[];
  orders: Order[];
  strategies: Strategy[];
  strategy_pnl?: StrategyPnl[];
  summary: {
    cash: number;
    buying_power: number;
    equity: number;
    positions_count: number;
    invested_pct: number;
    today_pnl: number;
    today_pnl_pct: number;
    unrealized_pnl: number;
    unrealized_pnl_pct: number;
    realized_pnl?: number;
    today_realized_pnl?: number;
    running_strategies: number;
    mode: string;
  };
}

export interface BacktestResult {
  engine?: string;
  symbols?: string[];
  strategy_id?: string;
  params?: Record<string, any>;
  init_cash?: number;
  total_return_pct?: number;
  max_drawdown_pct?: number;
  sharpe?: number;
  sortino?: number;
  calmar?: number;
  win_rate_pct?: number;
  profit_factor?: number;
  trades?: number;
  end_value?: number;
  buy_hold_return_pct?: number;
  equity_curve?: { date: string; equity: number }[];
  drawdown_curve?: { date: string; drawdown_pct: number }[];
  annual_returns?: { year: number; return_pct: number }[];
  per_symbol?: {
    symbol: string;
    total_return_pct?: number;
    sharpe?: number;
    max_drawdown_pct?: number;
    trades?: number;
  }[];
  errors?: { symbol: string; error: string }[];
}

export interface HealthStatus {
  ok: boolean;
  mode: string;
  has_alpaca_keys: boolean;
  data_provider: string;
  domain: string;
  version: string;
}

export interface SystemMetrics {
  cpu_percent: number;
  memory_percent: number;
  memory_used_mb: number;
  memory_total_mb: number;
  disk_percent: number;
  uptime_seconds: number;
  redis_connected: boolean;
  postgres_connected: boolean;
}

/* ═══════════════════════════════════════════════════════
   API FUNCTIONS
   ═══════════════════════════════════════════════════════ */
export const api = {
  health: () => apiGet<HealthStatus>("/api/health"),
  login: (token: string) =>
    apiPost<{ ok: boolean; token: string }>("/api/auth/login", { token }),
  dashboard: () => apiGet<DashboardData>("/api/dashboard"),
  journal: (status?: string, limit = 100, strategyId?: string) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (status) qs.set("status", status);
    if (strategyId) qs.set("strategy_id", strategyId);
    return apiGet<JournalResponse>(`/api/journal?${qs.toString()}`);
  },
  equityCurve: (days = 30) =>
    apiGet<{ days: number; points: EquityPoint[] }>(`/api/equity/curve?days=${days}`),
  strategies: (scope?: "manual" | "mined" | "all") => {
    if (scope === "manual") return apiGet<Strategy[]>("/api/strategies?scope=manual");
    if (scope === "mined") return apiGet<Strategy[]>("/api/strategies?scope=mined");
    return apiGet<Strategy[]>("/api/strategies");
  },
  strategy: (id: string) => apiGet<Strategy>(`/api/strategies/${id}`),
  createStrategy: (data: Partial<Strategy>) =>
    apiPost<Strategy>("/api/strategies", data),
  updateStrategy: (id: string, data: Partial<Strategy>) =>
    apiPatch<Strategy>(`/api/strategies/${id}`, data),
  deleteStrategy: (id: string) =>
    apiDelete(`/api/strategies/${id}`),
  validateCode: (code: string) =>
    apiPost<{ ok: boolean }>("/api/strategies/validate", { code }),
  templates: () => apiGet<Record<string, string>>("/api/strategies/templates"),
  backtest: (data: any, signal?: AbortSignal) =>
    apiPost<BacktestResult>("/api/backtest", data, signal),
  settings: () => apiGet<Record<string, any>>("/api/settings"),
  saveSettings: (data: any) => apiPut("/api/settings", data),
  placeOrder: (data: { symbol: string; qty: number; side: string }) =>
    apiPost("/api/orders", data),
  positions: () => apiGet<Position[]>("/api/positions"),
  orders: (status = "all", limit = 50) =>
    apiGet<Order[]>(`/api/orders?status=${status}&limit=${limit}`),
  account: () => apiGet<Account>("/api/account"),
  systemMetrics: () => apiGet<SystemMetrics>("/api/system/metrics"),
};

/* ═══════════════════════════════════════════════════════
   REACT HOOKS
   ═══════════════════════════════════════════════════════ */

/**
 * Auto-refreshing data hook.
 * Fetches data immediately and refreshes every `interval` ms.
 * Returns { data, loading, error, refetch }.
 */
export function useApiData<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  interval = 0, // 0 = no auto-refresh
  deps: any[] = []
): {
  data: T | null;
  loading: boolean;
  error: Error | null;
  refetch: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const mountedRef = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const doFetch = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await fetcherRef.current(signal ?? new AbortController().signal);
      if (mountedRef.current && !signal?.aborted) {
        setData(result);
        setError(null);
        setLoading(false);
      }
    } catch (err: any) {
      if (err?.name === "AbortError" || signal?.aborted) return;
      if (mountedRef.current) {
        setError(err instanceof Error ? err : new Error(String(err)));
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    setLoading(true);
    doFetch(controller.signal);

    let timer: ReturnType<typeof setInterval> | undefined;
    if (interval > 0) {
      timer = setInterval(() => {
        if (!controller.signal.aborted) doFetch();
      }, interval);
    }

    return () => {
      mountedRef.current = false;
      controller.abort();
      if (timer) clearInterval(timer);
    };
  }, [doFetch, interval, ...deps]);

  return { data, loading, error, refetch: doFetch };
}

/**
 * Polling hook for near-realtime updates.
 * Defaults to 5 second interval.
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs = 5000,
  deps: any[] = []
) {
  return useApiData(fetcher, intervalMs, deps);
}

/* ═══════════════════════════════════════════════════════
   FORMATTERS
   ═══════════════════════════════════════════════════════ */
export function fmtMoney(v: number | string | undefined | null): string {
  if (v === undefined || v === null || v === "") return "—";
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (isNaN(n)) return "—";
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function fmtPct(v: number | string | undefined | null): string {
  if (v === undefined || v === null || v === "") return "—";
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

export function fmtNumber(v: number | string | undefined | null, decimals = 0): string {
  if (v === undefined || v === null || v === "") return "—";
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (isNaN(n)) return "—";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function fmtQty(v: number | string | undefined | null): string {
  if (v === undefined || v === null || v === "") return "—";
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (isNaN(n)) return "—";
  return Number.isInteger(n) ? n.toString() : n.toFixed(4);
}

export function normKey(v: string | undefined | null): string {
  if (!v) return "";
  const s = String(v);
  const bare = s.includes(".") ? s.split(".").pop()! : s;
  return bare.toLowerCase();
}

export function sideLabel(side: string): string {
  const s = normKey(side);
  return s === "buy" ? "买入" : s === "sell" ? "卖出" : side;
}

export function sideColor(side: string): string {
  return normKey(side) === "buy" ? "var(--green)" : "var(--red)";
}

export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    new: "新建",
    accepted: "已接受",
    pending_new: "待提交",
    accepted_for_bidding: "已接受",
    partially_filled: "部分成交",
    filled: "已成交",
    done_for_day: "今日完成",
    canceled: "已取消",
    expired: "已过期",
    replaced: "已替换",
    pending_cancel: "取消中",
    pending_replace: "修改中",
    stopped: "已停止",
    rejected: "已拒绝",
    suspended: "已暂停",
    calculated: "计算中",
    held: "已挂起",
    open: "挂单中",
    closed: "已平仓",
    active: "活跃",
  };
  return map[normKey(status)] || status;
}

export function pnlColor(v: number | string | undefined | null): string {
  if (v === undefined || v === null || v === "") return "var(--text)";
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (isNaN(n) || n === 0) return "var(--text)";
  return n > 0 ? "var(--green)" : "var(--red)";
}

export function timeAgo(dateStr: string): string {
  if (!dateStr) return "";
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diff = now - then;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`;
  return `${Math.floor(diff / 86400000)} 天前`;
}

/* ═══════════════════════════════════════════════════════
   WEBSOCKET HOOK
   ═══════════════════════════════════════════════════════ */

/**
 * Connect to a WebSocket channel for real-time updates.
 * Returns { data, connected } where data is the latest message.
 * Auto-reconnects on disconnect.
 */
export function useWebSocket<T = any>(
  channel: string,
  onMessage?: (data: T) => void
): { data: T | null; connected: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  useEffect(() => {
    const token = getToken();
    if (!token) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const url = `${protocol}//${host}/ws/${channel}?token=${encodeURIComponent(token)}`;
    let stopped = false;

    function connect() {
      if (stopped) return;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data) as T;
          setData(parsed);
          onMessageRef.current?.(parsed);
        } catch {
          // non-JSON message (pong, etc.)
        }
      };

      ws.onclose = (ev) => {
        setConnected(false);
        // 4001 auth / 4004 unknown channel — do not spin reconnect
        if (stopped || ev.code === 4001 || ev.code === 4004) return;
        reconnectRef.current = setTimeout(connect, 3000);
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    connect();

    // Ping every 25 seconds to keep alive
    const pingInterval = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send("ping");
      }
    }, 25000);

    return () => {
      stopped = true;
      clearInterval(pingInterval);
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null; // prevent reconnect on cleanup
        wsRef.current.close();
      }
    };
  }, [channel]);

  return { data, connected };
}
