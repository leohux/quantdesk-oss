import { useCallback, useState } from "react";
import {
  PageHeader, Panel, Table, Th, Td, Tr, Badge, Button, Tabs, LoadingOverlay,
} from "../components/ui";
import {
  apiGet, usePolling, fmtMoney, fmtPct, fmtQty, journalPnl, pnlColor, timeAgo,
  type JournalResponse,
} from "../lib/api";
import { BookOpen, RefreshCw } from "lucide-react";

const tabs = [
  { key: "all", label: "全部" },
  { key: "open", label: "持仓中" },
  { key: "closed", label: "已平仓" },
  { key: "stale", label: "陈旧" },
  { key: "audit", label: "审计杂项" },
];

function statusLabel(status: string): string {
  switch (status) {
    case "closed":
      return "已平";
    case "open":
      return "持仓";
    case "stale":
      return "陈旧";
    case "signal_noise":
      return "信号噪声";
    case "superseded":
      return "已替换";
    default:
      return status;
  }
}

function statusVariant(status: string): "success" | "info" | "default" | "warning" {
  if (status === "closed") return "success";
  if (status === "open") return "info";
  if (status === "stale") return "warning";
  return "default";
}

export function JournalPage() {
  const [filter, setFilter] = useState("all");
  const isAudit = filter === "audit";
  const fetchJournal = useCallback(
    (signal: AbortSignal) =>
      apiGet<JournalResponse>(
        `/api/journal?limit=200${filter !== "all" ? `&status=${filter}` : ""}`,
        signal
      ),
    [filter]
  );
  const { data, loading, error, refetch } = usePolling(fetchJournal, 15000, [filter]);

  if (loading && !data) return <LoadingOverlay message="加载交易日记..." />;

  const trades = data?.trades || [];
  // Same SQL total as Dashboard summary.realized_pnl (not capped by list limit).
  const realizedSum = Number(data?.realized_pnl ?? 0);
  const todayRealized = Number(data?.today_realized_pnl ?? 0);

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="交易日记"
        subtitle={
          isAudit
            ? "信号噪声 / 已替换行（非成交，不计入盈亏）"
            : "开平仓流水与已实现盈亏"
        }
        right={
          <div className="flex items-center gap-2">
            {!isAudit && (
              <span className="text-xs text-[var(--muted)]">
                已实现合计{" "}
                <span className="mono" style={{ color: pnlColor(realizedSum) }}>
                  {fmtMoney(realizedSum)}
                </span>
                {todayRealized !== 0 && (
                  <span className="ml-2">
                    今日{" "}
                    <span className="mono" style={{ color: pnlColor(todayRealized) }}>
                      {fmtMoney(todayRealized)}
                    </span>
                  </span>
                )}
              </span>
            )}
            <Button variant="ghost" size="sm" onClick={refetch}>
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
        }
      />

      <div className="p-6 space-y-4">
        <Tabs tabs={tabs} active={filter} onChange={setFilter} />

        <Panel
          title={isAudit ? "审计杂项" : "流水"}
          subtitle={
            isAudit
              ? `${trades.length} 条 · 不计入已实现盈亏`
              : `${trades.length} 条`
          }
          right={
            <Badge variant="default">
              <BookOpen className="h-3 w-3" /> journal
            </Badge>
          }
        >
          {error && !data ? (
            <div className="py-8 text-center text-sm text-[var(--red)]">加载失败: {error.message}</div>
          ) : trades.length === 0 ? (
            <div className="py-8 text-center text-sm text-[var(--muted)]">
              {isAudit ? "暂无审计杂项" : "暂无记录"}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <thead>
                  <tr>
                    <Th>标的</Th>
                    <Th>策略</Th>
                    <Th>状态</Th>
                    <Th>数量</Th>
                    <Th>{isAudit ? "信号价" : "入场"}</Th>
                    <Th>{isAudit ? "备注" : "出场"}</Th>
                    {!isAudit && (
                      <>
                        <Th>盈亏</Th>
                        <Th>收益</Th>
                        <Th>持有</Th>
                      </>
                    )}
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
                        <span className="text-xs text-[var(--muted)]" title={t.strategy_name}>
                          {t.strategy_name.length > 24
                            ? t.strategy_name.slice(0, 22) + "…"
                            : t.strategy_name}
                        </span>
                      </Td>
                      <Td>
                        <Badge variant={statusVariant(t.status)}>
                          {statusLabel(t.status)}
                        </Badge>
                      </Td>
                      <Td className="mono">{fmtQty(t.qty)}</Td>
                      <Td className="mono">
                        {t.entry_price != null ? `$${t.entry_price.toFixed(2)}` : "-"}
                      </Td>
                      {isAudit ? (
                        <Td className="text-xs text-[var(--muted)] max-w-[280px] truncate" title={t.signal_reason}>
                          {t.signal_reason || "-"}
                        </Td>
                      ) : (
                        <Td className="mono">
                          {markOrExit != null ? `$${markOrExit.toFixed(2)}` : "-"}
                        </Td>
                      )}
                      {!isAudit && (
                        <>
                          <Td className="mono" style={{ color: pnlColor(pnl) }}>
                            {pnl != null ? fmtMoney(pnl) : "-"}
                          </Td>
                          <Td className="mono" style={{ color: pnlColor(t.return_pct) }}>
                            {t.return_pct != null ? fmtPct(t.return_pct) : "-"}
                          </Td>
                          <Td className="mono text-[var(--muted)]">
                            {t.holding_days != null ? `${t.holding_days}d` : "-"}
                          </Td>
                        </>
                      )}
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
        </Panel>
      </div>
    </div>
  );
}
