import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import {
  DollarSign, TrendingUp, TrendingDown, BarChart3, Activity, Briefcase,
  ArrowUpRight, ArrowDownRight, RefreshCw,
} from "lucide-react";
import {
  PageHeader, MetricCard, Panel, Table, Th, Td, Tr, Badge, Button,
  LoadingOverlay, StatusDot, ProgressBar, Modal, Tabs,
} from "../components/ui";
import {
  apiGet, usePolling, useWebSocket, fmtMoney, fmtPct, fmtQty, sideLabel, normKey,
  statusLabel, pnlColor, timeAgo, journalPnl,
} from "../lib/api";
import type {
  DashboardData, Position, Order, StrategyPnl, EquityPoint, JournalTrade,
} from "../lib/api";

/** Custom tooltip — Recharts paints item text with series fill (often unreadable). */
function ChartTooltip({
  active,
  payload,
  label,
  valueLabel,
}: {
  active?: boolean;
  payload?: ReadonlyArray<{ value?: unknown }>;
  label?: unknown;
  valueLabel: string;
}) {
  if (!active || !payload?.length) return null;
  const raw = payload[0]?.value;
  const num = typeof raw === "number" ? raw : Number(raw);
  const value = Number.isFinite(num) ? fmtMoney(num) : String(raw ?? "");
  const title = label == null || label === "" ? "" : String(label);
  return (
    <div
      style={{
        background: "#0e141c",
        border: "1px solid #1e2a38",
        borderRadius: 8,
        padding: "8px 10px",
        fontSize: 12,
        color: "#e8eef6",
        lineHeight: 1.45,
      }}
    >
      {title && (
        <div style={{ fontWeight: 600, marginBottom: 2, color: "#e8eef6" }}>{title}</div>
      )}
      <div style={{ color: "#e8eef6" }}>
        <span style={{ color: "#8b9bb0" }}>{valueLabel}</span>
        {" "}
        <span style={{ color: "#e8eef6", fontVariantNumeric: "tabular-nums" }}>{value}</span>
      </div>
    </div>
  );
}

export function DashboardPage() {
  // REST polling as base data (30s interval, slower since WS handles real-time)
  const { data, loading, error, refetch } = usePolling(
    (signal) => apiGet<DashboardData>("/api/dashboard", signal),
    30000
  );

  const { data: health } = usePolling(
    (signal) => apiGet("/api/health", signal),
    30000
  );
  const { data: curveData } = usePolling(
    (signal) => apiGet<{ days: number; points: EquityPoint[] }>("/api/equity/curve?days=30", signal),
    60000
  );

  // WebSocket for real-time updates
  const { data: wsPositions, connected: wsConnected } = useWebSocket<any>("positions");

  if (loading && !data) return <LoadingOverlay message="加载仪表盘..." />;
  if (error && !data) {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-4">
        <span className="text-[var(--red)] text-sm">加载失败: {error.message}</span>
        <Button variant="secondary" size="sm" onClick={refetch}>重试</Button>
      </div>
    );
  }

  if (!data) return null;

  // Prefer live WS marks only while connected; keep REST ownership/weights.
  const restPositions = data.positions || [];
  const wsPos = wsPositions?.positions;
  const wsAcct = wsPositions?.account;
  const wsUsable =
    wsConnected &&
    Array.isArray(wsPos) &&
    !(wsPos.length === 0 && restPositions.length > 0);
  const positions = wsUsable
    ? mergeLivePositions(restPositions, wsPos)
    : restPositions;
  const liveAccount =
    wsConnected &&
    wsAcct &&
    typeof wsAcct === "object" &&
    Object.keys(wsAcct).length > 0
      ? wsAcct
      : null;
  const account = liveAccount || data.account || {};
  const { orders = [], strategies = [], summary: restSummary, strategy_pnl = [] } = data;

  // Recalculate summary with live positions if available
  const equity = parseFloat(String(account?.equity ?? "0"));
  const cash = parseFloat(String(account?.cash ?? "0"));
  const lastEquity = parseFloat(String(account?.last_equity ?? "0"));
  const invested = positions.reduce((s: number, p: any) => s + parseFloat(p.market_value || "0"), 0);
  const todayPnl = lastEquity ? equity - lastEquity : 0;
  const unrealizedPnl = positions.reduce((s: number, p: any) => s + parseFloat(p.unrealized_pl || "0"), 0);
  const summary = wsUsable ? {
    ...restSummary,
    cash,
    equity,
    today_pnl: todayPnl,
    today_pnl_pct: lastEquity ? Math.round(todayPnl / lastEquity * 100 * 100) / 100 : 0,
    unrealized_pnl: unrealizedPnl,
    unrealized_pnl_pct: equity ? Math.round(unrealizedPnl / equity * 100 * 100) / 100 : 0,
    invested_pct: equity ? Math.round(invested / equity * 100 * 100) / 100 : 0,
    positions_count: positions.length,
  } : restSummary;
  const realizedPnl = Number(summary.realized_pnl ?? 0);
  const curvePoints = curveData?.points || [];
  const todayOrders = orders.filter((o) => {
    const d = o.submitted_at || o.created_at;
    if (!d) return false;
    return new Date(d).toDateString() === new Date().toDateString();
  });

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="仪表盘"
        subtitle="实时概览"
        right={
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5 text-xs text-[var(--muted)]">
              <StatusDot status={health?.ok ? "online" : "offline"} />
              <span>{health?.ok ? "系统正常" : "异常"}</span>
            </div>
            <span className="text-xs text-[var(--muted)] mono">v{health?.version || "?"}</span>
            <Button variant="ghost" size="sm" onClick={refetch}>
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
        }
      />

      <div className="p-6 space-y-6">
        {/* ── Row 1: Key Metrics ── */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          <MetricCard
            label="账户权益"
            value={fmtMoney(summary.equity)}
            icon={<DollarSign className="h-6 w-6" />}
            tone="teal"
          />
          <MetricCard
            label="今日盈亏"
            value={fmtMoney(summary.today_pnl)}
            hint={fmtPct(summary.today_pnl_pct)}
            tone={summary.today_pnl >= 0 ? "pos" : "neg"}
            icon={summary.today_pnl >= 0 ? <TrendingUp className="h-6 w-6" /> : <TrendingDown className="h-6 w-6" />}
          />
          <MetricCard
            label="未实现盈亏"
            value={fmtMoney(summary.unrealized_pnl)}
            hint={fmtPct(summary.unrealized_pnl_pct)}
            tone={summary.unrealized_pnl >= 0 ? "pos" : "neg"}
            icon={summary.unrealized_pnl >= 0 ? <TrendingUp className="h-6 w-6" /> : <TrendingDown className="h-6 w-6" />}
          />
          <MetricCard
            label="累计已实现"
            value={fmtMoney(realizedPnl)}
            hint={summary.today_realized_pnl ? `今日已实现 ${fmtMoney(summary.today_realized_pnl)}` : "journal 结算"}
            tone={realizedPnl >= 0 ? "pos" : "neg"}
            icon={<BarChart3 className="h-6 w-6" />}
          />
          <MetricCard
            label="已投资"
            value={`${Number(summary.invested_pct ?? 0).toFixed(1)}%`}
            hint={`${summary.positions_count} 持仓`}
            icon={<Briefcase className="h-6 w-6" />}
            tone={summary.invested_pct > 100 ? "amber" : "teal"}
          />
          <MetricCard
            label="运行策略"
            value={summary.running_strategies.toString()}
            hint={`现金 ${fmtMoney(summary.cash)}`}
            icon={<Activity className="h-6 w-6" />}
            tone={summary.running_strategies > 0 ? "pos" : "neutral"}
          />
        </div>

        {/* ── Row 2: Equity curve + charts ── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <Panel title="Paper 权益曲线" subtitle="近 30 天快照" className="lg:col-span-2">
            <EquityCurveChart points={curvePoints} />
          </Panel>
          <Panel title="持仓盈亏分布" className="lg:col-span-1">
            <PnLBarChart positions={positions} />
          </Panel>
        </div>

        {/* ── Row 2b: Allocation + Strategy live P&L ── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <Panel title="仓位分配" className="lg:col-span-1">
            <PositionAllocation positions={positions} equity={summary.equity} />
          </Panel>
          <Panel title="策略盈亏" subtitle="未实现 + 已实现" className="lg:col-span-2">
            <StrategyPnlTable rows={strategy_pnl} />
          </Panel>
        </div>

        {/* ── Row 3: Positions + Orders ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Open Positions */}
          <Panel
            title="当前持仓"
            subtitle={`${positions.length} 个`}
            right={
              <Badge variant={positions.length > 0 ? "success" : "default"}>
                {positions.length > 0 ? "持仓中" : "空仓"}
              </Badge>
            }
          >
            <PositionsTable positions={positions} />
          </Panel>

          {/* Today's Orders */}
          <Panel
            title="今日订单"
            subtitle={`${todayOrders.length} 笔`}
            right={
              <span className="text-xs text-[var(--muted)]">
                总历史 {orders.length} 笔
              </span>
            }
          >
            <OrdersTable orders={todayOrders} />
          </Panel>
        </div>

        {/* ── Row 4: Risk + Health ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Panel title="风险概览">
            <RiskOverview summary={summary} positions={positions} />
          </Panel>
          <Panel title="系统健康">
            <SystemHealthBar health={health} />
          </Panel>
        </div>
      </div>
    </div>
  );
}

/* ─── PnL Bar Chart (HTML — avoids Recharts tooltip/tick color bugs) ─── */
function PnLBarChart({ positions }: { positions: Position[] }) {
  if (positions.length === 0) {
    return <div className="h-[200px] flex items-center justify-center text-sm text-[var(--muted)]">暂无持仓</div>;
  }

  const data = positions
    .map((p) => ({
      symbol: p.symbol,
      pnl: parseFloat(p.unrealized_pl || "0"),
    }))
    .sort((a, b) => b.pnl - a.pnl);

  const maxAbs = Math.max(...data.map((d) => Math.abs(d.pnl)), 1);

  return (
    <div className="flex flex-col h-[220px]">
      <div className="pnl-scroll flex-1 min-h-0 overflow-y-auto overscroll-contain space-y-1.5 pr-1.5">
        {data.map((row) => {
          const pct = (Math.abs(row.pnl) / maxAbs) * 100;
          const pos = row.pnl >= 0;
          return (
            <div key={row.symbol} className="flex items-center gap-2 text-xs" title={`盈亏 ${fmtMoney(row.pnl)}`}>
              <div className="w-12 shrink-0 mono font-medium text-[#e8eef6]">{row.symbol}</div>
              <div className="relative flex-1 h-4 rounded bg-[#121a24]">
                <div className="absolute inset-y-0 left-1/2 w-px bg-[#1e2a38]" />
                {pos ? (
                  <div
                    className="absolute top-0.5 bottom-0.5 left-1/2 rounded-r"
                    style={{ width: `${pct / 2}%`, background: "#34d399", opacity: 0.85 }}
                  />
                ) : (
                  <div
                    className="absolute top-0.5 bottom-0.5 right-1/2 rounded-l"
                    style={{ width: `${pct / 2}%`, background: "#f87171", opacity: 0.85 }}
                  />
                )}
              </div>
              <div
                className="w-[72px] shrink-0 text-right mono tabular-nums"
                style={{ color: pos ? "#34d399" : "#f87171" }}
              >
                {fmtMoney(row.pnl)}
              </div>
            </div>
          );
        })}
      </div>
      {data.length > 8 && (
        <div className="pt-1.5 text-[10px] text-[var(--muted)] text-right shrink-0">
          共 {data.length} 个 · 可下滑
        </div>
      )}
    </div>
  );
}

/* ─── Position Allocation ─── */
function PositionAllocation({ positions, equity }: { positions: Position[]; equity: number }) {
  if (positions.length === 0) {
    return <div className="h-[200px] flex items-center justify-center text-sm text-[var(--muted)]">暂无持仓</div>;
  }

  const colors = ["var(--teal)", "var(--green)", "var(--amber)", "var(--red)", "#818cf8", "#f472b6", "#a78bfa", "#38bdf8"];

  return (
    <div className="space-y-2.5 max-h-[200px] overflow-y-auto">
      {positions
        .sort((a, b) => parseFloat(b.market_value || "0") - parseFloat(a.market_value || "0"))
        .map((p, i) => {
          const weight = p.weight_pct || (parseFloat(p.market_value || "0") / equity * 100);
          return (
            <div key={p.symbol} className="flex items-center gap-3">
              <div className="w-14 text-xs font-medium mono text-[var(--text)]">{p.symbol}</div>
              <div className="flex-1">
                <ProgressBar value={weight} max={100} color={i < 2 ? "teal" : i < 4 ? "green" : "amber"} />
              </div>
              <div className="w-14 text-right text-xs mono text-[var(--muted)]">{Number(weight || 0).toFixed(1)}%</div>
              <div className="w-20 text-right text-xs mono" style={{ color: pnlColor(p.unrealized_pl) }}>
                {fmtMoney(p.unrealized_pl)}
              </div>
            </div>
          );
        })}
    </div>
  );
}

function shortStrategy(name: string): string {
  if (!name) return "-";
  if (name.length <= 22) return name;
  return name.slice(0, 20) + "…";
}

/* ─── Live strategy P&L (not backtest Sharpe) ─── */
function StrategyPnlTable({ rows }: { rows: StrategyPnl[] }) {
  const [selected, setSelected] = useState<StrategyPnl | null>(null);

  if (!rows.length) {
    return <div className="h-[200px] flex items-center justify-center text-sm text-[var(--muted)]">暂无策略盈亏</div>;
  }
  return (
    <>
      <div className="max-h-[260px] overflow-y-auto">
        <Table>
          <thead>
            <tr>
              <Th>策略</Th>
              <Th>持仓</Th>
              <Th>市值</Th>
              <Th>未实现</Th>
              <Th>已实现</Th>
              <Th>合计</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Tr
                key={r.strategy_id}
                className="cursor-pointer"
                onClick={() => setSelected(r)}
                title="点击查看已平仓与历史交易"
              >
                <Td>
                  <div className="text-sm font-medium" title={r.strategy_name}>
                    {shortStrategy(r.strategy_name)}
                  </div>
                  <div className="text-[11px] text-[var(--muted)]">
                    {r.symbols.length ? r.symbols.join(", ") : "空仓"}
                    {r.closed_trades > 0 && (
                      <>
                        {" · "}
                        <button
                          type="button"
                          className="text-[var(--teal)] hover:underline cursor-pointer"
                          onClick={(e) => {
                            e.stopPropagation();
                            setSelected(r);
                          }}
                        >
                          {r.closed_trades} 笔已平
                        </button>
                      </>
                    )}
                  </div>
                </Td>
                <Td className="mono">{r.qty_positions}</Td>
                <Td className="mono">{fmtMoney(r.market_value)}</Td>
                <Td className="mono" style={{ color: pnlColor(r.unrealized_pnl) }}>
                  {fmtMoney(r.unrealized_pnl)}
                </Td>
                <Td className="mono" style={{ color: pnlColor(r.realized_pnl) }}>
                  {fmtMoney(r.realized_pnl)}
                </Td>
                <Td className="mono font-medium" style={{ color: pnlColor(r.total_pnl) }}>
                  {fmtMoney(r.total_pnl)}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </div>
      <StrategyTradesModal
        strategy={selected}
        onClose={() => setSelected(null)}
      />
    </>
  );
}

const journalTabs = [
  { key: "all", label: "全部" },
  { key: "closed", label: "已平仓" },
  { key: "open", label: "持仓中" },
];

function StrategyTradesModal({
  strategy,
  onClose,
}: {
  strategy: StrategyPnl | null;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState("all");
  const [trades, setTrades] = useState<JournalTrade[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sid = strategy?.strategy_id;

  const load = useCallback(async () => {
    if (!sid) return;
    setLoading(true);
    setError(null);
    try {
      const qs = new URLSearchParams({ limit: "200", strategy_id: sid });
      if (filter !== "all") qs.set("status", filter);
      const data = await apiGet<{ trades: JournalTrade[] }>(`/api/journal?${qs}`);
      // Client filter as fallback if API hasn't picked up strategy_id yet.
      // Also drop non-trade bookkeeping rows if an older API still returns them.
      const rows = (data.trades || []).filter(
        (t) =>
          t.strategy_id === sid &&
          t.status !== "signal_noise" &&
          t.status !== "superseded"
      );
      setTrades(rows);
    } catch (e: any) {
      setError(e?.message || "加载失败");
      setTrades([]);
    } finally {
      setLoading(false);
    }
  }, [sid, filter]);

  useEffect(() => {
    if (!strategy) {
      setTrades([]);
      setFilter("all");
      setError(null);
      return;
    }
    load();
  }, [strategy, load]);

  const realizedSum = trades
    .filter((t) => t.status === "closed" && t.realized_pnl != null)
    .reduce((s, t) => s + (t.realized_pnl || 0), 0);

  return (
    <Modal
      open={!!strategy}
      onClose={onClose}
      title={strategy?.strategy_name || "策略交易"}
      subtitle={
        strategy
          ? `${strategy.closed_trades} 笔已平 · 已实现 ${fmtMoney(strategy.realized_pnl)} · 合计 ${fmtMoney(strategy.total_pnl)}`
          : undefined
      }
      wide
    >
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <Tabs tabs={journalTabs} active={filter} onChange={setFilter} />
          <div className="flex items-center gap-2">
            {filter !== "open" && trades.some((t) => t.status === "closed") && (
              <span className="text-xs text-[var(--muted)]">
                已实现{" "}
                <span className="mono" style={{ color: pnlColor(realizedSum) }}>
                  {fmtMoney(realizedSum)}
                </span>
              </span>
            )}
            <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            </Button>
            <Link
              to="/journal"
              className="text-xs text-[var(--teal)] hover:underline"
              onClick={onClose}
            >
              打开交易日记 →
            </Link>
          </div>
        </div>

        {loading && trades.length === 0 ? (
          <div className="py-10 text-center text-sm text-[var(--muted)]">加载中...</div>
        ) : error ? (
          <div className="py-10 text-center text-sm text-[var(--red)]">{error}</div>
        ) : trades.length === 0 ? (
          <div className="py-10 text-center text-sm text-[var(--muted)]">
            {filter === "closed" ? "暂无已平仓记录" : "暂无交易记录"}
          </div>
        ) : (
          <div className="overflow-x-auto -mx-1">
            <Table>
              <thead>
                <tr>
                  <Th>标的</Th>
                  <Th>状态</Th>
                  <Th>数量</Th>
                  <Th>入场</Th>
                  <Th>出场</Th>
                  <Th>盈亏</Th>
                  <Th>收益</Th>
                  <Th>持有</Th>
                  <Th>时间</Th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => {
                  const pnl = journalPnl(t);
                  const markOrExit =
                    t.exit_price != null
                      ? t.exit_price
                      : t.status === "open"
                      ? t.current_price ?? null
                      : null;
                  return (
                  <Tr key={t.trade_id}>
                    <Td>
                      <span className="font-semibold mono">{t.symbol}</span>
                    </Td>
                    <Td>
                      <Badge
                        variant={
                          t.status === "closed"
                            ? "success"
                            : t.status === "open"
                            ? "info"
                            : "default"
                        }
                      >
                        {t.status === "closed"
                          ? "已平"
                          : t.status === "open"
                          ? "持仓"
                          : t.status}
                      </Badge>
                    </Td>
                    <Td className="mono">{fmtQty(t.qty)}</Td>
                    <Td className="mono">
                      {t.entry_price != null ? `$${t.entry_price.toFixed(2)}` : "-"}
                    </Td>
                    <Td className="mono">
                      {markOrExit != null ? `$${markOrExit.toFixed(2)}` : "-"}
                    </Td>
                    <Td className="mono" style={{ color: pnlColor(pnl) }}>
                      {pnl != null ? fmtMoney(pnl) : "-"}
                    </Td>
                    <Td className="mono" style={{ color: pnlColor(t.return_pct) }}>
                      {t.return_pct != null ? fmtPct(t.return_pct) : "-"}
                    </Td>
                    <Td className="mono text-[var(--muted)]">
                      {t.holding_days != null ? `${t.holding_days}d` : "-"}
                    </Td>
                    <Td className="text-xs text-[var(--muted)]">
                      {timeAgo(t.closed_at || t.opened_at || "")}
                    </Td>
                  </Tr>
                  );
                })}
              </tbody>
            </Table>
          </div>
        )}
      </div>
    </Modal>
  );
}

function EquityCurveChart({ points }: { points: EquityPoint[] }) {
  if (points.length < 2) {
    return (
      <div className="h-[220px] flex items-center justify-center text-sm text-[var(--muted)]">
        权益快照积累中（每约 5 分钟一点）— 稍后再看
      </div>
    );
  }
  const data = points.map((p) => ({
    t: p.ts ? new Date(p.ts).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "",
    equity: p.equity,
  }));
  const min = Math.min(...data.map((d) => d.equity));
  const max = Math.max(...data.map((d) => d.equity));
  const pad = (max - min) * 0.08 || 1;
  return (
    <div className="h-[220px]">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <defs>
            <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--teal)" stopOpacity={0.35} />
              <stop offset="100%" stopColor="var(--teal)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" opacity={0.3} />
          <XAxis dataKey="t" tick={{ fontSize: 10, fill: "var(--muted)" }} minTickGap={40} />
          <YAxis
            domain={[min - pad, max + pad]}
            tick={{ fontSize: 10, fill: "var(--muted)" }}
            tickFormatter={(v) => `$${Math.round(v / 1000)}k`}
            width={48}
          />
          <Tooltip
            content={(props) => <ChartTooltip {...props} valueLabel="权益" />}
          />
          <Area type="monotone" dataKey="equity" stroke="var(--teal)" fill="url(#eqFill)" strokeWidth={2} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Overlay WS marks onto REST rows so strategy/weight survive live ticks. */
function mergeLivePositions(rest: Position[], live: any[]): Position[] {
  const bySym = new Map(
    rest.map((p) => [String(p.symbol || "").toUpperCase(), p]),
  );
  return live.map((p) => {
    const r = bySym.get(String(p.symbol || "").toUpperCase());
    if (!r) return p as Position;
    return {
      ...r,
      ...p,
      strategy_id: p.strategy_id ?? r.strategy_id,
      strategy_name: p.strategy_name ?? r.strategy_name,
      weight_pct:
        p.weight_pct != null && Number(p.weight_pct) !== 0
          ? p.weight_pct
          : r.weight_pct,
    } as Position;
  });
}

/* ─── Positions Table ─── */
function PositionsTable({ positions }: { positions: Position[] }) {
  if (positions.length === 0) {
    return (
      <div className="py-8 text-center text-sm text-[var(--muted)]">
        空仓 — 策略运行后将自动建仓
      </div>
    );
  }

  return (
    <Table>
      <thead>
        <tr>
          <Th>标的</Th>
          <Th>策略</Th>
          <Th>数量</Th>
          <Th>成本</Th>
          <Th>现价</Th>
          <Th>盈亏</Th>
          <Th>仓位</Th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => {
          const pl = parseFloat(p.unrealized_pl || "0");
          const owner = p.strategy_name || p.strategy_id || "未归属";
          return (
            <Tr key={p.symbol}>
              <Td>
                <span className="font-semibold mono">{p.symbol}</span>
              </Td>
              <Td>
                <span className="text-xs text-[var(--muted)] truncate max-w-[140px] inline-block" title={owner}>
                  {shortStrategy(owner)}
                </span>
              </Td>
              <Td className="mono">{fmtQty(p.qty)}</Td>
              <Td className="mono">${parseFloat(p.avg_entry_price || "0").toFixed(2)}</Td>
              <Td className="mono">${parseFloat(p.current_price || "0").toFixed(2)}</Td>
              <Td>
                <span className="mono" style={{ color: pnlColor(pl) }}>
                  {fmtMoney(pl)}
                </span>
              </Td>
              <Td className="mono text-[var(--muted)]">{(p.weight_pct || 0).toFixed(1)}%</Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}

/* ─── Orders Table ─── */
function OrdersTable({ orders }: { orders: Order[] }) {
  if (orders.length === 0) {
    return <div className="py-8 text-center text-sm text-[var(--muted)]">暂无订单</div>;
  }

  return (
    <Table>
      <thead>
        <tr>
          <Th>标的</Th>
          <Th>方向</Th>
          <Th>数量</Th>
          <Th>状态</Th>
          <Th>时间</Th>
        </tr>
      </thead>
      <tbody>
        {orders.slice(0, 10).map((o) => (
          <Tr key={o.id}>
            <Td>
              <span className="font-medium mono">{o.symbol}</span>
            </Td>
            <Td>
              <Badge variant={normKey(o.side) === "buy" ? "success" : "danger"}>
                {normKey(o.side) === "buy" ? (
                  <ArrowUpRight className="h-3 w-3" />
                ) : (
                  <ArrowDownRight className="h-3 w-3" />
                )}
                {sideLabel(o.side)}
              </Badge>
            </Td>
            <Td className="mono">{fmtQty(o.qty)}</Td>
            <Td>
              <Badge
                variant={
                  normKey(o.status) === "filled"
                    ? "success"
                    : ["canceled","cancelled","rejected"].includes(normKey(o.status))
                    ? "danger"
                    : "default"
                }
              >
                {statusLabel(o.status)}
              </Badge>
            </Td>
            <Td className="text-xs text-[var(--muted)]">
              {timeAgo(o.submitted_at || o.created_at || "")}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

/* ─── Risk Overview ─── */
function RiskOverview({
  summary,
  positions,
}: {
  summary: DashboardData["summary"];
  positions: Position[];
}) {
  const maxPosition = positions.reduce(
    (max, p) => Math.max(max, p.weight_pct || 0),
    0
  );
  const totalPnl = positions.reduce(
    (sum, p) => sum + parseFloat(p.unrealized_pl || "0"),
    0
  );
  const losers = positions.filter((p) => parseFloat(p.unrealized_pl || "0") < 0);
  const winners = positions.filter((p) => parseFloat(p.unrealized_pl || "0") > 0);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <div className="p-3 rounded-lg bg-[var(--panel-2)]/50 border border-[var(--border)]/30">
          <div className="text-xs text-[var(--muted)] mb-1">最大单仓</div>
          <div className="text-lg mono" style={{ color: maxPosition > 30 ? "var(--amber)" : "var(--text)" }}>
            {Number(maxPosition || 0).toFixed(1)}%
          </div>
          <ProgressBar value={maxPosition} max={50} color={maxPosition > 30 ? "amber" : "teal"} showLabel />
        </div>
        <div className="p-3 rounded-lg bg-[var(--panel-2)]/50 border border-[var(--border)]/30">
          <div className="text-xs text-[var(--muted)] mb-1">投资比例</div>
          <div className="text-lg mono" style={{ color: summary.invested_pct > 80 ? "var(--amber)" : "var(--text)" }}>
            {Number(summary.invested_pct ?? 0).toFixed(1)}%
          </div>
          <ProgressBar value={summary.invested_pct} max={100} color={summary.invested_pct > 80 ? "amber" : "green"} showLabel />
        </div>
      </div>
      <div className="grid grid-cols-3 gap-3 text-center">
        <div>
          <div className="text-xs text-[var(--muted)]">盈利</div>
          <div className="text-lg mono text-[var(--green)]">{winners.length}</div>
        </div>
        <div>
          <div className="text-xs text-[var(--muted)]">亏损</div>
          <div className="text-lg mono text-[var(--red)]">{losers.length}</div>
        </div>
        <div>
          <div className="text-xs text-[var(--muted)]">总未实现</div>
          <div className="text-lg mono" style={{ color: pnlColor(totalPnl) }}>
            {fmtMoney(totalPnl)}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─── System Health ─── */
function SystemHealthBar({ health }: { health: any }) {
  const items = [
    { label: "API 服务", ok: health?.ok ?? false },
    { label: "数据源", ok: health?.has_alpaca_keys ?? false },
    { label: "交易模式", ok: true, value: health?.mode === "paper" ? "模拟盘" : health?.mode },
    { label: "域名", ok: true, value: health?.domain },
  ];

  return (
    <div className="space-y-3">
      {items.map((item) => (
        <div
          key={item.label}
          className="flex items-center justify-between p-2.5 rounded-lg bg-[var(--panel-2)]/50 border border-[var(--border)]/30"
        >
          <div className="flex items-center gap-2">
            <StatusDot status={item.ok ? "online" : "offline"} />
            <span className="text-sm text-[var(--text)]">{item.label}</span>
          </div>
          <span className="text-xs text-[var(--muted)] mono">
            {item.value || (item.ok ? "正常" : "异常")}
          </span>
        </div>
      ))}
    </div>
  );
}
