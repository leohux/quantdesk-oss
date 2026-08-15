import { useState, useEffect } from "react";
import {
  PageHeader, Panel, Button, Input, Badge, LoadingOverlay, StatusDot, useToast,
} from "../components/ui";
import {
  api, useApiData,
} from "../lib/api";
import {
  Settings, Save, CheckCircle, AlertCircle, Key, Database,
  Bell, Shield, Loader2, TestTube,
} from "lucide-react";

export function SettingsPage() {
  const { data: settings, loading } = useApiData(() => api.settings(), 0);
  const [form, setForm] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const { addToast } = useToast();

  useEffect(() => {
    if (settings) {
      const flat: Record<string, string> = {};
      for (const [k, v] of Object.entries(settings)) {
        if (k.endsWith("_set")) continue;
        flat[k] = v != null ? String(v) : "";
      }
      setForm(flat);
    }
  }, [settings]);

  const update = (key: string, val: string) => setForm((f) => ({ ...f, [key]: val }));

  const handleSave = async () => {
    setSaving(true);
    try {
      const secretKeys = new Set([
        "alpaca_api_key",
        "alpaca_secret_key",
        "polygon_api_key",
        "telegram_bot_token",
        "access_token",
      ]);
      const payload: Record<string, any> = {};
      for (const [k, v] of Object.entries(form)) {
        if (k.endsWith("_set")) continue;
        if (secretKeys.has(k)) {
          const s = String(v ?? "");
          if (!s || s.startsWith("*")) continue;
          payload[k] = s;
          continue;
        }
        if (v === "" || v == null) continue;
        if (k === "risk_per_trade_pct" || k === "max_position_pct") {
          payload[k] = Number(v);
        } else {
          payload[k] = v;
        }
      }
      await api.saveSettings(payload);
      addToast({ type: "success", message: "设置已保存" });
    } catch (err: any) {
      addToast({ type: "error", message: `保存失败: ${err.message}` });
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <LoadingOverlay message="加载设置..." />;

  const sections = [
    {
      key: "broker",
      icon: <Key className="h-4 w-4" />,
      title: "券商 API",
      subtitle: "Alpaca / Polygon",
      fields: [
        { key: "alpaca_api_key", label: "Alpaca API Key", type: "password" },
        { key: "alpaca_secret_key", label: "Alpaca Secret Key", type: "password" },
        { key: "alpaca_mode", label: "Alpaca 模式", placeholder: "paper / live" },
        { key: "polygon_api_key", label: "Polygon API Key", type: "password" },
      ],
    },
    {
      key: "notify",
      icon: <Bell className="h-4 w-4" />,
      title: "通知",
      subtitle: "Telegram / Webhook",
      fields: [
        { key: "telegram_bot_token", label: "Telegram Bot Token", type: "password" },
        { key: "webhook_url", label: "Webhook URL", placeholder: "https://..." },
      ],
    },
    {
      key: "db",
      icon: <Database className="h-4 w-4" />,
      title: "数据库",
      subtitle: "PostgreSQL / DuckDB",
      fields: [
        { key: "postgres_url", label: "PostgreSQL URL", placeholder: "postgresql://..." },
        { key: "duckdb_path", label: "DuckDB 路径", placeholder: "/path/to/db.duckdb" },
      ],
    },
    {
      key: "risk",
      icon: <Shield className="h-4 w-4" />,
      title: "风控参数",
      subtitle: "交易默认值",
      fields: [
        { key: "risk_per_trade_pct", label: "单笔风险 (%)", placeholder: "2" },
        { key: "max_position_pct", label: "最大仓位 (%)", placeholder: "20" },
      ],
    },
  ];

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="设置"
        subtitle="平台配置"
        right={
          <Button onClick={handleSave} loading={saving}>
            <Save className="h-3.5 w-3.5" />
            保存设置
          </Button>
        }
      />

      <div className="p-6 space-y-4 max-w-3xl">
        {sections.map((section) => (
          <Panel key={section.key} title={section.title} subtitle={section.subtitle}>
            <div className="space-y-3">
              {section.fields.map((field) => (
                <div key={field.key} className="grid grid-cols-1 sm:grid-cols-[160px_1fr] items-start sm:items-center gap-1.5 sm:gap-3">
                  <label className="text-xs text-[var(--muted)] font-medium sm:text-right">
                    {field.label}
                    {field.type === "password" && (settings as any)?.[`${field.key}_set`] ? (
                      <div className="text-[10px] text-[var(--teal)] font-normal mt-0.5">已配置（留空不改）</div>
                    ) : null}
                  </label>
                  <Input
                    type={field.type || "text"}
                    placeholder={field.placeholder || ""}
                    value={form[field.key] || ""}
                    onChange={(e) => update(field.key, e.target.value)}
                  />
                </div>
              ))}
            </div>
          </Panel>
        ))}

        {/* Access Token */}
        <Panel title="访问令牌" subtitle="用于 API 和前端登录">
          <div className="grid grid-cols-1 sm:grid-cols-[160px_1fr] items-start sm:items-center gap-1.5 sm:gap-3">
            <label className="text-xs text-[var(--muted)] font-medium sm:text-right">
              Access Token
            </label>
            <div className="flex gap-2">
              <Input
                type="password"
                value={form.access_token || ""}
                onChange={(e) => update("access_token", e.target.value)}
              />
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  const t = form.access_token;
                  if (t) {
                    navigator.clipboard.writeText(t);
                    addToast({ type: "info", message: "已复制" });
                  }
                }}
              >
                复制
              </Button>
            </div>
          </div>
        </Panel>

        {/* System Info */}
        <Panel title="系统信息">
          <div className="grid grid-cols-2 gap-3">
            {[
              { label: "版本", value: "0.3.0" },
              { label: "交易模式", value: "Paper Trading" },
              { label: "数据源", value: "yfinance + Alpaca" },
              { label: "前端", value: "React + Tailwind" },
            ].map((item) => (
              <div key={item.label} className="flex justify-between p-2.5 rounded-lg bg-[var(--panel-2)]/50 border border-[var(--border)]/30">
                <span className="text-xs text-[var(--muted)]">{item.label}</span>
                <span className="text-xs mono text-[var(--text)]">{item.value}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
