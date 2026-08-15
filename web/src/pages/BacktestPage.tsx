import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  FlaskConical,
  Play,
  Download,
  TrendingUp,
  BarChart3,
  Target,
  Square,
} from "lucide-react";
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
  Select,
  DatePicker,
  LoadingOverlay,
  EmptyState,
  useToast,
} from "../components/ui";
import { api, useApiData, BacktestResult, Strategy, fmtMoney, fmtPct } from "../lib/api";

const chartTooltipProps = {
  contentStyle: {
    background: "#0e141c",
    border: "1px solid #1e2a38",
    borderRadius: 8,
    fontSize: 12,
    color: "#e8eef6",
  },
  labelStyle: { color: "#e8eef6" },
  itemStyle: { color: "#e8eef6" },
};

function todayISO() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function yearsAgoISO(n: number) {
  const d = new Date();
  d.setFullYear(d.getFullYear() - n);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function yearStartISO() {
  return `${new Date().getFullYear()}-01-01`;
}

const RANGE_PRESETS: {
  key: string;
  label: string;
  apply: (setStart: (v: string) => void, setEnd: (v: string) => void) => void;
}[] = [
  { key: "1y", label: "1年", apply: (s, e) => { s(yearsAgoISO(1)); e(""); } },
  { key: "3y", label: "3年", apply: (s, e) => { s(yearsAgoISO(3)); e(""); } },
  { key: "5y", label: "5年", apply: (s, e) => { s(yearsAgoISO(5)); e(""); } },
  { key: "ytd", label: "今年", apply: (s, e) => { s(yearStartISO()); e(""); } },
  { key: "now", label: "至今", apply: (_s, e) => { e(""); } },
];

function isAbortError(e: unknown) {
  return (
    (e instanceof DOMException && e.name === "AbortError") ||
    (e instanceof Error && e.name === "AbortError")
  );
}

export function BacktestPage() {
  const [params] = useSearchParams();
  const { addToast } = useToast();
  const abortRef = useRef<AbortController | null>(null);

  const { data: strategiesRaw } = useApiData<Strategy[]>(
    (signal) => api.strategies("manual"),
  );
  const strategies = strategiesRaw || [];

  const [strategyId, setStrategyId] = useState(params.get("strategy") || "");
  const [symbolText, setSymbolText] = useState("AAPL");
  const [start, setStart] = useState("2020-01-01");
  const [end, setEnd] = useState("");
  const [initCash, setInitCash] = useState(100000);
  const [commission, setCommission] = useState(0.05);
  const [slippage, setSlippage] = useState(2);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  /* Auto-select strategy from URL or first available */
  useEffect(() => {
    if (!strategies.length) return;
    const id = params.get("strategy") || strategies[0].id;
    if (!strategyId || !strategies.find((s) => s.id === strategyId)) {
      setStrategyId(id);
    }
  }, [strategies, params, strategyId]);

  const selected = strategies.find((s) => s.id === strategyId);

  /* Update symbol input when strategy changes (if strategy has symbols param) */
  useEffect(() => {
    if (selected?.params?.symbols && Array.isArray(selected.params.symbols)) {
      setSymbolText((selected.params.symbols as string[]).join(", "));
    }
  }, [selected]);

  const symbols = useMemo(
    () =>
      symbolText
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
    [symbolText],
  );

  const applyPreset = (key: string) => {
    const preset = RANGE_PRESETS.find((p) => p.key === key);
    if (!preset) return;
    preset.apply(setStart, setEnd);
  };

  const cancel = () => {
    abortRef.current?.abort();
  };

  const run = async () => {
    if (!strategyId) {
      addToast({ type: "warning", message: "请选择策略" });
      return;
    }
    if (!symbols.length) {
      addToast({ type: "warning", message: "请至少输入一个标的" });
      return;
    }
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setLoading(true);
    try {
      const data = await api.backtest(
        {
          strategy_id: strategyId,
          symbols,
          start,
          end: end || null,
          init_cash: initCash,
          fees: commission / 100,
          slippage_bps: slippage,
        },
        ac.signal,
      );
      setResult(data);
      addToast({ type: "success", message: "回测完成" });
    } catch (e) {
      if (isAbortError(e) || ac.signal.aborted) {
        addToast({ type: "info", message: "已取消回测" });
      } else {
        addToast({
          type: "error",
          message: `回测失败: ${(e as Error).message || e}`,
        });
      }
    } finally {
      if (abortRef.current === ac) abortRef.current = null;
      setLoading(false);
    }
  };

  const annual = useMemo(() => result?.annual_returns || [], [result]);
  const annualDomain = useMemo((): [number, number] => {
    if (!annual.length) return [-5, 5];
    const vals = annual.map((a) => a.return_pct);
    const lo = Math.min(0, ...vals);
    const hi = Math.max(0, ...vals);
    const span = Math.max(hi - lo, 1);
    const pad = span * 0.15;
    return [lo === 0 ? 0 : lo - pad, hi === 0 ? 0 : hi + pad];
  }, [annual]);

  const cancelBtn = (
    <Button variant="secondary" onClick={cancel} className="gap-1.5">
      <Square className="h-3.5 w-3.5 fill-current" />
      取消
    </Button>
  );

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <PageHeader
        title="回测中心"
        subtitle="选择策略与标的，运行回测并查看绩效分析"
        right={
          loading ? (
            cancelBtn
          ) : (
            <Button variant="primary" onClick={run} className="gap-1.5">
              <Play className="h-3.5 w-3.5" />
              运行回测
            </Button>
          )
        }
      />

      <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
        {/* ── Left: Configuration Panel ── */}
        <div className="w-full md:w-[350px] shrink-0 min-w-0 border-b md:border-b-0 md:border-r border-[var(--border)] overflow-x-hidden overflow-y-auto p-5 space-y-5 max-h-[50vh] md:max-h-none">
          <Panel title="回测配置" className="min-w-0 overflow-hidden">
            <div className="space-y-4 min-w-0">
              <Select
                label="策略"
                value={strategyId}
                onChange={(e) => setStrategyId(e.target.value)}
              >
                {strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </Select>
              {selected?.description && (
                <p className="text-xs text-[var(--muted)] -mt-2">
                  {selected.description}
                </p>
              )}

              <Input
                label="标的 (逗号分隔)"
                value={symbolText}
                onChange={(e) => setSymbolText(e.target.value)}
                placeholder="AAPL, MSFT, NVDA"
              />

              <div className="space-y-2">
                <label className="text-xs text-[var(--muted)] font-medium">回测区间</label>
                <div className="flex items-center gap-2 min-w-0">
                  <DatePicker
                    value={start}
                    onChange={(v) => {
                      setStart(v);
                      if (end && v && end < v) setEnd("");
                    }}
                    max={end || todayISO()}
                    placeholder="开始日期"
                    className="flex-1"
                  />
                  <span className="text-xs text-[var(--muted)] shrink-0">至</span>
                  <DatePicker
                    value={end}
                    onChange={setEnd}
                    min={start || undefined}
                    max={todayISO()}
                    placeholder="至今"
                    clearable
                    clearLabel="至今"
                    className="flex-1"
                  />
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {RANGE_PRESETS.map((p) => (
                    <button
                      key={p.key}
                      type="button"
                      onClick={() => applyPreset(p.key)}
                      className="h-7 px-2.5 rounded-md text-xs border border-[var(--border)] bg-[var(--panel-2)] text-[var(--muted)] hover:text-[var(--text)] hover:border-[var(--teal)]/40 transition-colors cursor-pointer"
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <Input
                  label="初始资金 ($)"
                  type="number"
                  value={initCash}
                  onChange={(e) => setInitCash(Number(e.target.value))}
                />
                <Input
                  label="佣金 (%)"
                  type="number"
                  step="0.001"
                  value={commission}
                  onChange={(e) => setCommission(Number(e.target.value))}
                />
              </div>

              <Input
                label="滑点 (bps)"
                type="number"
                value={slippage}
                onChange={(e) => setSlippage(Number(e.target.value))}
              />

              {loading ? (
                <Button variant="secondary" size="lg" onClick={cancel} className="w-full">
                  <Square className="h-4 w-4 fill-current" />
                  取消回测
                </Button>
              ) : (
                <Button variant="primary" size="lg" onClick={run} className="w-full">
                  <Play className="h-4 w-4" />
                  运行回测
                </Button>
              )}
            </div>
          </Panel>

          {/* Strategy code preview */}
          {selected?.code && (
            <Panel title="策略代码">
              <textarea
                readOnly
                value={selected.code}
                className="w-full h-40 p-3 rounded-lg bg-[var(--panel-2)] border border-[var(--border)] text-xs mono text-[var(--muted)] resize-none focus:outline-none"
              />
            </Panel>
          )}
        </div>

        {/* ── Right: Results ── */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {loading && !result && (
            <LoadingOverlay message="正在运行回测…" action={cancelBtn} />
          )}

          {!loading && !result && (
            <EmptyState
              icon={<FlaskConical className="h-12 w-12" />}
              title="选择策略并运行回测"
              description="在左侧配置策略参数，点击运行回测查看结果"
            />
          )}

          {result && (
            <>
              {/* ── Metric Cards ── */}
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                <MetricCard
                  label="夏普比率"
                  value={result.sharpe?.toFixed(2) ?? "—"}
                  tone="teal"
                  icon={<TrendingUp className="h-5 w-5" />}
                />
                <MetricCard
                  label="索提诺比率"
                  value={result.sortino?.toFixed(2) ?? "—"}
                  icon={<Target className="h-5 w-5" />}
                />
                <MetricCard
                  label="卡玛比率"
                  value={result.calmar?.toFixed(2) ?? "—"}
                  icon={<BarChart3 className="h-5 w-5" />}
                />
                <MetricCard
                  label="胜率"
                  value={fmtPct(result.win_rate_pct ?? null)}
                  icon={<Target className="h-5 w-5" />}
                />
                <MetricCard
                  label="总收益"
                  value={fmtPct(result.total_return_pct ?? null)}
                  tone={(result.total_return_pct ?? 0) >= 0 ? "pos" : "neg"}
                  icon={<TrendingUp className="h-5 w-5" />}
                />
                <MetricCard
                  label="最大回撤"
                  value={fmtPct(
                    result ? -Math.abs(result.max_drawdown_pct ?? 0) : null,
                  )}
                  tone="neg"
                  icon={<BarChart3 className="h-5 w-5" />}
                />
              </div>

              {/* ── Benchmark comparison ── */}
              {result.buy_hold_return_pct != null && (
                <Panel title="基准对比">
                  <div className="flex items-center gap-6">
                    <div className="flex items-center gap-2">
                      <span
                        className="inline-block w-3 h-3 rounded-full"
                        style={{ background: "#2dd4bf" }}
                      />
                      <span className="text-sm text-[var(--muted)]">
                        策略收益
                      </span>
                      <span className="mono text-sm font-semibold text-[var(--green)]">
                        {fmtPct(result.total_return_pct ?? null)}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span
                        className="inline-block w-3 h-3 rounded-full"
                        style={{ background: "#8b9bb0" }}
                      />
                      <span className="text-sm text-[var(--muted)]">
                        买入持有
                      </span>
                      <span className="mono text-sm font-semibold text-[var(--text)]">
                        {fmtPct(result.buy_hold_return_pct)}
                      </span>
                    </div>
                    <Badge
                      variant={
                        (result.total_return_pct ?? 0) >=
                        (result.buy_hold_return_pct ?? 0)
                          ? "success"
                          : "danger"
                      }
                    >
                      {(result.total_return_pct ?? 0) >=
                      (result.buy_hold_return_pct ?? 0)
                        ? "跑赢基准"
                        : "跑输基准"}
                    </Badge>
                  </div>
                </Panel>
              )}

              {/* ── Equity Curve ── */}
              <Panel title="权益曲线">
                <div className="flex items-center justify-between mb-3">
                  <div className="text-sm text-[var(--muted)] mono">
                    期末 {fmtMoney(result.end_value ?? null)}
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      addToast({
                        type: "info",
                        message: "导出功能开发中",
                      });
                    }}
                  >
                    <Download className="h-3.5 w-3.5" />
                    导出
                  </Button>
                </div>
                <div className="h-[260px]">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={result.equity_curve || []}>
                      <defs>
                        <linearGradient id="bt" x1="0" y1="0" x2="0" y2="1">
                          <stop
                            offset="0%"
                            stopColor="#2dd4bf"
                            stopOpacity={0.35}
                          />
                          <stop
                            offset="100%"
                            stopColor="#2dd4bf"
                            stopOpacity={0}
                          />
                        </linearGradient>
                      </defs>
                      <CartesianGrid
                        stroke="#1e2a38"
                        strokeDasharray="3 3"
                      />
                      <XAxis dataKey="date" hide />
                      <YAxis
                        stroke="#8b9bb0"
                        tick={{ fontSize: 11 }}
                        domain={["auto", "auto"]}
                      />
                      <Tooltip
                        {...chartTooltipProps}
                        formatter={(value: number | string) => [
                          fmtMoney(Number(value)),
                          "权益",
                        ]}
                      />
                      <Area
                        type="monotone"
                        dataKey="equity"
                        stroke="#2dd4bf"
                        fill="url(#bt)"
                        strokeWidth={2}
                        name="策略"
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </Panel>

              {/* ── Drawdown + Annual Returns ── */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
                <Panel title="回撤曲线">
                  <div className="h-[200px]">
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={result.drawdown_curve || []}>
                        <defs>
                          <linearGradient
                            id="dd"
                            x1="0"
                            y1="0"
                            x2="0"
                            y2="1"
                          >
                            <stop
                              offset="0%"
                              stopColor="#f87171"
                              stopOpacity={0.3}
                            />
                            <stop
                              offset="100%"
                              stopColor="#f87171"
                              stopOpacity={0}
                            />
                          </linearGradient>
                        </defs>
                        <CartesianGrid
                          stroke="#1e2a38"
                          strokeDasharray="3 3"
                        />
                        <XAxis dataKey="date" hide />
                        <YAxis
                          stroke="#8b9bb0"
                          tick={{ fontSize: 11 }}
                          tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                        />
                        <Tooltip
                          {...chartTooltipProps}
                          formatter={(value: number | string) => [
                            `${Number(value).toFixed(2)}%`,
                            "回撤",
                          ]}
                        />
                        <Area
                          type="monotone"
                          dataKey="drawdown_pct"
                          stroke="#f87171"
                          fill="url(#dd)"
                          strokeWidth={2}
                          name="回撤"
                        />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                </Panel>

                <Panel title="年度收益">
                  <div className="h-[200px]">
                    {annual.length === 0 ? (
                      <div className="h-full flex items-center justify-center text-sm text-[var(--muted)]">
                        暂无年度收益数据
                      </div>
                    ) : (
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart
                          data={annual}
                          margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
                        >
                          <CartesianGrid
                            stroke="#1e2a38"
                            strokeDasharray="3 3"
                            vertical={false}
                          />
                          <XAxis
                            dataKey="year"
                            stroke="#8b9bb0"
                            tick={{ fontSize: 11, fill: "#8b9bb0" }}
                            axisLine={{ stroke: "#1e2a38" }}
                            tickLine={false}
                          />
                          <YAxis
                            stroke="#8b9bb0"
                            tick={{ fontSize: 11, fill: "#8b9bb0" }}
                            tickFormatter={(v: number) => `${v.toFixed(0)}%`}
                            domain={annualDomain}
                            axisLine={false}
                            tickLine={false}
                            width={48}
                          />
                          <ReferenceLine y={0} stroke="#8b9bb0" strokeOpacity={0.5} />
                          <Tooltip
                            {...chartTooltipProps}
                            cursor={{ fill: "rgba(255,255,255,0.04)" }}
                            formatter={(value: number | string) => [
                              `${Number(value).toFixed(2)}%`,
                              "收益",
                            ]}
                            labelFormatter={(label) => `${label} 年`}
                          />
                          <Bar
                            dataKey="return_pct"
                            name="收益"
                            maxBarSize={48}
                            radius={
                              annual.every((a) => a.return_pct < 0)
                                ? [0, 0, 3, 3]
                                : [3, 3, 0, 0]
                            }
                          >
                            {annual.map((entry, index) => (
                              <Cell
                                key={index}
                                fill={
                                  entry.return_pct >= 0 ? "#34d399" : "#f87171"
                                }
                              />
                            ))}
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    )}
                  </div>
                </Panel>
              </div>

              {/* ── Per-Symbol Breakdown ── */}
              {result.per_symbol?.length ? (
                <Panel title="分标的表现">
                  <Table>
                    <thead>
                      <tr>
                        <Th>标的</Th>
                        <Th>收益</Th>
                        <Th>夏普</Th>
                        <Th>最大回撤</Th>
                        <Th>交易次数</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.per_symbol.map((r) => (
                        <Tr key={r.symbol}>
                          <Td className="font-medium">
                            <Badge variant="info">{r.symbol}</Badge>
                          </Td>
                          <Td className="mono text-[var(--green)]">
                            {fmtPct(r.total_return_pct ?? null)}
                          </Td>
                          <Td className="mono">
                            {r.sharpe?.toFixed(2) ?? "—"}
                          </Td>
                          <Td className="mono text-[var(--red)]">
                            {fmtPct(
                              r.max_drawdown_pct != null
                                ? -Math.abs(r.max_drawdown_pct)
                                : null,
                            )}
                          </Td>
                          <Td className="mono">{r.trades ?? "—"}</Td>
                        </Tr>
                      ))}
                    </tbody>
                  </Table>
                </Panel>
              ) : null}

              {/* ── Errors ── */}
              {result.errors?.length ? (
                <Panel title="错误">
                  <div className="space-y-2">
                    {result.errors.map((e, i) => (
                      <div
                        key={i}
                        className="text-sm text-[var(--red)] bg-[var(--red)]/5 px-3 py-2 rounded-lg border border-[var(--red)]/20"
                      >
                        <span className="font-medium">{e.symbol}:</span>{" "}
                        {e.error}
                      </div>
                    ))}
                  </div>
                </Panel>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
