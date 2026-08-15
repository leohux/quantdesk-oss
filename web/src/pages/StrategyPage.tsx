import { useState, useEffect, useMemo } from "react";
import {
  PageHeader, Panel, Button, Badge, Input, Tabs, LoadingOverlay,
  EmptyState, StatusDot, useToast, MetricCard,
} from "../components/ui";
import { api, useApiData, fmtPct } from "../lib/api";
import type { Strategy, BacktestResult } from "../lib/api";
import {
  Plus, Save, Code, Settings, FlaskConical, CheckCircle,
  AlertCircle, Loader2, Trash2, Play, Pause, BarChart3,
} from "lucide-react";

const DEFAULT_CODE = `"""Custom strategy template.
Must define: generate_signals(close, params) -> (entries, exits)
entries/exits are boolean Series aligned with close.
"""
import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    fast = int(params.get("fast", 20))
    slow = int(params.get("slow", 60))
    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()
    position = (fast_ma > slow_ma).astype(int)
    entries = (position == 1) & (position.shift(1).fillna(0) == 0)
    exits = (position == 0) & (position.shift(1).fillna(0) == 1)
    return entries.fillna(False), exits.fillna(False)
`

export function StrategyPage() {
  const { addToast } = useToast();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [tab, setTab] = useState("code");
  const [showMined, setShowMined] = useState(false);

  // Editable state
  const [editCode, setEditCode] = useState("");
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editParams, setEditParams] = useState<Record<string, any>>({});
  const [editSymbols, setEditSymbols] = useState("");
  const [editEnabled, setEditEnabled] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [codeDirty, setCodeDirty] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const { data: manualStrategies, loading: manualLoading, refetch: refetchManual } = useApiData(
    () => api.strategies("manual"),
    10000
  );
  const { data: minedStrategies, loading: minedLoading, refetch: refetchMined } = useApiData(
    () => (showMined ? api.strategies("mined") : Promise.resolve([] as Strategy[])),
    0,
    [showMined]
  );

  // Backtest
  const [btSymbol, setBtSymbol] = useState("AAPL");
  const [btStart, setBtStart] = useState("2020-01-01");
  const [btCash, setBtCash] = useState("100000");
  const [btLoading, setBtLoading] = useState(false);
  const [btResult, setBtResult] = useState<BacktestResult | null>(null);

  const manualList = manualStrategies || [];
  const minedList = minedStrategies || [];
  const list = useMemo(
    () => (showMined ? [...manualList, ...minedList] : manualList),
    [manualList, minedList, showMined]
  );
  const visibleList = list;
  const loading = manualLoading || (showMined && minedLoading);
  const refetch = async () => {
    await refetchManual();
    if (showMined) await refetchMined();
  };
  const selected = useMemo(
    () => visibleList.find((s) => s.id === selectedId) || null,
    [visibleList, selectedId]
  );

  // Load full strategy detail (list endpoint omits code)
useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    setDetailLoading(true);
    (async () => {
      try {
        const detail = await api.strategy(selectedId);
        if (cancelled) return;
        setEditCode(detail.code || DEFAULT_CODE);
        setEditName(detail.name);
        setEditDesc(detail.description || "");
        setEditParams({ ...(detail.params || {}) });
        setEditSymbols(
          Array.isArray(detail.params?.symbols)
            ? detail.params.symbols.join(", ")
            : ""
        );
        setEditEnabled(detail.enabled);
        setDirty(false);
        setBtResult(null);
      } catch (err: any) {
        if (!cancelled) {
          addToast({ type: "error", message: `加载策略失败: ${err.message}` });
        }
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // Auto-select first strategy
  useEffect(() => {
    if (visibleList.length > 0 && !selectedId) {
      setSelectedId(visibleList[0].id);
    }
  }, [visibleList, selectedId]);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const result = await api.createStrategy({
        name: newName.trim(),
        type: "custom",
        code: DEFAULT_CODE,
      });
      addToast({ type: "success", message: `策略 "${newName}" 已创建` });
      setNewName("");
      setShowNew(false);
      await refetch();
      setSelectedId(result.id);
    } catch (err: any) {
      addToast({ type: "error", message: `创建失败: ${err.message}` });
    } finally {
      setCreating(false);
    }
  };

  const handleValidate = async () => {
    if (detailLoading || !editCode.trim()) {
      addToast({ type: "error", message: "代码还在加载，请稍候再验证" });
      return;
    }
    setValidating(true);
    try {
      await api.validateCode(editCode);
      addToast({ type: "success", message: "代码验证通过 ✓" });
    } catch (err: any) {
      addToast({ type: "error", message: `验证失败: ${err.message}` });
    } finally {
      setValidating(false);
    }
  };

  const handleSave = async () => {
    if (!selectedId) return;
    if (detailLoading) {
      addToast({ type: "error", message: "策略还在加载，请稍候再保存" });
      return;
    }
    if ((tab === "code" || codeDirty) && !editCode.trim()) {
      addToast({ type: "error", message: "代码为空，无法保存" });
      return;
    }
    setSaving(true);
    try {
      const symbols = editSymbols
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      const payload: Record<string, unknown> = {
        name: editName,
        description: editDesc,
        enabled: editEnabled,
        params: { ...editParams, symbols },
      };
      if (tab === "code" || codeDirty) {
        payload.code = editCode;
      }
      await api.updateStrategy(selectedId, payload as any);
      addToast({ type: "success", message: "策略已保存" });
      setDirty(false);
      setCodeDirty(false);
      refetch();
    } catch (err: any) {
      addToast({ type: "error", message: `保存失败: ${err.message}` });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("确定删除此策略？")) return;
    try {
      await api.deleteStrategy(id);
      addToast({ type: "info", message: "策略已删除" });
      if (selectedId === id) setSelectedId(null);
      refetch();
    } catch (err: any) {
      addToast({ type: "error", message: `删除失败: ${err.message}` });
    }
  };

  const handleToggle = async () => {
    if (!selectedId) return;
    const next = !editEnabled;
    setEditEnabled(next);
    setDirty(true);
  };

  const handleRunBacktest = async () => {
    if (!selectedId) return;
    setBtLoading(true);
    setBtResult(null);
    try {
      const result = await api.backtest({
        strategy_id: selectedId,
        symbols: [btSymbol.toUpperCase()],
        start: btStart,
        init_cash: parseFloat(btCash) || 100000,
      });
      setBtResult(result);
    } catch (err: any) {
      addToast({ type: "error", message: `回测失败: ${err.message}` });
    } finally {
      setBtLoading(false);
    }
  };

  if (loading && list.length === 0) return <LoadingOverlay message="加载策略..." />;

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="策略实验室"
        subtitle={`${visibleList.length} 个策略${showMined ? "" : "（已隐藏挖矿策略）"}`}
        right={
          <Button onClick={() => setShowNew(!showNew)} size="sm">
            <Plus className="h-3.5 w-3.5" />
            新建策略
          </Button>
        }
      />

      <div className="flex flex-col md:flex-row h-[calc(100vh-65px)] overflow-hidden">
        {/* ── Left Sidebar ── */}
        <div className="w-full md:w-[260px] shrink-0 border-b md:border-b-0 md:border-r border-[var(--border)] flex flex-col max-h-[40vh] md:max-h-none">
          {/* New strategy form */}
          {showNew && (
            <div className="p-3 border-b border-[var(--border)] bg-[var(--panel-2)]/50">
              <Input
                placeholder="策略名称"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
              />
              <div className="flex gap-2 mt-2">
                <Button size="sm" onClick={handleCreate} loading={creating} disabled={!newName.trim()}>
                  创建
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setShowNew(false)}>
                  取消
                </Button>
              </div>
            </div>
          )}

          <div className="px-3 py-2 border-b border-[var(--border)]/50">
            <label className="flex items-center gap-2 text-xs text-[var(--muted)] cursor-pointer">
              <input
                type="checkbox"
                checked={showMined}
                onChange={(e) => setShowMined(e.target.checked)}
              />
              显示 Alpha/MiMo 挖矿策略（{list.length}）
            </label>
          </div>

          {/* Strategy list */}
          <div className="flex-1 overflow-y-auto p-2 space-y-1">
            {visibleList.length === 0 && (
              <div className="p-4 text-center text-sm text-[var(--muted)]">
                暂无策略，点击上方创建
              </div>
            )}
            {visibleList.map((s) => (
              <div
                key={s.id}
                onClick={() => setSelectedId(s.id)}
                className={`p-3 rounded-lg cursor-pointer transition-all border ${
                  selectedId === s.id
                    ? "bg-[var(--teal)]/5 border-[var(--teal)]/30"
                    : "border-transparent hover:bg-white/[0.03]"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-[var(--text)] truncate">
                    {s.name}
                  </span>
                  <StatusDot status={s.enabled ? "online" : "offline"} />
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-[10px] text-[var(--muted)] mono">{s.type}</span>
                  {s.metrics?.sharpe != null && Number.isFinite(Number(s.metrics.sharpe)) && (
                    <span className="text-[10px] text-[var(--teal)] mono">
                      Sharpe {Number(s.metrics.sharpe).toFixed(2)}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ── Right Main Area ── */}
        <div className="flex-1 min-w-0 min-h-0 flex flex-col">
          {!selected ? (
            <EmptyState
              icon={<Code className="h-12 w-12" />}
              title="选择或创建策略"
              description="从左侧选择一个策略进行编辑"
            />
          ) : (
            <>
              {/* Tabs */}
              <div className="border-b border-[var(--border)] px-4">
                <Tabs
                  tabs={[
                    { key: "code", label: "代码编辑器" },
                    { key: "params", label: "参数配置" },
                    { key: "backtest", label: "回测入口" },
                  ]}
                  active={tab}
                  onChange={setTab}
                />
              </div>

              {/* Tab content */}
              <div className="flex-1 overflow-hidden">
                {/* CODE TAB */}
                {tab === "code" && (
                  <div className="flex flex-col h-full">
                    <div className="flex items-center justify-between px-4 py-2.5 border-b border-[var(--border)]/50 bg-[var(--panel)]/50">
                      <div className="flex items-center gap-2">
                        <Code className="h-4 w-4 text-[var(--teal)]" />
                        <span className="text-sm font-medium">{editName}</span>
                        {dirty && (
                          <Badge variant="warning">未保存</Badge>
                        )}
                      </div>
                      <div className="flex gap-2">
                        <Button size="sm" variant="secondary" onClick={handleValidate} loading={validating} disabled={detailLoading}>
                          <CheckCircle className="h-3.5 w-3.5" />
                          验证代码
                        </Button>
                        <Button size="sm" onClick={handleSave} loading={saving} disabled={!dirty || detailLoading}>
                          <Save className="h-3.5 w-3.5" />
                          保存
                        </Button>
                      </div>
                    </div>
                    {detailLoading && (
                      <div className="px-4 py-2 text-xs text-[var(--muted)] border-b border-[var(--border)]/30">
                        正在加载策略代码…
                      </div>
                    )}
                    <textarea
                      className="flex-1 w-full p-4 bg-[#070a0f] text-[var(--text)] mono text-sm resize-none focus:outline-none border-0"
                      value={editCode}
                      onChange={(e) => {
                        setEditCode(e.target.value);
                        setDirty(true);
                        setCodeDirty(true);
                      }}
                      spellCheck={false}
                    />
                  </div>
                )}

                {/* PARAMS TAB */}
                {tab === "params" && (
                  <div className="p-6 overflow-y-auto h-full">
                    <div className="max-w-xl space-y-4">
                      <div className="flex items-center justify-between">
                        <h3 className="text-sm font-semibold flex items-center gap-2">
                          <Settings className="h-4 w-4 text-[var(--teal)]" />
                          参数配置
                        </h3>
                        <div className="flex items-center gap-3">
                          <button
                            onClick={handleToggle}
                            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors cursor-pointer ${
                              editEnabled ? "bg-[var(--green)]" : "bg-[var(--border)]"
                            }`}
                          >
                            <span
                              className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
                                editEnabled ? "translate-x-4" : "translate-x-0.5"
                              }`}
                            />
                          </button>
                          <span className="text-xs text-[var(--muted)]">
                            {editEnabled ? "已启用" : "已禁用"}
                          </span>
                        </div>
                      </div>

                      <div className="space-y-3">
                        <Input
                          label="策略名称"
                          value={editName}
                          onChange={(e) => {
                            setEditName(e.target.value);
                            setDirty(true);
                          }}
                        />
                        <Input
                          label="描述"
                          value={editDesc}
                          onChange={(e) => {
                            setEditDesc(e.target.value);
                            setDirty(true);
                          }}
                        />
                        <Input
                          label="交易标的 (逗号分隔)"
                          value={editSymbols}
                          onChange={(e) => {
                            setEditSymbols(e.target.value);
                            setDirty(true);
                          }}
                          placeholder="AAPL, MSFT, NVDA"
                        />

                        {/* Dynamic params */}
                        {Object.entries(editParams)
                          .filter(([k]) => k !== "symbols")
                          .map(([key, val]) => (
                            <Input
                              key={key}
                              label={key}
                              type="number"
                              value={String(val)}
                              onChange={(e) => {
                                setEditParams((p) => ({
                                  ...p,
                                  [key]: parseFloat(e.target.value) || 0,
                                }));
                                setDirty(true);
                              }}
                            />
                          ))}
                      </div>

                      <Button onClick={handleSave} loading={saving} disabled={!dirty || detailLoading}>
                        <Save className="h-3.5 w-3.5" />
                        保存参数
                      </Button>

                      {/* Danger zone */}
                      <div className="pt-4 mt-4 border-t border-[var(--border)]">
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => handleDelete(selectedId!)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          删除策略
                        </Button>
                      </div>
                    </div>
                  </div>
                )}

                {/* BACKTEST TAB */}
                {tab === "backtest" && (
                  <div className="p-6 overflow-y-auto h-full">
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                      {/* Config */}
                      <div className="space-y-4">
                        <h3 className="text-sm font-semibold flex items-center gap-2">
                          <FlaskConical className="h-4 w-4 text-[var(--teal)]" />
                          快速回测
                        </h3>
                        <Input
                          label="标的"
                          value={btSymbol}
                          onChange={(e) => setBtSymbol(e.target.value.toUpperCase())}
                        />
                        <Input
                          label="开始日期"
                          type="date"
                          value={btStart}
                          onChange={(e) => setBtStart(e.target.value)}
                        />
                        <Input
                          label="初始资金"
                          type="number"
                          value={btCash}
                          onChange={(e) => setBtCash(e.target.value)}
                        />
                        <Button
                          onClick={handleRunBacktest}
                          loading={btLoading}
                        >
                          <Play className="h-3.5 w-3.5" />
                          运行回测
                        </Button>
                      </div>

                      {/* Results */}
                      {btResult ? (
                        <div className="space-y-3">
                          <h3 className="text-sm font-semibold flex items-center gap-2">
                            <BarChart3 className="h-4 w-4 text-[var(--teal)]" />
                            回测结果
                          </h3>
                          <div className="grid grid-cols-2 gap-3">
                            <MetricCard
                              label="Sharpe"
                              value={btResult.sharpe?.toFixed(2) ?? "—"}
                              tone="teal"
                            />
                            <MetricCard
                              label="总收益"
                              value={fmtPct(btResult.total_return_pct)}
                              tone={(btResult.total_return_pct ?? 0) >= 0 ? "pos" : "neg"}
                            />
                            <MetricCard
                              label="最大回撤"
                              value={fmtPct(btResult.max_drawdown_pct)}
                              tone="neg"
                            />
                            <MetricCard
                              label="交易次数"
                              value={String(btResult.trades ?? "—")}
                            />
                          </div>
                          <div className="grid grid-cols-2 gap-3">
                            <MetricCard
                              label="胜率"
                              value={fmtPct(btResult.win_rate_pct)}
                            />
                            <MetricCard
                              label="Buy & Hold"
                              value={fmtPct(btResult.buy_hold_return_pct)}
                              tone={(btResult.buy_hold_return_pct ?? 0) >= 0 ? "pos" : "neg"}
                            />
                          </div>
                        </div>
                      ) : (
                        <div className="flex items-center justify-center">
                          <EmptyState
                            icon={<FlaskConical className="h-10 w-10" />}
                            title="运行回测查看结果"
                            description="配置参数后点击运行"
                          />
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
