import { useState, useCallback } from "react";
import {
  PageHeader,
  Panel,
  MetricCard,
  Table,
  Th,
  Td,
  Tr,
  Badge,
  Button,
  Input,
  Tabs,
  LoadingOverlay,
  StatusDot,
  EmptyState,
  useToast,
  MetricCardSkeleton,
} from "../components/ui";
import {
  api,
  apiGet,
  apiDelete,
  usePolling,
  fmtMoney,
  fmtPct,
  fmtQty,
  sideLabel, normKey,
  statusLabel,
  pnlColor,
  timeAgo,
  Account,
  Position,
  Order,
  EquityPoint,
} from "../lib/api";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import {
  DollarSign,
  TrendingUp,
  TrendingDown,
  ArrowUpRight,
  ArrowDownRight,
  Briefcase,
  Clock,
  Send,
  RefreshCw,
  AlertCircle,
} from "lucide-react";

/* ─── Helpers ─────────────────────────────────────── */
const statusBadgeVariant = (s: string) => {
  const key = normKey(s);
  if (["filled", "done_for_day"].includes(key)) return "success" as const;
  if (["canceled", "cancelled", "expired", "rejected"].includes(key)) return "danger" as const;
  if (["new", "accepted", "pending_new", "partially_filled", "open", "active"].includes(key))
    return "info" as const;
  return "default" as const;
};

const orderFilterTabs = [
  { key: "all", label: "全部" },
  { key: "open", label: "挂单" },
  { key: "closed", label: "已完结" },
];

/* ═══════════════════════════════════════════════════════
   PAPER TRADING PAGE
   ═══════════════════════════════════════════════════════ */
export function PaperPage() {
  const { addToast } = useToast();

  /* ── Tabs & form state ──────────────────────────── */
  const [activeTab, setActiveTab] = useState("positions");
  const [orderFilter, setOrderFilter] = useState("all");
  const [symbol, setSymbol] = useState("");
  const [qty, setQty] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [submitting, setSubmitting] = useState(false);
  const [closingSymbol, setClosingSymbol] = useState<string | null>(null);

  /* ── Data fetching (auto-refresh) ───────────────── */
  const account = usePolling<Account>(
    useCallback((sig) => apiGet<Account>("/api/account", sig), []),
    5000,
  );
  const positions = usePolling<Position[]>(
    useCallback((sig) => apiGet<Position[]>("/api/positions", sig), []),
    5000,
  );
  const orders = usePolling<Order[]>(
    useCallback((sig) => apiGet<Order[]>("/api/orders?status=all&limit=100", sig), []),
    8000,
  );
  const curve = usePolling<{ points: EquityPoint[] }>(
    useCallback((sig) => apiGet<{ points: EquityPoint[] }>("/api/equity/curve?days=30", sig), []),
    60000,
  );
  const health = usePolling(
    useCallback((sig) => apiGet("/api/health", sig), []),
    30000,
  );

  const loading = account.loading || positions.loading;
  const acc = account.data;
  const posList = positions.data ?? [];
  const orderList = orders.data ?? [];
  const curvePoints = curve.data?.points ?? [];
  const accountError = account.error && !acc ? account.error : null;

  /* ── Derived account metrics ────────────────────── */
  const cash = parseFloat(String(acc?.cash ?? "0"));
  const buyingPower = parseFloat(String(acc?.buying_power ?? "0"));
  const equity = parseFloat(String(acc?.equity ?? "0"));
  const lastEquityRaw = acc?.last_equity;
  const lastEquity = lastEquityRaw != null && lastEquityRaw !== ""
    ? parseFloat(String(lastEquityRaw))
    : NaN;
  const hasLastEquity = Number.isFinite(lastEquity) && lastEquity > 0;
  const todayPnl = hasLastEquity ? equity - lastEquity : 0;
  const todayPnlPct = hasLastEquity ? (todayPnl / lastEquity) * 100 : 0;
  const unrealized = posList.reduce((s, p) => s + parseFloat(p.unrealized_pl || "0"), 0);
  const posCount = posList.length;

  /* ── Close position ─────────────────────────────── */
  const closePosition = async (sym: string) => {
    setClosingSymbol(sym);
    try {
      await apiDelete(`/api/positions/${encodeURIComponent(sym)}`);
      addToast({ type: "success", message: `已平仓 ${sym}` });
      positions.refetch();
      account.refetch();
    } catch (e: any) {
      addToast({ type: "error", message: `平仓失败: ${e.message || e}` });
    } finally {
      setClosingSymbol(null);
    }
  };

  /* ── Place order ────────────────────────────────── */
  const placeOrder = async () => {
    if (!symbol.trim()) {
      addToast({ type: "warning", message: "请输入标的代码" });
      return;
    }
    const q = parseInt(qty, 10);
    if (!q || q <= 0) {
      addToast({ type: "warning", message: "请输入有效数量" });
      return;
    }
    setSubmitting(true);
    try {
      await api.placeOrder({ symbol: symbol.trim().toUpperCase(), qty: q, side });
      addToast({ type: "success", message: `已提交 ${sideLabel(side)} ${symbol.toUpperCase()} × ${q}` });
      setSymbol("");
      setQty("");
      orders.refetch();
      positions.refetch();
      account.refetch();
    } catch (e: any) {
      addToast({ type: "error", message: `下单失败: ${e.message || e}` });
    } finally {
      setSubmitting(false);
    }
  };

  /* ── Filtered orders ────────────────────────────── */
  const filteredOrders = orderList.filter((o) => {
    const st = normKey(o.status);
    if (orderFilter === "open") return ["new", "accepted", "pending_new", "partially_filled", "open", "active"].includes(st);
    if (orderFilter === "closed") return ["filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"].includes(st);
    return true;
  });

  /* ── Render ─────────────────────────────────────── */
  return (
    <div>
      {/* Header */}
      <PageHeader
        title="模拟交易"
        subtitle="Paper Trading"
        right={
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1.5">
              <StatusDot status={health.data?.ok ? "online" : "offline"} />
              <Badge variant={health.data?.ok ? "success" : "danger"}>
                {health.data?.mode === "paper" ? "模拟盘" : health.data?.mode || "…"}
              </Badge>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                account.refetch();
                positions.refetch();
                orders.refetch();
              }}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              刷新
            </Button>
          </div>
        }
      />

      <div className="p-6 space-y-5">
        {accountError && (
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 rounded-xl border border-[var(--red)]/30 bg-[var(--red)]/5">
            <span className="text-sm text-[var(--red)]">
              账户加载失败: {accountError.message}
            </span>
            <Button variant="secondary" size="sm" onClick={() => account.refetch()}>
              重试
            </Button>
          </div>
        )}

        {/* Metric cards */}
        {loading ? (
          <div className="grid grid-cols-2 xl:grid-cols-5 gap-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <MetricCardSkeleton key={i} />
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-2 xl:grid-cols-5 gap-3">
            <MetricCard
              label="账户权益"
              value={fmtMoney(equity)}
              hint={`现金 ${fmtMoney(cash)}`}
              icon={<DollarSign className="h-5 w-5" />}
              tone="teal"
            />
            <MetricCard
              label="购买力"
              value={fmtMoney(buyingPower)}
              icon={<Briefcase className="h-5 w-5" />}
            />
            <MetricCard
              label="今日盈亏"
              value={hasLastEquity ? fmtMoney(todayPnl) : "?"}
              hint={hasLastEquity ? fmtPct(todayPnlPct) : "等待日切"}
              icon={todayPnl >= 0 ? <TrendingUp className="h-5 w-5" /> : <TrendingDown className="h-5 w-5" />}
              tone={hasLastEquity ? (todayPnl >= 0 ? "pos" : "neg") : "neutral"}
              trend={hasLastEquity ? { value: fmtPct(todayPnlPct), direction: todayPnl > 0 ? "up" : todayPnl < 0 ? "down" : "flat" } : undefined}
            />
            <MetricCard
              label="未实现盈亏"
              value={fmtMoney(unrealized)}
              icon={unrealized >= 0 ? <TrendingUp className="h-5 w-5" /> : <TrendingDown className="h-5 w-5" />}
              tone={unrealized >= 0 ? "pos" : "neg"}
            />
            <MetricCard
              label="持仓数量"
              value={String(posCount)}
              icon={<Briefcase className="h-5 w-5" />}
              hint={posCount > 0 ? `${posList.length} 只标的` : "暂无持仓"}
            />
          </div>
        )}

        <Panel title="Paper 权益曲线" subtitle="近 30 天">
          <PaperEquityCurve points={curvePoints} />
        </Panel>

        {/* Tab bar */}
        <Tabs
          tabs={[
            { key: "positions", label: "持仓", count: posList.length },
            { key: "orders", label: "订单", count: orderList.length },
            { key: "entry", label: "下单" },
          ]}
          active={activeTab}
          onChange={setActiveTab}
        />

        {/* ── Positions Tab ───────────────────────────── */}
        {activeTab === "positions" && (
          <Panel title="当前持仓" subtitle={`${posList.length} 只标的`}>
            {positions.loading ? (
              <LoadingOverlay message="加载持仓数据..." />
            ) : posList.length === 0 ? (
              <EmptyState
                icon={<Briefcase className="h-10 w-10" />}
                title="暂无持仓"
                description="通过下单面板开始模拟交易"
                action={
                  <Button size="sm" onClick={() => setActiveTab("entry")}>
                    <Send className="h-3.5 w-3.5" />
                    去下单
                  </Button>
                }
              />
            ) : (
              <Table>
                <thead>
                  <Tr>
                    <Th>标的</Th>
                    <Th>策略</Th>
                    <Th>数量</Th>
                    <Th>成本</Th>
                    <Th>现价</Th>
                    <Th>市值</Th>
                    <Th>权重%</Th>
                    <Th>盈亏</Th>
                    <Th>盈亏%</Th>
                    <Th>操作</Th>
                  </Tr>
                </thead>
                <tbody>
                  {posList.map((p) => {
                    const pl = parseFloat(p.unrealized_pl || "0");
                    const plpc = parseFloat(p.unrealized_plpc || "0") * 100;
                    const owner = p.strategy_name || p.strategy_id || "未归属";
                    return (
                      <Tr key={p.symbol}>
                        <Td>
                          <span className="font-semibold">{p.symbol}</span>
                          <span className="text-xs text-[var(--muted)] ml-1.5">{sideLabel(p.side)}</span>
                        </Td>
                        <Td>
                          <span className="text-xs text-[var(--muted)]" title={owner}>
                            {owner.length > 18 ? owner.slice(0, 16) + "…" : owner}
                          </span>
                        </Td>
                        <Td className="mono">{fmtQty(p.qty)}</Td>
                        <Td className="mono">{fmtMoney(p.avg_entry_price)}</Td>
                        <Td className="mono">{fmtMoney(p.current_price)}</Td>
                        <Td className="mono">{fmtMoney(p.market_value)}</Td>
                        <Td className="mono">{(p.weight_pct ?? 0).toFixed(1)}%</Td>
                        <Td className="mono" style={{ color: pnlColor(pl) }}>
                          {fmtMoney(pl)}
                        </Td>
                        <Td className="mono" style={{ color: pnlColor(plpc) }}>
                          {fmtPct(plpc)}
                        </Td>
                        <Td>
                          <Button
                            variant="danger"
                            size="sm"
                            loading={closingSymbol === p.symbol}
                            onClick={() => closePosition(p.symbol)}
                          >
                            平仓
                          </Button>
                        </Td>
                      </Tr>
                    );
                  })}
                </tbody>
              </Table>
            )}
          </Panel>
        )}

        {/* ── Orders Tab ──────────────────────────────── */}
        {activeTab === "orders" && (
          <Panel
            title="订单记录"
            subtitle={`${orderList.length} 条记录`}
            right={
              <div className="flex gap-1">
                {orderFilterTabs.map((f) => (
                  <button
                    key={f.key}
                    onClick={() => setOrderFilter(f.key)}
                    className={`px-3 py-1 text-xs rounded-lg font-medium transition-colors cursor-pointer ${
                      orderFilter === f.key
                        ? "bg-[var(--teal)]/15 text-[var(--teal)] border border-[var(--teal)]/30"
                        : "text-[var(--muted)] hover:text-[var(--text)] border border-transparent"
                    }`}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            }
          >
            {orders.loading ? (
              <LoadingOverlay message="加载订单数据..." />
            ) : filteredOrders.length === 0 ? (
              <EmptyState
                icon={<Clock className="h-10 w-10" />}
                title="暂无订单"
                description={orderFilter !== "all" ? "当前筛选条件下无订单" : "下单后订单将显示在这里"}
              />
            ) : (
              <Table>
                <thead>
                  <Tr>
                    <Th>标的</Th>
                    <Th>方向</Th>
                    <Th>数量</Th>
                    <Th>已成交</Th>
                    <Th>类型</Th>
                    <Th>状态</Th>
                    <Th>提交时间</Th>
                  </Tr>
                </thead>
                <tbody>
                  {filteredOrders.map((o) => (
                    <Tr key={o.id}>
                      <Td>
                        <span className="font-semibold">{o.symbol}</span>
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
                      <Td className="mono">{fmtQty(o.filled_qty)}</Td>
                      <Td className="text-[var(--muted)]">{o.type || "market"}</Td>
                      <Td>
                        <Badge variant={statusBadgeVariant(o.status)}>
                          {statusLabel(o.status)}
                        </Badge>
                      </Td>
                      <Td className="text-[var(--muted)] text-xs">
                        {timeAgo(o.submitted_at || o.created_at || "")}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Panel>
        )}

        {/* ── Order Entry Tab ─────────────────────────── */}
        {activeTab === "entry" && (
          <Panel title="手动下单" subtitle="Paper Order Entry">
            <div className="max-w-md space-y-4">
              <Input
                label="标的代码"
                placeholder="e.g. AAPL, TSLA, NVDA"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              />

              <Input
                label="数量"
                type="number"
                min={1}
                placeholder="100"
                value={qty}
                onChange={(e) => setQty(e.target.value)}
              />

              <div className="space-y-1">
                <label className="text-xs text-[var(--muted)] font-medium">方向</label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    onClick={() => setSide("buy")}
                    className={`h-10 rounded-lg font-semibold text-sm transition-all cursor-pointer ${
                      side === "buy"
                        ? "bg-[var(--green)] text-black shadow-lg shadow-[var(--green)]/20"
                        : "bg-[var(--panel-2)] border border-[var(--border)] text-[var(--muted)] hover:text-[var(--green)]"
                    }`}
                  >
                    <ArrowUpRight className="h-4 w-4 inline mr-1" />
                    买入 BUY
                  </button>
                  <button
                    onClick={() => setSide("sell")}
                    className={`h-10 rounded-lg font-semibold text-sm transition-all cursor-pointer ${
                      side === "sell"
                        ? "bg-[var(--red)] text-white shadow-lg shadow-[var(--red)]/20"
                        : "bg-[var(--panel-2)] border border-[var(--border)] text-[var(--muted)] hover:text-[var(--red)]"
                    }`}
                  >
                    <ArrowDownRight className="h-4 w-4 inline mr-1" />
                    卖出 SELL
                  </button>
                </div>
              </div>

              <div className="pt-2">
                <Button
                  loading={submitting}
                  onClick={placeOrder}
                  className="w-full"
                  size="lg"
                >
                  <Send className="h-4 w-4" />
                  {side === "buy" ? "买入" : "卖出"} {symbol || "—"}
                  {qty ? ` × ${qty}` : ""}
                </Button>
              </div>

              <div className="flex items-start gap-2 p-3 rounded-lg bg-[var(--amber)]/5 border border-[var(--amber)]/20 text-xs text-[var(--amber)]">
                <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
                <span>模拟交易使用虚拟资金，不影响真实账户。订单将使用市价单类型执行。</span>
              </div>
            </div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function PaperEquityCurve({ points }: { points: EquityPoint[] }) {
  if (points.length < 2) {
    return (
      <div className="h-[180px] flex items-center justify-center text-sm text-[var(--muted)]">
        权益快照积累中（约每 5 分钟一点）
      </div>
    );
  }
  const data = points.map((p) => ({
    t: p.ts
      ? new Date(p.ts).toLocaleString("zh-CN", {
          month: "numeric",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "",
    equity: p.equity,
  }));
  const min = Math.min(...data.map((d) => d.equity));
  const max = Math.max(...data.map((d) => d.equity));
  const pad = (max - min) * 0.08 || 1;
  return (
    <div className="h-[180px]">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <defs>
            <linearGradient id="paperEqFill" x1="0" y1="0" x2="0" y2="1">
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
            contentStyle={{
              background: "var(--panel)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              fontSize: 12,
            }}
            formatter={(value: number) => [fmtMoney(value), "权益"]}
          />
          <Area
            type="monotone"
            dataKey="equity"
            stroke="var(--teal)"
            fill="url(#paperEqFill)"
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
