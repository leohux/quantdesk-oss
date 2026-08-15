import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { Activity, Lock, AlertCircle, Eye, EyeOff } from "lucide-react";
import { Button, Input } from "../components/ui";
import { getToken } from "../lib/api";

const TOKEN_KEY = "quantdesk_token";
const ROLE_KEY = "quantdesk_role";

export function LoginPage() {
  const [mode, setMode] = useState<"jwt" | "token">("jwt");
  const [password, setPassword] = useState("");
  const [legacyToken, setLegacyToken] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  if (getToken()) return <Navigate to="/" replace />;

  const handleJwtLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password.trim()) return;

    setLoading(true);
    setError("");

    try {
      const res = await fetch("/api/auth/jwt-login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || "登录失败");
      }

      const data = await res.json();
      localStorage.setItem(TOKEN_KEY, data.access_token);
      localStorage.setItem(ROLE_KEY, data.role);
      // Also store refresh token
      localStorage.setItem("quantdesk_refresh", data.refresh_token);
      navigate("/");
    } catch (err: any) {
      setError(err.message || "连接失败");
    } finally {
      setLoading(false);
    }
  };

  const handleTokenLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!legacyToken.trim()) return;

    setLoading(true);
    setError("");

    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: legacyToken.trim() }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || "认证失败");
      }

      const data = await res.json();
      localStorage.setItem(TOKEN_KEY, data.token);
      localStorage.setItem(ROLE_KEY, "admin");
      navigate("/");
    } catch (err: any) {
      setError(err.message || "连接失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="h-14 w-14 rounded-2xl bg-[var(--teal)]/10 border border-[var(--teal)]/30 grid place-items-center mx-auto mb-4">
            <Activity className="h-7 w-7 text-[var(--teal)]" />
          </div>
          <h1 className="text-2xl font-bold tracking-wide">QuantDesk</h1>
          <p className="text-sm text-[var(--muted)] mt-1">美股量化交易平台</p>
        </div>

        {/* Mode Switch */}
        <div className="flex gap-1 p-1 rounded-lg bg-[var(--panel-2)] border border-[var(--border)] mb-4">
          <button
            onClick={() => { setMode("jwt"); setError(""); }}
            className={`flex-1 py-2 text-sm rounded-md transition cursor-pointer ${
              mode === "jwt"
                ? "bg-[var(--teal)]/10 text-[var(--teal)] border border-[var(--teal)]/30"
                : "text-[var(--muted)] hover:text-[var(--text)]"
            }`}
          >
            密码登录
          </button>
          <button
            onClick={() => { setMode("token"); setError(""); }}
            className={`flex-1 py-2 text-sm rounded-md transition cursor-pointer ${
              mode === "token"
                ? "bg-[var(--teal)]/10 text-[var(--teal)] border border-[var(--teal)]/30"
                : "text-[var(--muted)] hover:text-[var(--text)]"
            }`}
          >
            <Lock className="h-3.5 w-3.5 inline mr-1.5" />
            Token 登录
          </button>
        </div>

        {/* JWT Login Form */}
        {mode === "jwt" && (
          <form onSubmit={handleJwtLogin} className="space-y-4">
            <div className="card space-y-4">
              <div className="relative">
                <Input
                  type={showPassword ? "text" : "password"}
                  placeholder="密码"
                  value={password}
                  autoFocus
                  onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--muted)] hover:text-[var(--text)] cursor-pointer"
                >
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>

              {error && (
                <div className="flex items-center gap-2 text-sm text-[var(--red)]">
                  <AlertCircle className="h-4 w-4 shrink-0" />
                  <span>{error}</span>
                </div>
              )}

              <Button
                type="submit"
                className="w-full"
                loading={loading}
                disabled={!password.trim()}
              >
                登录
              </Button>

            </div>
          </form>
        )}

        {/* Token Login Form */}
        {mode === "token" && (
          <form onSubmit={handleTokenLogin} className="space-y-4">
            <div className="card space-y-4">
              <div className="flex items-center gap-2 text-sm text-[var(--muted)]">
                <Lock className="h-4 w-4" />
                <span>输入 Access Token 登录</span>
              </div>

              <Input
                type="password"
                placeholder="Access Token"
                value={legacyToken}
                onChange={(e) => setLegacyToken(e.target.value)}
                autoFocus
              />

              {error && (
                <div className="flex items-center gap-2 text-sm text-[var(--red)]">
                  <AlertCircle className="h-4 w-4 shrink-0" />
                  <span>{error}</span>
                </div>
              )}

              <Button
                type="submit"
                className="w-full"
                loading={loading}
                disabled={!legacyToken.trim()}
              >
                登录
              </Button>
            </div>
          </form>
        )}

        <p className="text-center text-xs text-[var(--muted)]/50 mt-6">
          quantdesk.example.com · v1.0.0
        </p>
      </div>
    </div>
  );
}
