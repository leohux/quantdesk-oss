import type { ReactNode, ButtonHTMLAttributes, CSSProperties, InputHTMLAttributes, SelectHTMLAttributes, ThHTMLAttributes, TdHTMLAttributes, ChangeEvent } from "react";
import { createPortal } from "react-dom";
import { Activity, AlertCircle, Calendar, CheckCircle, ChevronDown, ChevronLeft, ChevronRight, Info, X, Loader2 } from "lucide-react";
import { Children, isValidElement, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, createContext, useContext, useCallback } from "react";

/* ─── Theme Variables (reference) ───────────────────────
   --bg:       #070a0f
   --panel:    #0e141c
   --panel-2:  #121a24
   --border:   #1e2a38
   --text:     #e8eef6
   --muted:    #8b9bb0
   --teal:     #2dd4bf
   --green:    #34d399
   --red:      #f87171
   --amber:    #fbbf24
   ─────────────────────────────────────────────────────── */

/* ═══════════════════════════════════════════════════════
   PAGE HEADER
   ═══════════════════════════════════════════════════════ */
export function PageHeader({
  title,
  subtitle,
  right,
}: {
  title: string;
  subtitle?: string;
  right?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 px-4 sm:px-6 py-4 border-b border-[var(--border)]">
      <div className="flex items-center gap-3 min-w-0">
        <div className="h-9 w-9 shrink-0 rounded-lg bg-[var(--teal)]/10 border border-[var(--teal)]/30 grid place-items-center">
          <Activity className="h-4 w-4 text-[var(--teal)]" />
        </div>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">{title}</h1>
          {subtitle && (
            <p className="text-xs text-[var(--muted)] truncate">{subtitle}</p>
          )}
        </div>
      </div>
      {right && <div className="flex items-center gap-2 flex-wrap">{right}</div>}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   METRIC CARD
   ═══════════════════════════════════════════════════════ */
export function MetricCard({
  label,
  value,
  hint,
  tone = "neutral",
  icon,
  trend,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "neutral" | "pos" | "neg" | "teal" | "amber";
  icon?: ReactNode;
  trend?: { value: string; direction: "up" | "down" | "flat" };
}) {
  const toneColors: Record<string, string> = {
    neutral: "text-[var(--text)]",
    pos: "text-[var(--green)]",
    neg: "text-[var(--red)]",
    teal: "text-[var(--teal)]",
    amber: "text-[var(--amber)]",
  };
  const borderColors: Record<string, string> = {
    neutral: "border-[var(--border)]",
    pos: "border-[var(--green)]/20",
    neg: "border-[var(--red)]/20",
    teal: "border-[var(--teal)]/20",
    amber: "border-[var(--amber)]/20",
  };

  return (
    <div className={`card border ${borderColors[tone]} relative overflow-hidden`}>
      {icon && (
        <div className="absolute top-3 right-3 opacity-10 text-[var(--teal)]">
          {icon}
        </div>
      )}
      <div className="text-xs text-[var(--muted)] mb-1.5 font-medium tracking-wide uppercase">
        {label}
      </div>
      <div className={`text-2xl font-bold mono ${toneColors[tone]}`}>
        {value}
      </div>
      {(hint || trend) && (
        <div className="flex items-center gap-2 mt-1.5">
          {hint && <span className="text-xs text-[var(--muted)]">{hint}</span>}
          {trend && (
            <span
              className={`text-xs mono ${
                trend.direction === "up"
                  ? "text-[var(--green)]"
                  : trend.direction === "down"
                  ? "text-[var(--red)]"
                  : "text-[var(--muted)]"
              }`}
            >
              {trend.direction === "up" ? "↑" : trend.direction === "down" ? "↓" : "→"}{" "}
              {trend.value}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   BUTTON
   ═══════════════════════════════════════════════════════ */
export function Button({
  children,
  variant = "primary",
  size = "md",
  loading = false,
  disabled = false,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger" | "ghost";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
}) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-all cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed";
  const sizes: Record<string, string> = {
    sm: "h-7 px-2.5 text-xs",
    md: "h-9 px-4 text-sm",
    lg: "h-11 px-6 text-base",
  };
  const variants: Record<string, string> = {
    primary:
      "bg-[var(--teal)] text-black hover:bg-[var(--teal)]/90 active:scale-[0.97]",
    secondary:
      "bg-[var(--panel-2)] border border-[var(--border)] text-[var(--text)] hover:bg-[var(--panel-2)]/80",
    danger:
      "bg-[var(--red)]/10 border border-[var(--red)]/30 text-[var(--red)] hover:bg-[var(--red)]/20",
    ghost:
      "text-[var(--muted)] hover:text-[var(--text)] hover:bg-white/5",
  };

  return (
    <button
      className={`${base} ${sizes[size]} ${variants[variant]} ${className}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
      {children}
    </button>
  );
}

/* ═══════════════════════════════════════════════════════
   INPUT / SELECT
   ═══════════════════════════════════════════════════════ */
export function Input({
  label,
  error,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label?: string; error?: string }) {
  return (
    <div className="space-y-1">
      {label && (
        <label className="text-xs text-[var(--muted)] font-medium">{label}</label>
      )}
      <input
        className="w-full h-9 px-3 rounded-lg bg-[var(--panel-2)] border border-[var(--border)] text-sm text-[var(--text)] placeholder:text-[var(--muted)]/50 focus:border-[var(--teal)]/50 focus:outline-none transition-colors"
        {...props}
      />
      {error && <p className="text-xs text-[var(--red)]">{error}</p>}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   DATE PICKER (calendar popover)
   ═══════════════════════════════════════════════════════ */
const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];
const MONTHS = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"];

function toISODate(d: Date) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function parseISODate(s: string): Date | null {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  if (dt.getFullYear() !== y || dt.getMonth() !== m - 1 || dt.getDate() !== d) return null;
  return dt;
}

function formatDisplayDate(s: string) {
  const d = parseISODate(s);
  if (!d) return "";
  return `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`;
}

function startOfDay(d: Date) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

export function DatePicker({
  label,
  value,
  onChange,
  placeholder = "选择日期",
  min,
  max,
  clearable = false,
  clearLabel = "清空",
  disabled = false,
  className = "",
}: {
  label?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  min?: string;
  max?: string;
  clearable?: boolean;
  clearLabel?: string;
  disabled?: boolean;
  className?: string;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [popStyle, setPopStyle] = useState<CSSProperties | null>(null);
  const selected = parseISODate(value);
  const minDate = min ? parseISODate(min) : null;
  const maxDate = max ? parseISODate(max) : null;
  const initialMonth = selected || maxDate || new Date();
  const [viewYear, setViewYear] = useState(initialMonth.getFullYear());
  const [viewMonth, setViewMonth] = useState(initialMonth.getMonth());
  const [mode, setMode] = useState<"day" | "month" | "year">("day");

  useEffect(() => {
    if (!open) return;
    const base = selected || maxDate || new Date();
    setViewYear(base.getFullYear());
    setViewMonth(base.getMonth());
    setMode("day");
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const placePop = useCallback(() => {
    const btn = btnRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const pad = 8;
    const gap = 4;
    const width = Math.min(288, window.innerWidth - pad * 2);
    let left = r.left;
    if (left + width > window.innerWidth - pad) left = window.innerWidth - pad - width;
    if (left < pad) left = pad;
    const estH = 340;
    const spaceBelow = window.innerHeight - r.bottom - gap - pad;
    const openUp = spaceBelow < estH && r.top > spaceBelow;
    setPopStyle({
      position: "fixed",
      left,
      width,
      top: openUp ? "auto" : r.bottom + gap,
      bottom: openUp ? window.innerHeight - r.top + gap : "auto",
      zIndex: 80,
    });
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setPopStyle(null);
      return;
    }
    placePop();
  }, [open, placePop, mode]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (rootRef.current?.contains(t) || popRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onRepos = () => placePop();
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onRepos);
    window.addEventListener("scroll", onRepos, true);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onRepos);
      window.removeEventListener("scroll", onRepos, true);
    };
  }, [open, placePop]);

  const days = useMemo(() => {
    const first = new Date(viewYear, viewMonth, 1);
    const startPad = first.getDay();
    const cells: { date: Date; inMonth: boolean }[] = [];
    const gridStart = new Date(viewYear, viewMonth, 1 - startPad);
    for (let i = 0; i < 42; i++) {
      const d = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
      cells.push({ date: d, inMonth: d.getMonth() === viewMonth });
    }
    return cells;
  }, [viewYear, viewMonth]);

  const yearStart = Math.floor(viewYear / 12) * 12;

  const isDisabled = (d: Date) => {
    const day = startOfDay(d);
    if (minDate && day < startOfDay(minDate)) return true;
    if (maxDate && day > startOfDay(maxDate)) return true;
    return false;
  };

  const pickDay = (d: Date) => {
    if (isDisabled(d)) return;
    onChange(toISODate(d));
    setOpen(false);
  };

  const shiftMonth = (delta: number) => {
    const d = new Date(viewYear, viewMonth + delta, 1);
    setViewYear(d.getFullYear());
    setViewMonth(d.getMonth());
  };

  const today = startOfDay(new Date());

  const pop =
    open && popStyle
      ? createPortal(
          <div
            ref={popRef}
            style={popStyle}
            className="rounded-xl border border-[var(--border)] bg-[var(--panel)] shadow-2xl p-3 animate-fade-in"
          >
            <div className="flex items-center justify-between gap-1 mb-2">
              <button
                type="button"
                className="h-8 w-8 grid place-items-center rounded-lg text-[var(--muted)] hover:bg-white/5 hover:text-[var(--text)] cursor-pointer"
                onClick={() => {
                  if (mode === "day") shiftMonth(-1);
                  else if (mode === "month") setViewYear((y) => y - 1);
                  else setViewYear((y) => y - 12);
                }}
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setMode(mode === "year" ? "day" : "year")}
                  className="h-8 px-2 rounded-lg text-sm font-medium text-[var(--text)] hover:bg-white/5 cursor-pointer"
                >
                  {viewYear}年
                </button>
                {mode !== "year" && (
                  <button
                    type="button"
                    onClick={() => setMode(mode === "month" ? "day" : "month")}
                    className="h-8 px-2 rounded-lg text-sm font-medium text-[var(--text)] hover:bg-white/5 cursor-pointer"
                  >
                    {MONTHS[viewMonth]}
                  </button>
                )}
              </div>
              <button
                type="button"
                className="h-8 w-8 grid place-items-center rounded-lg text-[var(--muted)] hover:bg-white/5 hover:text-[var(--text)] cursor-pointer"
                onClick={() => {
                  if (mode === "day") shiftMonth(1);
                  else if (mode === "month") setViewYear((y) => y + 1);
                  else setViewYear((y) => y + 12);
                }}
              >
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>

            {mode === "day" && (
              <>
                <div className="grid grid-cols-7 mb-1">
                  {WEEKDAYS.map((w) => (
                    <div key={w} className="h-8 grid place-items-center text-[11px] text-[var(--muted)]">
                      {w}
                    </div>
                  ))}
                </div>
                <div className="grid grid-cols-7 gap-0.5">
                  {days.map(({ date, inMonth }) => {
                    const iso = toISODate(date);
                    const selectedDay = value === iso;
                    const isToday = startOfDay(date).getTime() === today.getTime();
                    const disabledDay = isDisabled(date);
                    return (
                      <button
                        key={iso + String(inMonth)}
                        type="button"
                        disabled={disabledDay}
                        onClick={() => pickDay(date)}
                        className={`h-9 rounded-lg text-sm transition-colors cursor-pointer disabled:opacity-25 disabled:cursor-not-allowed ${
                          selectedDay
                            ? "bg-[var(--teal)] text-black font-semibold"
                            : isToday
                              ? "border border-[var(--teal)]/50 text-[var(--teal)]"
                              : inMonth
                                ? "text-[var(--text)] hover:bg-white/5"
                                : "text-[var(--muted)]/40 hover:bg-white/5"
                        }`}
                      >
                        {date.getDate()}
                      </button>
                    );
                  })}
                </div>
              </>
            )}

            {mode === "month" && (
              <div className="grid grid-cols-3 gap-1.5 py-1">
                {MONTHS.map((m, i) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => {
                      setViewMonth(i);
                      setMode("day");
                    }}
                    className={`h-10 rounded-lg text-sm cursor-pointer ${
                      i === viewMonth
                        ? "bg-[var(--teal)] text-black font-semibold"
                        : "text-[var(--text)] hover:bg-white/5"
                    }`}
                  >
                    {m}
                  </button>
                ))}
              </div>
            )}

            {mode === "year" && (
              <div className="grid grid-cols-3 gap-1.5 py-1 max-h-56 overflow-y-auto">
                {Array.from({ length: 12 }, (_, i) => yearStart + i).map((y) => (
                  <button
                    key={y}
                    type="button"
                    onClick={() => {
                      setViewYear(y);
                      setMode("month");
                    }}
                    className={`h-10 rounded-lg text-sm cursor-pointer ${
                      y === viewYear
                        ? "bg-[var(--teal)] text-black font-semibold"
                        : "text-[var(--text)] hover:bg-white/5"
                    }`}
                  >
                    {y}
                  </button>
                ))}
              </div>
            )}

            <div className="flex items-center justify-between mt-2 pt-2 border-t border-[var(--border)]">
              <button
                type="button"
                className="h-8 px-2.5 rounded-lg text-xs text-[var(--teal)] hover:bg-[var(--teal)]/10 cursor-pointer"
                onClick={() => {
                  const t = new Date();
                  if (!isDisabled(t)) {
                    onChange(toISODate(t));
                    setOpen(false);
                  } else {
                    setViewYear(t.getFullYear());
                    setViewMonth(t.getMonth());
                    setMode("day");
                  }
                }}
              >
                今天
              </button>
              {clearable && (
                <button
                  type="button"
                  className="h-8 px-2.5 rounded-lg text-xs text-[var(--muted)] hover:text-[var(--text)] hover:bg-white/5 cursor-pointer"
                  onClick={() => {
                    onChange("");
                    setOpen(false);
                  }}
                >
                  {clearLabel}
                </button>
              )}
            </div>
          </div>,
          document.body,
        )
      : null;

  return (
    <div className={`space-y-1 min-w-0 w-full ${className}`} ref={rootRef}>
      {label && (
        <label className="text-xs text-[var(--muted)] font-medium">{label}</label>
      )}
      <button
        type="button"
        ref={btnRef}
        disabled={disabled}
        onClick={() => !disabled && setOpen((v) => !v)}
        className="w-full min-w-0 h-9 px-3 rounded-lg bg-[var(--panel-2)] border border-[var(--border)] text-sm text-left flex items-center gap-2 focus:border-[var(--teal)]/50 focus:outline-none transition-colors disabled:opacity-50 cursor-pointer"
      >
        <Calendar className="h-3.5 w-3.5 shrink-0 text-[var(--muted)]" />
        <span className={`flex-1 min-w-0 truncate ${value ? "text-[var(--text)]" : "text-[var(--muted)]/60"}`}>
          {value ? formatDisplayDate(value) : placeholder}
        </span>
        {clearable && value && (
          <span
            role="button"
            tabIndex={-1}
            onClick={(e) => {
              e.stopPropagation();
              onChange("");
            }}
            className="shrink-0 text-[var(--muted)] hover:text-[var(--text)]"
          >
            <X className="h-3.5 w-3.5" />
          </span>
        )}
      </button>
      {pop}
    </div>
  );
}

type SelectOption = { value: string; label: string; disabled?: boolean };

function collectSelectOptions(children: ReactNode): SelectOption[] {
  const out: SelectOption[] = [];
  Children.forEach(children, (child) => {
    if (!isValidElement<{ value?: string | number; children?: ReactNode; disabled?: boolean }>(child)) return;
    if (child.type !== "option") return;
    out.push({
      value: String(child.props.value ?? ""),
      label: String(child.props.children ?? ""),
      disabled: Boolean(child.props.disabled),
    });
  });
  return out;
}

/** Custom select — native <select> popups ignore CSS width and overflow past the viewport. */
export function Select({
  label,
  children,
  value,
  defaultValue,
  onChange,
  disabled,
  id,
  className = "",
}: SelectHTMLAttributes<HTMLSelectElement> & { label?: string }) {
  const options = collectSelectOptions(children);
  const autoId = useId();
  const selectId = id || autoId;
  const rootRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const [open, setOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState<CSSProperties | null>(null);
  const [internal, setInternal] = useState(String(defaultValue ?? ""));
  const current = value !== undefined ? String(value) : internal;
  const selected = options.find((o) => o.value === current) || options[0];

  const placeMenu = useCallback(() => {
    const btn = btnRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const gap = 4;
    const pad = 8;
    const maxH = 240;
    const width = Math.min(r.width, window.innerWidth - pad * 2);
    let left = r.left;
    if (left + width > window.innerWidth - pad) left = window.innerWidth - pad - width;
    if (left < pad) left = pad;
    const spaceBelow = window.innerHeight - r.bottom - gap - pad;
    const spaceAbove = r.top - gap - pad;
    const openUp = spaceBelow < Math.min(maxH, 160) && spaceAbove > spaceBelow;
    const height = Math.min(maxH, Math.max(120, openUp ? spaceAbove : spaceBelow));
    setMenuStyle({
      position: "fixed",
      left,
      width,
      maxWidth: width,
      minWidth: 0,
      boxSizing: "border-box",
      maxHeight: height,
      top: openUp ? "auto" : r.bottom + gap,
      bottom: openUp ? window.innerHeight - r.top + gap : "auto",
      zIndex: 80,
    });
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setMenuStyle(null);
      return;
    }
    placeMenu();
  }, [open, placeMenu, options.length]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (rootRef.current?.contains(t) || listRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onReposition = () => placeMenu();
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onReposition);
    window.addEventListener("scroll", onReposition, true);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onReposition);
      window.removeEventListener("scroll", onReposition, true);
    };
  }, [open, placeMenu]);

  const pick = (next: string) => {
    if (value === undefined) setInternal(next);
    onChange?.({ target: { value: next } } as ChangeEvent<HTMLSelectElement>);
    setOpen(false);
  };

  const menu =
    open && menuStyle
      ? createPortal(
          <ul
            ref={listRef}
            role="listbox"
            style={menuStyle}
            className="overflow-y-auto overflow-x-hidden rounded-lg border border-[var(--border)] bg-[var(--panel)] shadow-xl py-1"
          >
            {options.map((opt) => {
              const active = opt.value === current;
              return (
                <li key={opt.value} role="option" aria-selected={active} className="min-w-0">
                  <button
                    type="button"
                    disabled={opt.disabled}
                    title={opt.label}
                    onClick={() => pick(opt.value)}
                    className={`block w-full max-w-full min-w-0 px-3 py-2 text-left text-sm overflow-hidden text-ellipsis whitespace-nowrap disabled:opacity-40 ${
                      active
                        ? "bg-white/10 text-[var(--text)]"
                        : "text-[var(--text)] hover:bg-white/5"
                    }`}
                  >
                    {opt.label}
                  </button>
                </li>
              );
            })}
          </ul>,
          document.body,
        )
      : null;

  return (
    <div className="space-y-1 min-w-0 w-full" ref={rootRef}>
      {label && (
        <label htmlFor={selectId} className="text-xs text-[var(--muted)] font-medium">
          {label}
        </label>
      )}
      <div className="relative min-w-0 w-full">
        <button
          type="button"
          ref={btnRef}
          id={selectId}
          disabled={disabled}
          aria-haspopup="listbox"
          aria-expanded={open}
          onClick={() => !disabled && setOpen((v) => !v)}
          className={`w-full min-w-0 max-w-full h-9 px-3 rounded-lg bg-[var(--panel-2)] border border-[var(--border)] text-sm text-[var(--text)] focus:border-[var(--teal)]/50 focus:outline-none transition-colors flex items-center gap-2 text-left disabled:opacity-50 ${className}`}
        >
          <span className="flex-1 min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">
            {selected?.label || "选择…"}
          </span>
          <ChevronDown className={`h-3.5 w-3.5 shrink-0 text-[var(--muted)] transition-transform ${open ? "rotate-180" : ""}`} />
        </button>
        {menu}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   TABLE
   ═══════════════════════════════════════════════════════ */
export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">{children}</table>
    </div>
  );
}

export function Th({ children, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className="text-left text-xs text-[var(--muted)] font-medium uppercase tracking-wider pb-2 px-3 first:pl-0"
      {...props}
    >
      {children}
    </th>
  );
}

export function Td({ children, className = "", ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={`py-2.5 px-3 first:pl-0 ${className}`} {...props}>
      {children}
    </td>
  );
}

export function Tr({ children, className = "", ...props }: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr
      className={`border-b border-[var(--border)]/50 last:border-0 hover:bg-white/[0.02] transition-colors ${className}`}
      {...props}
    >
      {children}
    </tr>
  );
}

/* ═══════════════════════════════════════════════════════
   BADGE
   ═══════════════════════════════════════════════════════ */
export function Badge({
  children,
  variant = "default",
}: {
  children: ReactNode;
  variant?: "default" | "success" | "danger" | "warning" | "info";
}) {
  const variants: Record<string, string> = {
    default: "bg-[var(--panel-2)] text-[var(--muted)] border-[var(--border)]",
    success: "bg-[var(--green)]/10 text-[var(--green)] border-[var(--green)]/20",
    danger: "bg-[var(--red)]/10 text-[var(--red)] border-[var(--red)]/20",
    warning: "bg-[var(--amber)]/10 text-[var(--amber)] border-[var(--amber)]/20",
    info: "bg-[var(--teal)]/10 text-[var(--teal)] border-[var(--teal)]/20",
  };
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full border ${variants[variant]}`}
    >
      {children}
    </span>
  );
}

/* ═══════════════════════════════════════════════════════
   TABS
   ═══════════════════════════════════════════════════════ */
export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: string; label: string; count?: number }[];
  active: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="flex gap-1 border-b border-[var(--border)]">
      {tabs.map((tab) => (
        <button
          key={tab.key}
          onClick={() => onChange(tab.key)}
          className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px cursor-pointer ${
            active === tab.key
              ? "border-[var(--teal)] text-[var(--teal)]"
              : "border-transparent text-[var(--muted)] hover:text-[var(--text)]"
          }`}
        >
          {tab.label}
          {tab.count !== undefined && (
            <span className="ml-1.5 text-xs opacity-60">({tab.count})</span>
          )}
        </button>
      ))}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   SKELETON / LOADING
   ═══════════════════════════════════════════════════════ */
export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      className={`animate-pulse rounded-lg bg-[var(--panel-2)] ${className}`}
    />
  );
}

export function MetricCardSkeleton() {
  return (
    <div className="card border border-[var(--border)]">
      <Skeleton className="h-3 w-20 mb-3" />
      <Skeleton className="h-7 w-28 mb-2" />
      <Skeleton className="h-3 w-16" />
    </div>
  );
}

export function TableSkeleton({ rows = 5, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex gap-3">
          {Array.from({ length: cols }).map((_, j) => (
            <Skeleton key={j} className="h-4 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}

export function LoadingSpinner({ size = 20 }: { size?: number }) {
  return <Loader2 className="animate-spin text-[var(--teal)]" style={{ width: size, height: size }} />;
}

export function LoadingOverlay({
  message = "加载中...",
  action,
}: {
  message?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 gap-3">
      <LoadingSpinner size={28} />
      <span className="text-sm text-[var(--muted)]">{message}</span>
      {action}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   EMPTY STATE
   ═══════════════════════════════════════════════════════ */
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      {icon && <div className="text-[var(--muted)]/30 mb-3">{icon}</div>}
      <h3 className="text-sm font-medium text-[var(--muted)]">{title}</h3>
      {description && (
        <p className="text-xs text-[var(--muted)]/60 mt-1 max-w-sm">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   ERROR BOUNDARY
   ═══════════════════════════════════════════════════════ */
export function ErrorFallback({
  error,
  resetError,
}: {
  error: Error;
  resetError?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <AlertCircle className="h-8 w-8 text-[var(--red)] mb-3" />
      <h3 className="text-sm font-medium text-[var(--red)]">出错了</h3>
      <p className="text-xs text-[var(--muted)] mt-1 max-w-md mono">{error.message}</p>
      {resetError && (
        <Button variant="secondary" size="sm" onClick={resetError} className="mt-4">
          重试
        </Button>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   TOAST SYSTEM
   ═══════════════════════════════════════════════════════ */
export interface Toast {
  id: string;
  type: "success" | "error" | "info" | "warning";
  message: string;
  duration?: number;
}

const ToastContext = createContext<{
  toasts: Toast[];
  addToast: (toast: Omit<Toast, "id">) => void;
  removeToast: (id: string) => void;
}>({
  toasts: [],
  addToast: () => {},
  removeToast: () => {},
});

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((toast: Omit<Toast, "id">) => {
    const id = Math.random().toString(36).slice(2, 9);
    setToasts((prev) => [...prev, { ...toast, id }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, toast.duration || 4000);
  }, []);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ toasts, addToast, removeToast }}>
      {children}
      <ToastContainer toasts={toasts} removeToast={removeToast} />
    </ToastContext.Provider>
  );
}

function ToastContainer({
  toasts,
  removeToast,
}: {
  toasts: Toast[];
  removeToast: (id: string) => void;
}) {
  if (toasts.length === 0) return null;

  const icons: Record<string, ReactNode> = {
    success: <CheckCircle className="h-4 w-4 text-[var(--green)]" />,
    error: <AlertCircle className="h-4 w-4 text-[var(--red)]" />,
    info: <Info className="h-4 w-4 text-[var(--teal)]" />,
    warning: <AlertCircle className="h-4 w-4 text-[var(--amber)]" />,
  };

  const borders: Record<string, string> = {
    success: "border-[var(--green)]/30",
    error: "border-[var(--red)]/30",
    info: "border-[var(--teal)]/30",
    warning: "border-[var(--amber)]/30",
  };

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-start gap-3 px-4 py-3 rounded-lg bg-[var(--panel)] border ${borders[toast.type]} shadow-xl backdrop-blur-sm animate-slide-in`}
        >
          <span className="mt-0.5 shrink-0">{icons[toast.type]}</span>
          <span className="text-sm text-[var(--text)] flex-1">{toast.message}</span>
          <button
            onClick={() => removeToast(toast.id)}
            className="text-[var(--muted)] hover:text-[var(--text)] cursor-pointer"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   STATUS DOT
   ═══════════════════════════════════════════════════════ */
export function StatusDot({ status }: { status: "online" | "offline" | "warning" }) {
  const colors = {
    online: "bg-[var(--green)]",
    offline: "bg-[var(--red)]",
    warning: "bg-[var(--amber)]",
  };
  return (
    <span className="relative flex h-2 w-2">
      {status === "online" && (
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[var(--green)] opacity-40" />
      )}
      <span className={`relative inline-flex rounded-full h-2 w-2 ${colors[status]}`} />
    </span>
  );
}

/* ═══════════════════════════════════════════════════════
   PROGRESS BAR
   ═══════════════════════════════════════════════════════ */
export function ProgressBar({
  value,
  max = 100,
  color = "teal",
  showLabel = false,
}: {
  value: number;
  max?: number;
  color?: "teal" | "green" | "red" | "amber";
  showLabel?: boolean;
}) {
  const pct = Math.min(Math.max((value / max) * 100, 0), 100);
  const colors: Record<string, string> = {
    teal: "bg-[var(--teal)]",
    green: "bg-[var(--green)]",
    red: "bg-[var(--red)]",
    amber: "bg-[var(--amber)]",
  };
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 rounded-full bg-[var(--panel-2)] overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${colors[color]}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {showLabel && (
        <span className="text-xs mono text-[var(--muted)] w-10 text-right">
          {Math.round(pct)}%
        </span>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   MODAL
   ═══════════════════════════════════════════════════════ */
export function Modal({
  open,
  onClose,
  title,
  subtitle,
  children,
  wide = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />
      <div
        className={`relative w-full ${wide ? "max-w-3xl" : "max-w-lg"} max-h-[85vh] flex flex-col rounded-xl border border-[var(--border)] bg-[var(--panel)] shadow-2xl animate-fade-in`}
      >
        <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-[var(--border)]">
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-[var(--text)] truncate">{title}</h2>
            {subtitle && (
              <p className="text-xs text-[var(--muted)] mt-0.5 truncate">{subtitle}</p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 p-1.5 rounded-lg text-[var(--muted)] hover:text-[var(--text)] hover:bg-white/5 transition-colors cursor-pointer"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="overflow-y-auto p-5 flex-1">{children}</div>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════
   PANEL / CARD WRAPPER
   ═══════════════════════════════════════════════════════ */
export function Panel({
  title,
  subtitle,
  right,
  children,
  className = "",
  noPad = false,
}: {
  title?: string;
  subtitle?: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  noPad?: boolean;
}) {
  return (
    <div className={`card ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between mb-4">
          <div>
            {title && (
              <h3 className="text-sm font-semibold text-[var(--text)]">{title}</h3>
            )}
            {subtitle && (
              <p className="text-xs text-[var(--muted)] mt-0.5">{subtitle}</p>
            )}
          </div>
          {right}
        </div>
      )}
      <div className={noPad ? "-mx-4 -mb-4" : ""}>{children}</div>
    </div>
  );
}
