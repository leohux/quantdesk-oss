import { useMemo } from "react";
import {
  PageHeader, Panel, Badge, MetricCard, StatusDot, Button, Table, Tr, Th, Td,
} from "../components/ui";
import {
  ShieldAlert, AlertTriangle, Lock, RefreshCw, CheckCircle, Clock3,
} from "lucide-react";
import { apiGet, usePolling, fmtMoney } from "../lib/api";

type LiveReadiness = {
  state: string;
  engine: string;
  mode: string;
  live_submission_unlocked: boolean;
  checks: { name: string; passed: boolean; detail: string }[];
  settings?: Record<string, any>;
  account?: Record<string, any> | null;
  positions?: any[];
  orders?: any[];
};

export function LivePage() {
  const { data, loading, error, refetch } = usePolling(
    (signal) => apiGet<LiveReadiness>("/api/live/readiness", signal),
    15000
  );
  const { data: risk } = usePolling(
    (signal) => apiGet<any>("/api/live/risk", signal),
    15000
  );
  const { data: audit } = usePolling(
    (signal) => apiGet<any[]>("/api/live/audit?limit=20", signal),
    20000
  );

  const stateTone = useMemo(() => {
    const s = data?.state || "LOCKED";
    if (s === "LIVE_ARMED") return "danger";
    if (s === "PAPER_CONNECTED" || s === "SHADOW_READY") return "info";
    return "warning";
  }, [data?.state]);

  const passed = data?.checks?.filter((x) => x.passed).length || 0;
  const total = data?.checks?.length || 0;

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="实盘交易"
        subtitle="IBKR Live Readiness"
        right={
          <div className="flex items-center gap-2">
            <Badge variant={stateTone as any}>
              <Lock className="h-3 w-3" />
              {data?.state || "LOCKED"}
            </Badge>
            <Button variant="ghost" size="sm" onClick={refetch}>
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
        }
      />

      <div className="p-6 space-y-6">
        <div className="flex items-start gap-4 p-5 rounded-xl bg-[var(--amber)]/5 border border-[var(--amber)]/20">
          <AlertTriangle className="h-6 w-6 text-[var(--amber)] shrink-0 mt-0.5" />
          <div>
            <h3 className="text-sm font-semibold text-[var(--amber)]">默认硬锁，Fail Closed</h3>
            <p className="text-sm text-[var(--muted)] mt-1 leading-relaxed">
              本页只读展示 IBKR Live readiness。即使连上 Gateway，也不会因为前端操作直接开启实盘；
              真正下单仍需后端多重门禁同时解锁。
            </p>
            {error && <p className="text-xs text-[var(--red)] mt-2">加载异常: {error.message}</p>}
          </div>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          <MetricCard label="状态" value={data?.state || "LOCKED"} tone="amber" icon={<ShieldAlert className="h-6 w-6" />} />
          <MetricCard label="执行引擎" value={data?.engine || "ibkr"} hint={data?.mode || "paper"} icon={<Clock3 className="h-6 w-6" />} />
          <MetricCard label="检查项" value={`${passed}/${total}`} hint="readiness checks" icon={<CheckCircle className="h-6 w-6" />} tone={passed === total && total > 0 ? "pos" : "teal"} />
          <MetricCard label="账户权益" value={fmtMoney(Number(data?.account?.equity || 0))} icon={<ShieldAlert className="h-6 w-6" />} />
          <MetricCard label="持仓数" value={String(data?.positions?.length || 0)} hint={`${data?.orders?.length || 0} open orders`} />
          <MetricCard label="提交锁" value={data?.live_submission_unlocked ? "UNLOCKED" : "LOCKED"} tone={data?.live_submission_unlocked ? "neg" : "pos"} />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Panel title="Readiness Checks" subtitle="所有 broker 写操作都走这组门禁">
            <div className="space-y-3">
              {(data?.checks || []).map((item) => (
                <div key={item.name} className="flex items-start gap-3 p-3 rounded-lg bg-[var(--panel-2)]/50 border border-[var(--border)]/30">
                  <StatusDot status={item.passed ? "online" : "warning"} />
                  <div className="min-w-0">
                    <div className="text-sm text-[var(--text)]">{item.name}</div>
                    <div className="text-xs text-[var(--muted)] break-all">{item.detail}</div>
                  </div>
                </div>
              ))}
              {!loading && (!data?.checks || data.checks.length === 0) && (
                <div className="text-sm text-[var(--muted)]">暂无检查项</div>
              )}
            </div>
          </Panel>

          <Panel title="Guard Limits" subtitle="Live risk guard / kill switch">
            <Table>
              <thead>
                <Tr>
                  <Th>项</Th>
                  <Th>值</Th>
                </Tr>
              </thead>
              <tbody>
                {Object.entries(risk?.limits || {}).map(([k, v]) => (
                  <Tr key={k}>
                    <Td>{k}</Td>
                    <Td className="mono">{String(v)}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </Panel>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Panel title="Broker Positions" subtitle="IBKR / mock 只读">
            <Table>
              <thead>
                <Tr>
                  <Th>Symbol</Th>
                  <Th>Qty</Th>
                  <Th>MV</Th>
                  <Th>Weight</Th>
                </Tr>
              </thead>
              <tbody>
                {(data?.positions || []).map((p: any) => (
                  <Tr key={p.symbol}>
                    <Td>{p.symbol}</Td>
                    <Td className="mono">{p.qty}</Td>
                    <Td className="mono">{fmtMoney(Number(p.market_value || 0))}</Td>
                    <Td className="mono">{Number(p.weight_pct || 0).toFixed(2)}%</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </Panel>

          <Panel title="Open Orders" subtitle="reconciliation input">
            <Table>
              <thead>
                <Tr>
                  <Th>ID</Th>
                  <Th>Symbol</Th>
                  <Th>Side</Th>
                  <Th>Status</Th>
                </Tr>
              </thead>
              <tbody>
                {(data?.orders || []).map((o: any) => (
                  <Tr key={o.id}>
                    <Td className="mono">{String(o.id).slice(0, 12)}</Td>
                    <Td>{o.symbol}</Td>
                    <Td>{o.side}</Td>
                    <Td>{o.status}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </Panel>
        </div>

        <Panel title="Audit Trail" subtitle="preview / reject / submit / reconcile">
          <div className="space-y-2">
            {(audit || []).map((row: any, idx) => (
              <div key={idx} className="rounded-lg border border-[var(--border)]/30 bg-[var(--panel-2)]/40 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-sm text-[var(--text)]">{row.event}</div>
                  <div className="text-xs text-[var(--muted)] mono">{row.timestamp}</div>
                </div>
                <pre className="mt-2 text-xs text-[var(--muted)] whitespace-pre-wrap break-all">{JSON.stringify(row, null, 2)}</pre>
              </div>
            ))}
            {!loading && (!audit || audit.length === 0) && (
              <div className="text-sm text-[var(--muted)]">暂无审计事件</div>
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}
