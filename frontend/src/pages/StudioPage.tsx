import React, { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  Calendar,
  CheckCircle2,
  Copy,
  KeyRound,
  Mail,
  Play,
  Plus,
  RotateCw,
  Settings2,
  Trash2,
  XCircle,
} from "lucide-react";
import { apiRequest, formatBytes, formatRelativeTime } from "../lib/api";
import type {
  DeepHealth,
  DeliveryItem,
  EditionItem,
  McpTokenItem,
  SettingsPayload,
  SourceItem,
} from "../lib/types";
import { useToast } from "../components/Toast";

type StudioTab = "editions" | "deliveries" | "settings";

export function StudioPage() {
  const qc = useQueryClient();
  const { notify } = useToast();
  const [tab, setTab] = useState<StudioTab>("editions");
  const [liveBanner, setLiveBanner] = useState<string>("");

  // New edition form
  const [edName, setEdName] = useState("每日晨报");
  const [edSubtitle, setEdSubtitle] = useState("每日精选热点与深度长文");
  const [edCron, setEdCron] = useState("0 6 * * *");
  const [edCoverStyle, setEdCoverStyle] = useState<"editorial" | "dark" | "minimal">("editorial");
  const [edLookback, setEdLookback] = useState(24);
  const [edAiBrief, setEdAiBrief] = useState(true);
  const [selectedSourceIds, setSelectedSourceIds] = useState<number[]>([]);

  // Settings local drafts
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState(465);
  const [smtpSsl, setSmtpSsl] = useState(true);
  const [smtpUser, setSmtpUser] = useState("");
  const [smtpPass, setSmtpPass] = useState("");
  const [smtpFrom, setSmtpFrom] = useState("");
  const [kindleEmail, setKindleEmail] = useState("");

  const [aiEnabled, setAiEnabled] = useState(false);
  const [aiBaseUrl, setAiBaseUrl] = useState("");
  const [aiKey, setAiKey] = useState("");
  const [aiModel, setAiModel] = useState("");
  const [aiProxy, setAiProxy] = useState(false);

  const [netProxy, setNetProxy] = useState("");
  const [mcpName, setMcpName] = useState("ai-ops");
  const [mcpReadonly, setMcpReadonly] = useState(true);
  const [createdToken, setCreatedToken] = useState<string | null>(null);

  const editionsQuery = useQuery({
    queryKey: ["editions"],
    queryFn: () => apiRequest<{ items: EditionItem[] }>("/api/v1/editions"),
  });
  const sourcesQuery = useQuery({
    queryKey: ["sources"],
    queryFn: () => apiRequest<{ items: SourceItem[] }>("/api/v1/sources"),
  });
  const deliveriesQuery = useQuery({
    queryKey: ["deliveries"],
    queryFn: () =>
      apiRequest<{
        items: DeliveryItem[];
        summary: { total_sent: number; total_failed: number };
      }>("/api/v1/deliveries"),
  });
  const settingsQuery = useQuery({
    queryKey: ["settings"],
    queryFn: () => apiRequest<SettingsPayload>("/api/v1/settings"),
  });
  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: () => apiRequest<DeepHealth>("/api/v1/health"),
    enabled: tab === "settings",
  });
  const mcpTokensQuery = useQuery({
    queryKey: ["mcp-tokens"],
    queryFn: () => apiRequest<{ items: McpTokenItem[] }>("/api/v1/settings/mcp-tokens"),
    enabled: tab === "settings",
  });

  useEffect(() => {
    const s = settingsQuery.data;
    if (!s) return;
    setSmtpHost(s.smtp.host);
    setSmtpPort(s.smtp.port);
    setSmtpSsl(s.smtp.use_ssl);
    setSmtpUser(s.smtp.username);
    setSmtpFrom(s.smtp.from_email);
    setKindleEmail(s.smtp.default_kindle_email);
    setAiEnabled(s.ai.enabled);
    setAiBaseUrl(s.ai.base_url);
    setAiModel(s.ai.model);
    setAiProxy(s.ai.use_proxy);
    setNetProxy(s.network.outbound_proxy);
  }, [settingsQuery.data]);

  // Live SSE progress listener
  useEffect(() => {
    const es = new EventSource("/api/v1/deliveries/stream");
    es.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data) as { type: string; data?: Record<string, unknown> };
        if (parsed.type === "delivery.stage") {
          setLiveBanner(`正在执行刊物流水线：${String(parsed.data?.stage ?? "")}`);
        } else if (parsed.type === "delivery.completed") {
          setLiveBanner("");
          qc.invalidateQueries({ queryKey: ["deliveries"] });
          qc.invalidateQueries({ queryKey: ["books"] });
          qc.invalidateQueries({ queryKey: ["editions"] });
        } else if (parsed.type === "delivery.failed") {
          setLiveBanner("");
          qc.invalidateQueries({ queryKey: ["deliveries"] });
        }
      } catch {
        // ignore keepalive
      }
    };
    return () => es.close();
  }, [qc]);

  const createEdition = async (e: React.FormEvent) => {
    e.preventDefault();
    if (selectedSourceIds.length === 0) {
      notify("请至少勾选 1 个订阅源", "error");
      return;
    }
    try {
      await apiRequest("/api/v1/editions", {
        method: "POST",
        body: JSON.stringify({
          name: edName.trim(),
          subtitle: edSubtitle.trim(),
          cover_style: edCoverStyle,
          cron_expr: edCron.trim(),
          lookback_hours: edLookback,
          ai_briefing_enabled: edAiBrief,
          sources: selectedSourceIds.map((id, idx) => ({ source_id: id, sort_order: idx })),
        }),
      });
      await qc.invalidateQueries({ queryKey: ["editions"] });
      notify("已创建定时刊物", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "创建失败", "error");
    }
  };

  const triggerEdition = async (ed: EditionItem, sendEmail: boolean) => {
    try {
      await apiRequest(`/api/v1/editions/${ed.id}/deliver?send_email=${sendEmail}`, {
        method: "POST",
      });
      notify(
        sendEmail
          ? `已触发《${ed.name}》编译并推送至 Kindle`
          : `已触发《${ed.name}》仅生成 EPUB 归档`,
        "success",
      );
      setTab("deliveries");
    } catch (err) {
      notify(err instanceof Error ? err.message : "触发失败", "error");
    }
  };

  const deleteEdition = async (ed: EditionItem) => {
    if (!window.confirm(`确认删除刊物《${ed.name}》？`)) return;
    await apiRequest(`/api/v1/editions/${ed.id}`, { method: "DELETE" });
    await qc.invalidateQueries({ queryKey: ["editions"] });
    notify("已删除刊物", "success");
  };

  const saveSmtp = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await apiRequest("/api/v1/settings/smtp", {
        method: "PUT",
        body: JSON.stringify({
          host: smtpHost,
          port: smtpPort,
          use_ssl: smtpSsl,
          username: smtpUser,
          password: smtpPass || undefined,
          from_email: smtpFrom,
          default_kindle_email: kindleEmail,
        }),
      });
      setSmtpPass("");
      await qc.invalidateQueries({ queryKey: ["settings"] });
      notify("SMTP 与 Kindle 邮箱配置已加密保存", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存失败", "error");
    }
  };

  const testSmtp = async () => {
    try {
      await apiRequest("/api/v1/settings/smtp/test", {
        method: "POST",
        body: JSON.stringify({ target_email: smtpFrom || smtpUser }),
      });
      notify("测试邮件已发送成功，请检查收件箱", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "SMTP 测试失败", "error");
    }
  };

  const saveAi = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await apiRequest("/api/v1/settings/ai", {
        method: "PUT",
        body: JSON.stringify({
          enabled: aiEnabled,
          base_url: aiBaseUrl,
          api_key: aiKey || undefined,
          model: aiModel,
          use_proxy: aiProxy,
        }),
      });
      setAiKey("");
      await qc.invalidateQueries({ queryKey: ["settings"] });
      notify("AI 接口配置已加密保存", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存失败", "error");
    }
  };

  const testAi = async () => {
    try {
      const res = await apiRequest<{ reply: string; model: string }>("/api/v1/settings/ai/test", {
        method: "POST",
      });
      notify(`AI 连通成功 (${res.model})：${res.reply}`, "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "AI 连通测试失败", "error");
    }
  };

  const createMcpToken = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await apiRequest<{ token: string }>("/api/v1/settings/mcp-tokens", {
        method: "POST",
        body: JSON.stringify({ name: mcpName.trim(), readonly: mcpReadonly }),
      });
      setCreatedToken(res.token);
      await qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
      notify("MCP Token 已生成，请立即复制保存", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "生成失败", "error");
    }
  };

  const editions = editionsQuery.data?.items ?? [];
  const sources = sourcesQuery.data?.items ?? [];
  const deliveries = deliveriesQuery.data?.items ?? [];
  const summary = deliveriesQuery.data?.summary;

  return (
    <div className="max-w-5xl mx-auto pb-16">
      <header className="pb-6 mb-7 border-b-2" style={{ borderColor: "var(--fg-primary)" }}>
        <p className="text-xs uppercase tracking-[0.2em] mb-1.5" style={{ color: "var(--accent)" }}>
          PUBLISHING &amp; AI-OPS STUDIO
        </p>
        <h1 className="font-editorial text-4xl font-semibold">发行工作室 · Studio</h1>

        {liveBanner && (
          <div
            className="mt-4 rounded-xl px-4 py-2.5 text-xs font-medium flex items-center gap-2"
            style={{ backgroundColor: "var(--accent-soft)", color: "var(--accent)" }}
          >
            <Activity className="w-4 h-4 animate-pulse" />
            <span>{liveBanner}</span>
          </div>
        )}

        <div className="flex flex-wrap gap-2 mt-5">
          {[
            { id: "editions" as const, label: `定时刊物 (${editions.length})`, icon: Calendar },
            { id: "deliveries" as const, label: "投递遥测与日志", icon: Activity },
            { id: "settings" as const, label: "SMTP / AI / MCP 运维设置", icon: Settings2 },
          ].map((item) => {
            const Icon = item.icon;
            const active = tab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                className="inline-flex items-center gap-1.5 text-xs font-medium px-4 py-2 rounded-full border cursor-pointer"
                style={{
                  backgroundColor: active ? "var(--fg-primary)" : "var(--bg-surface)",
                  color: active ? "var(--bg-canvas)" : "var(--fg-secondary)",
                  borderColor: "var(--border-hairline)",
                }}
              >
                <Icon className="w-3.5 h-3.5" />
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      </header>

      {tab === "editions" && (
        <div className="space-y-8">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            {editions.map((ed) => (
              <div
                key={ed.id}
                className="rounded-2xl border p-5 flex flex-col justify-between gap-4"
                style={{
                  backgroundColor: "var(--bg-surface)",
                  borderColor: "var(--border-hairline)",
                  boxShadow: "var(--shadow-paper)",
                }}
              >
                <div>
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <h3 className="font-editorial text-2xl font-semibold">{ed.name}</h3>
                      <p className="text-xs mt-0.5" style={{ color: "var(--fg-secondary)" }}>
                        {ed.subtitle} · 第 {ed.issue_counter} 期
                      </p>
                    </div>
                    <span
                      className="text-[11px] font-mono px-2.5 py-1 rounded-full"
                      style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-secondary)" }}
                    >
                      {ed.cron_expr}
                    </span>
                  </div>

                  <div className="flex flex-wrap gap-1.5 mt-3">
                    {ed.sources.map((s) => (
                      <span
                        key={s.source_id}
                        className="text-[11px] px-2 py-0.5 rounded border"
                        style={{
                          borderColor: "var(--border-hairline)",
                          color: "var(--fg-secondary)",
                        }}
                      >
                        {s.source_name}
                      </span>
                    ))}
                  </div>

                  <div className="text-xs mt-3 space-y-0.5" style={{ color: "var(--fg-muted)" }}>
                    <div>
                      窗口：{ed.lookback_hours}h · 去重：{ed.dedup_window_days}天 · 单卷上限：
                      {ed.max_mb_per_volume}MB · {ed.ai_briefing_enabled ? "含 AI 头版综述" : "无 AI 综述"}
                    </div>
                    {ed.next_run_at && (
                      <div>下次发行：{new Date(ed.next_run_at).toLocaleString("zh-CN")}</div>
                    )}
                  </div>
                </div>

                <div
                  className="flex flex-wrap items-center justify-between gap-2 pt-3 border-t"
                  style={{ borderColor: "var(--border-hairline)" }}
                >
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => triggerEdition(ed, true)}
                      className="inline-flex items-center gap-1 text-xs font-medium px-3 py-1.5 rounded-lg cursor-pointer"
                      style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
                    >
                      <Play className="w-3.5 h-3.5" />
                      <span>立即生成并推 Kindle</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => triggerEdition(ed, false)}
                      className="text-xs px-2.5 py-1.5 rounded-lg border cursor-pointer"
                      style={{ borderColor: "var(--border-hairline)", color: "var(--fg-secondary)" }}
                    >
                      仅生成 EPUB
                    </button>
                  </div>
                  <button
                    type="button"
                    onClick={() => deleteEdition(ed)}
                    className="p-1.5 rounded-lg cursor-pointer"
                    style={{ color: "var(--fg-muted)" }}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>

          {/* Create Edition Form */}
          <form
            onSubmit={createEdition}
            className="rounded-2xl border p-6 space-y-4"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <h2 className="font-editorial text-xl font-semibold">编排新定时刊物</h2>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <input
                type="text"
                required
                value={edName}
                onChange={(e) => setEdName(e.target.value)}
                placeholder="刊物名称"
                className="rounded-lg border px-3 py-2 text-sm"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <input
                type="text"
                value={edSubtitle}
                onChange={(e) => setEdSubtitle(e.target.value)}
                placeholder="副标题"
                className="rounded-lg border px-3 py-2 text-sm"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <input
                type="text"
                required
                value={edCron}
                onChange={(e) => setEdCron(e.target.value)}
                placeholder="Cron（如 0 6 * * * 每天早6点）"
                className="rounded-lg border px-3 py-2 text-sm font-mono"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
            </div>

            <div className="flex flex-wrap items-center gap-4 text-xs">
              <label className="flex items-center gap-1.5">
                <span>封面风格：</span>
                <select
                  value={edCoverStyle}
                  onChange={(e) => setEdCoverStyle(e.target.value as "editorial" | "dark" | "minimal")}
                  className="rounded border px-2 py-1"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                >
                  <option value="editorial">纸墨经典</option>
                  <option value="dark">暗夜黑金</option>
                  <option value="minimal">极简白底</option>
                </select>
              </label>
              <label className="flex items-center gap-1.5">
                <span>抓取窗口(小时)：</span>
                <input
                  type="number"
                  min={1}
                  max={168}
                  value={edLookback}
                  onChange={(e) => setEdLookback(Number(e.target.value))}
                  className="w-16 rounded border px-2 py-1"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={edAiBrief}
                  onChange={(e) => setEdAiBrief(e.target.checked)}
                />
                <span>生成 AI 今日头版综述</span>
              </label>
            </div>

            <div>
              <div className="text-xs font-medium mb-2" style={{ color: "var(--fg-secondary)" }}>
                勾选收录的订阅源：
              </div>
              {sources.length === 0 ? (
                <p className="text-xs" style={{ color: "var(--fg-muted)" }}>
                  尚无订阅源，请先前往「订阅与剪藏」添加源。
                </p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {sources.map((src) => {
                    const checked = selectedSourceIds.includes(src.id);
                    return (
                      <button
                        key={src.id}
                        type="button"
                        onClick={() =>
                          setSelectedSourceIds((prev) =>
                            checked ? prev.filter((id) => id !== src.id) : [...prev, src.id],
                          )
                        }
                        className="text-xs px-3 py-1.5 rounded-full border cursor-pointer"
                        style={{
                          backgroundColor: checked ? "var(--accent)" : "var(--bg-canvas)",
                          color: checked ? "var(--accent-fg)" : "var(--fg-secondary)",
                          borderColor: "var(--border-hairline)",
                        }}
                      >
                        {checked ? "✓ " : "+ "}
                        {src.name}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            <button
              type="submit"
              className="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-xs font-medium cursor-pointer"
              style={{ backgroundColor: "var(--fg-primary)", color: "var(--bg-canvas)" }}
            >
              <Plus className="w-3.5 h-3.5" />
              <span>创建定时刊物</span>
            </button>
          </form>
        </div>
      )}

      {tab === "deliveries" && (
        <div className="space-y-4">
          {summary && (
            <div className="flex gap-4 text-xs">
              <span
                className="px-3 py-1.5 rounded-lg border"
                style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
              >
                累计成功推送：<strong>{summary.total_sent}</strong>
              </span>
              <span
                className="px-3 py-1.5 rounded-lg border"
                style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
              >
                失败记录：<strong>{summary.total_failed}</strong>
              </span>
            </div>
          )}

          <div className="space-y-3">
            {deliveries.map((d) => (
              <div
                key={d.id}
                className="rounded-xl border p-4 flex flex-wrap items-center justify-between gap-3"
                style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm">{d.title}</span>
                    <span
                      className="text-[11px] px-2 py-0.5 rounded-full font-medium"
                      style={{
                        backgroundColor:
                          d.status === "sent"
                            ? "var(--fg-primary)"
                            : d.status === "failed"
                              ? "#991b1b"
                              : "var(--bg-subtle)",
                        color:
                          d.status === "sent" || d.status === "failed"
                            ? "#ffffff"
                            : "var(--fg-secondary)",
                      }}
                    >
                      {d.status}
                    </span>
                    <span className="text-xs" style={{ color: "var(--fg-muted)" }}>
                      {d.trigger} · {formatRelativeTime(d.created_at)}
                    </span>
                  </div>
                  <div className="text-xs mt-1" style={{ color: "var(--fg-secondary)" }}>
                    收件：{d.target_email || "仅归档"} · {d.article_count} 篇 ·{" "}
                    {formatBytes(d.file_size_bytes)} · 耗时 {(d.duration_ms / 1000).toFixed(1)}s
                  </div>
                  {d.error_message && (
                    <div className="text-xs text-red-700 mt-1">{d.error_message}</div>
                  )}
                </div>

                {d.status === "failed" && (
                  <button
                    type="button"
                    onClick={async () => {
                      await apiRequest(`/api/v1/deliveries/${d.id}/retry`, { method: "POST" });
                      notify("已重新加入投递队列", "success");
                    }}
                    className="inline-flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg border cursor-pointer"
                    style={{ borderColor: "var(--border-hairline)" }}
                  >
                    <RotateCw className="w-3.5 h-3.5" />
                    <span>重试</span>
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === "settings" && (
        <div className="space-y-6">
          {/* Deep infrastructure health */}
          {healthQuery.data && (
            <div
              className="rounded-2xl border p-5"
              style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
            >
              <h2 className="font-editorial text-xl font-semibold mb-3">基础设施与 AI-Ops 健康状态</h2>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                {[
                  { label: `PostgreSQL (${healthQuery.data.database.dialect ?? "-"})`, ok: healthQuery.data.database.ok },
                  { label: "Redis 队列/SSE", ok: healthQuery.data.redis.ok },
                  { label: "NewsNow Sidecar", ok: healthQuery.data.newsnow.ok },
                  { label: "RSSHub 内网", ok: healthQuery.data.rsshub.ok },
                  { label: "Byparr 反爬浏览器", ok: healthQuery.data.byparr.ok },
                  { label: "SMTP 发件凭据", ok: healthQuery.data.smtp_configured },
                  { label: "Kindle 收件邮箱", ok: healthQuery.data.kindle_email_configured },
                  { label: `AI (${healthQuery.data.ai_model ?? "未启用"})`, ok: healthQuery.data.ai_enabled },
                ].map((item) => (
                  <div
                    key={item.label}
                    className="rounded-lg border px-3 py-2 flex items-center gap-2"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  >
                    {item.ok ? (
                      <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
                    ) : (
                      <XCircle className="w-4 h-4 text-amber-600 shrink-0" />
                    )}
                    <span className="truncate">{item.label}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* SMTP + Kindle */}
          <form
            onSubmit={saveSmtp}
            className="rounded-2xl border p-6 space-y-4"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div className="flex items-center gap-2">
              <Mail className="w-5 h-5" style={{ color: "var(--accent)" }} />
              <h2 className="font-editorial text-xl font-semibold">SMTP 发件与 Kindle 邮箱</h2>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
              <input
                type="text"
                value={smtpHost}
                onChange={(e) => setSmtpHost(e.target.value)}
                placeholder="smtp.gmail.com"
                className="rounded-lg border px-3 py-2"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <input
                type="number"
                value={smtpPort}
                onChange={(e) => setSmtpPort(Number(e.target.value))}
                placeholder="465"
                className="rounded-lg border px-3 py-2"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <label className="flex items-center gap-2 text-xs px-2">
                <input
                  type="checkbox"
                  checked={smtpSsl}
                  onChange={(e) => setSmtpSsl(e.target.checked)}
                />
                <span>使用 SSL/TLS（465 端口勾选）</span>
              </label>
              <input
                type="email"
                value={smtpUser}
                onChange={(e) => setSmtpUser(e.target.value)}
                placeholder="发件账号（如 Gmail）"
                className="rounded-lg border px-3 py-2"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <input
                type="password"
                value={smtpPass}
                onChange={(e) => setSmtpPass(e.target.value)}
                placeholder={
                  settingsQuery.data?.smtp.password_set
                    ? "已设置（留空保持不变）"
                    : "应用专用密码 / SMTP 授权码"
                }
                className="rounded-lg border px-3 py-2"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <input
                type="email"
                value={kindleEmail}
                onChange={(e) => setKindleEmail(e.target.value)}
                placeholder="默认 Kindle 邮箱（xxx@kindle.com）"
                className="rounded-lg border px-3 py-2"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
            </div>
            <div className="flex items-center gap-2">
              <button
                type="submit"
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                保存 SMTP 配置
              </button>
              <button
                type="button"
                onClick={testSmtp}
                className="px-4 py-2 rounded-lg text-xs border cursor-pointer"
                style={{ borderColor: "var(--border-hairline)" }}
              >
                发送测试邮件
              </button>
            </div>
          </form>

          {/* AI (OpenAI / CPA compatible) */}
          <form
            onSubmit={saveAi}
            className="rounded-2xl border p-6 space-y-4"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Bot className="w-5 h-5" style={{ color: "var(--accent)" }} />
                <h2 className="font-editorial text-xl font-semibold">
                  AI 编排引擎（OpenAI / CPA 兼容）
                </h2>
              </div>
              <label className="flex items-center gap-2 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={aiEnabled}
                  onChange={(e) => setAiEnabled(e.target.checked)}
                />
                <span>启用 AI 综述 / 导读 / 翻译</span>
              </label>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
              <input
                type="url"
                value={aiBaseUrl}
                onChange={(e) => setAiBaseUrl(e.target.value)}
                placeholder="https://api.openai.com/v1"
                className="rounded-lg border px-3 py-2"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <input
                type="password"
                value={aiKey}
                onChange={(e) => setAiKey(e.target.value)}
                placeholder={
                  settingsQuery.data?.ai.api_key_set ? "API Key 已加密保存（留空不变）" : "sk-..."
                }
                className="rounded-lg border px-3 py-2"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <input
                type="text"
                value={aiModel}
                onChange={(e) => setAiModel(e.target.value)}
                placeholder="模型名称（如 gpt-5.4-mini / gemini-2.5-flash）"
                className="rounded-lg border px-3 py-2"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
            </div>
            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={aiProxy}
                  onChange={(e) => setAiProxy(e.target.checked)}
                />
                <span>请求 AI 接口时走外网代理（{netProxy || "未配置"}）</span>
              </label>
              <button
                type="submit"
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                保存 AI 配置
              </button>
              <button
                type="button"
                onClick={testAi}
                className="px-4 py-2 rounded-lg text-xs border cursor-pointer"
                style={{ borderColor: "var(--border-hairline)" }}
              >
                测试连通性
              </button>
            </div>
          </form>

          {/* MCP Server AI-Ops Tokens */}
          <div
            className="rounded-2xl border p-6 space-y-4"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div className="flex items-center gap-2">
              <KeyRound className="w-5 h-5" style={{ color: "var(--accent)" }} />
              <h2 className="font-editorial text-xl font-semibold">原生 MCP Server（AI 运维入口）</h2>
            </div>
            <p className="text-xs" style={{ color: "var(--fg-secondary)" }}>
              端点：<code className="px-1.5 py-0.5 rounded" style={{ backgroundColor: "var(--bg-subtle)" }}>
                POST {window.location.origin}/api/v1/mcp
              </code>
              （Streamable HTTP JSON-RPC 2.0，Bearer Token 鉴权）
            </p>

            <form onSubmit={createMcpToken} className="flex flex-wrap items-center gap-3">
              <input
                type="text"
                required
                value={mcpName}
                onChange={(e) => setMcpName(e.target.value)}
                placeholder="令牌名称（如 antigravity-ops）"
                className="rounded-lg border px-3 py-2 text-xs"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={mcpReadonly}
                  onChange={(e) => setMcpReadonly(e.target.checked)}
                />
                <span>只读权限</span>
              </label>
              <button
                type="submit"
                className="px-3.5 py-2 rounded-lg text-xs font-medium cursor-pointer"
                style={{ backgroundColor: "var(--fg-primary)", color: "var(--bg-canvas)" }}
              >
                签发新 Token
              </button>
            </form>

            {createdToken && (
              <div
                className="rounded-xl border p-4 text-xs space-y-2"
                style={{ backgroundColor: "var(--accent-soft)", borderColor: "var(--accent)" }}
              >
                <div className="font-semibold">新令牌仅显示一次，请立即复制：</div>
                <div className="flex items-center justify-between gap-2 font-mono break-all">
                  <span>{createdToken}</span>
                  <button
                    type="button"
                    onClick={() => {
                      navigator.clipboard.writeText(createdToken);
                      notify("已复制 Token", "success");
                    }}
                    className="p-1.5 rounded border cursor-pointer shrink-0"
                    style={{ borderColor: "var(--border-hairline)" }}
                  >
                    <Copy className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            )}

            <div className="space-y-2">
              {(mcpTokensQuery.data?.items ?? []).map((t) => (
                <div
                  key={t.id}
                  className="flex items-center justify-between rounded-lg border px-3.5 py-2 text-xs"
                  style={{ borderColor: "var(--border-hairline)" }}
                >
                  <div>
                    <span className="font-medium mr-2">{t.name}</span>
                    <span className="font-mono" style={{ color: "var(--fg-muted)" }}>
                      {t.token_prefix}…
                    </span>
                    <span className="ml-2 px-1.5 py-0.5 rounded" style={{ backgroundColor: "var(--bg-subtle)" }}>
                      {t.readonly ? "只读" : "读写"}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={async () => {
                      await apiRequest(`/api/v1/settings/mcp-tokens/${t.id}`, { method: "DELETE" });
                      await qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
                      notify("已吊销令牌", "success");
                    }}
                    className="text-red-700 cursor-pointer"
                  >
                    吊销
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
