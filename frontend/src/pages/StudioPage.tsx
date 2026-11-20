import React, { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  Calendar,
  CheckCircle2,
  Copy,
  Globe,
  KeyRound,
  Lock,
  Mail,
  Pencil,
  Play,
  Plus,
  Power,
  RefreshCw,
  RotateCw,
  Settings2,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import { apiRequest, copyToClipboard, formatBytes, formatRelativeTime } from "../lib/api";
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
type CoverStyle = "editorial" | "dark" | "minimal";

export function StudioPage() {
  const qc = useQueryClient();
  const { notify } = useToast();
  const [tab, setTab] = useState<StudioTab>("editions");
  const [liveBanner, setLiveBanner] = useState<string>("");

  // New edition form
  const [edName, setEdName] = useState("每日晨报");
  const [edSubtitle, setEdSubtitle] = useState("每日精选热点与深度长文");
  const [edCron, setEdCron] = useState("0 6 * * *");
  const [edCoverStyle, setEdCoverStyle] = useState<CoverStyle>("editorial");
  const [edLookback, setEdLookback] = useState(24);
  const [edRetentionDays, setEdRetentionDays] = useState(30);
  const [edStripLinks, setEdStripLinks] = useState(true);
  const [edAiBrief, setEdAiBrief] = useState(true);
  const [selectedSourceIds, setSelectedSourceIds] = useState<number[]>([]);

  // Edit edition modal state
  const [editingEdition, setEditingEdition] = useState<EditionItem | null>(null);
  const [editEdName, setEditEdName] = useState("");
  const [editEdSubtitle, setEditEdSubtitle] = useState("");
  const [editEdCron, setEditEdCron] = useState("0 6 * * *");
  const [editEdCoverStyle, setEditEdCoverStyle] = useState<CoverStyle>("editorial");
  const [editEdLookback, setEditEdLookback] = useState(24);
  const [editEdDedupDays, setEditEdDedupDays] = useState(14);
  const [editEdMaxArticles, setEditEdMaxArticles] = useState(40);
  const [editEdRetentionDays, setEditEdRetentionDays] = useState(30);
  const [editEdStripLinks, setEditEdStripLinks] = useState(true);
  const [editEdAiBrief, setEditEdAiBrief] = useState(true);
  const [editEdEnabled, setEditEdEnabled] = useState(true);
  const [editEdTargets, setEditEdTargets] = useState("");
  const [editEdSourceIds, setEditEdSourceIds] = useState<number[]>([]);
  const [editEdSaving, setEditEdSaving] = useState(false);

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
  const [netNewsnowUrl, setNetNewsnowUrl] = useState("");
  const [netRsshubUrl, setNetRsshubUrl] = useState("");
  const [netByparrUrl, setNetByparrUrl] = useState("");

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordSaving, setPasswordSaving] = useState(false);

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
    setNetNewsnowUrl(s.network.newsnow_base_url);
    setNetRsshubUrl(s.network.rsshub_base_url);
    setNetByparrUrl(s.network.byparr_base_url);
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

  const openEditEdition = (ed: EditionItem) => {
    setEditingEdition(ed);
    setEditEdName(ed.name);
    setEditEdSubtitle(ed.subtitle);
    setEditEdCron(ed.cron_expr);
    setEditEdCoverStyle(ed.cover_style);
    setEditEdLookback(ed.lookback_hours);
    setEditEdDedupDays(ed.dedup_window_days);
    setEditEdMaxArticles(ed.max_articles);
    setEditEdRetentionDays(ed.retention_days ?? 30);
    setEditEdStripLinks(ed.strip_links ?? true);
    setEditEdAiBrief(ed.ai_briefing_enabled);
    setEditEdEnabled(ed.enabled);
    setEditEdTargets(ed.target_emails);
    setEditEdSourceIds(ed.sources.map((s) => s.source_id));
  };

  const toggleEditionEnabled = async (ed: EditionItem) => {
    try {
      await apiRequest(`/api/v1/editions/${ed.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !ed.enabled }),
      });
      await qc.invalidateQueries({ queryKey: ["editions"] });
      notify(
        !ed.enabled
          ? `已开启《${ed.name}》定时推送`
          : `已暂停《${ed.name}》定时推送`,
        "success",
      );
    } catch (err) {
      notify(err instanceof Error ? err.message : "切换失败", "error");
    }
  };

  const saveEditedEdition = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingEdition) return;
    if (editEdSourceIds.length === 0) {
      notify("请至少保留 1 个订阅源", "error");
      return;
    }
    setEditEdSaving(true);
    try {
      await apiRequest(`/api/v1/editions/${editingEdition.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: editEdName.trim(),
          subtitle: editEdSubtitle.trim(),
          cron_expr: editEdCron.trim(),
          cover_style: editEdCoverStyle,
          lookback_hours: editEdLookback,
          dedup_window_days: editEdDedupDays,
          max_articles: editEdMaxArticles,
          retention_days: editEdRetentionDays,
          strip_links: editEdStripLinks,
          ai_briefing_enabled: editEdAiBrief,
          enabled: editEdEnabled,
          target_emails: editEdTargets.trim(),
          sources: editEdSourceIds.map((id, idx) => ({ source_id: id, sort_order: idx })),
        }),
      });
      setEditingEdition(null);
      await qc.invalidateQueries({ queryKey: ["editions"] });
      notify("刊物配置已更新", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setEditEdSaving(false);
    }
  };

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
          retention_days: edRetentionDays,
          strip_links: edStripLinks,
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
    try {
      await apiRequest(`/api/v1/editions/${ed.id}`, { method: "DELETE" });
      await qc.invalidateQueries({ queryKey: ["editions"] });
      notify("已删除刊物", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "删除失败", "error");
    }
  };

  const saveSmtp = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await apiRequest("/api/v1/settings/smtp", {
        method: "PUT",
        body: JSON.stringify({
          host: smtpHost.trim(),
          port: smtpPort,
          use_ssl: smtpSsl,
          username: smtpUser.trim(),
          password: smtpPass || undefined,
          from_email: smtpFrom.trim(),
          default_kindle_email: kindleEmail.trim(),
        }),
      });
      setSmtpPass("");
      await qc.invalidateQueries({ queryKey: ["settings"] });
      await qc.invalidateQueries({ queryKey: ["health"] });
      notify("SMTP 与 Kindle 邮箱配置已加密保存", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存失败", "error");
    }
  };

  const testSmtp = async () => {
    try {
      await apiRequest("/api/v1/settings/smtp/test", {
        method: "POST",
        body: JSON.stringify({ target_email: smtpFrom.trim() || smtpUser.trim() }),
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
          base_url: aiBaseUrl.trim(),
          api_key: aiKey || undefined,
          model: aiModel.trim(),
          use_proxy: aiProxy,
        }),
      });
      setAiKey("");
      await qc.invalidateQueries({ queryKey: ["settings"] });
      await qc.invalidateQueries({ queryKey: ["health"] });
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

  const saveNetwork = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await apiRequest("/api/v1/settings/network", {
        method: "PUT",
        body: JSON.stringify({
          outbound_proxy: netProxy.trim(),
          newsnow_base_url: netNewsnowUrl.trim(),
          rsshub_base_url: netRsshubUrl.trim(),
          byparr_base_url: netByparrUrl.trim(),
        }),
      });
      await qc.invalidateQueries({ queryKey: ["settings"] });
      await qc.invalidateQueries({ queryKey: ["health"] });
      notify("网络与代理配置已保存", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存网络配置失败", "error");
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPassword.length < 8) {
      notify("新密码至少需要 8 位字符", "error");
      return;
    }
    if (newPassword !== confirmPassword) {
      notify("两次输入的新密码不一致", "error");
      return;
    }
    setPasswordSaving(true);
    try {
      await apiRequest("/api/v1/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      notify("登录密码已更新", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "修改密码失败", "error");
    } finally {
      setPasswordSaving(false);
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

  const handleCopyToken = async (token: string) => {
    const ok = await copyToClipboard(token);
    if (ok) {
      notify("已复制 Token", "success");
    } else {
      notify("自动复制受限，请手动选中上方令牌复制", "error");
    }
  };

  const editions = editionsQuery.data?.items ?? [];
  const sources = sourcesQuery.data?.items ?? [];
  const deliveries = deliveriesQuery.data?.items ?? [];
  const summary = deliveriesQuery.data?.summary;
  const health = healthQuery.data;

  const healthCards: { label: string; sub?: string; ok: boolean }[] = health
    ? [
        {
          label: `PostgreSQL (${health.database.dialect ?? "-"})`,
          ok: health.database.ok,
        },
        { label: "Redis 队列/SSE", ok: health.redis.ok },
        {
          label: "Worker 后台进程",
          sub: health.worker
            ? `队列 ${health.worker.queue_depth} · ${health.worker.last_seen ? formatRelativeTime(health.worker.last_seen) : "无心跳"}`
            : undefined,
          ok: health.worker ? health.worker.ok : false,
        },
        {
          label: "Calibre 转码引擎",
          sub: health.tools.calibre_ebook_convert ? "ebook-convert 就绪" : "不可用",
          ok: health.tools.calibre_ebook_convert,
        },
        {
          label: "Pandoc 排版引擎",
          sub: health.tools.pandoc ? "DOCX/MD 就绪" : "不可用",
          ok: health.tools.pandoc,
        },
        {
          label: "存储空间余量",
          sub:
            typeof health.storage?.free_gb === "number"
              ? `${health.storage.free_gb.toFixed(1)} GB 可用`
              : "未上报",
          ok: typeof health.storage?.free_gb === "number" ? health.storage.free_gb >= 2 : false,
        },
        { label: "NewsNow Sidecar", ok: health.newsnow.ok },
        { label: "RSSHub 内网", ok: health.rsshub.ok },
        { label: "Byparr 反爬浏览器", ok: health.byparr.ok },
        { label: "SMTP 发件凭据", ok: health.smtp_configured },
        { label: "Kindle 收件邮箱", ok: health.kindle_email_configured },
        { label: `AI (${health.ai_model ?? "未启用"})`, ok: health.ai_enabled },
      ]
    : [];

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
            { id: "settings" as const, label: "SMTP / AI / 网络 / 安全设置", icon: Settings2 },
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
                    <div className="min-w-0">
                      <h3 className="font-editorial text-2xl font-semibold truncate">{ed.name}</h3>
                      <p className="text-xs mt-0.5" style={{ color: "var(--fg-secondary)" }}>
                        {ed.subtitle} · 第 {ed.issue_counter} 期
                      </p>
                    </div>
                    <div className="flex flex-col items-end gap-1.5 shrink-0">
                      <button
                        type="button"
                        onClick={() => toggleEditionEnabled(ed)}
                        className="inline-flex items-center gap-1 text-[11px] font-medium px-2.5 py-1 rounded-full border cursor-pointer"
                        style={{
                          backgroundColor: ed.enabled ? "var(--accent-soft)" : "var(--bg-subtle)",
                          color: ed.enabled ? "var(--accent)" : "var(--fg-muted)",
                          borderColor: ed.enabled ? "var(--accent)" : "var(--border-hairline)",
                        }}
                        title={ed.enabled ? "点击暂停定时推送" : "点击开启定时推送"}
                      >
                        <Power className="w-3 h-3" />
                        <span>{ed.enabled ? "定时已开启" : "定时已暂停"}</span>
                      </button>
                      <span
                        className="text-[11px] font-mono px-2 py-0.5 rounded-full"
                        style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-secondary)" }}
                      >
                        {ed.cron_expr}
                      </span>
                    </div>
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
                      窗口：{ed.lookback_hours}h · 去重：{ed.dedup_window_days}天 · 上限：
                      {ed.max_articles}篇 / {ed.max_mb_per_volume}MB · 留存：
                      {ed.retention_days ?? 30}天
                    </div>
                    <div>
                      {ed.ai_briefing_enabled ? "含 AI 头版综述" : "无 AI 综述"} ·{" "}
                      {ed.strip_links !== false ? "剥离正文超链接（防误触）" : "保留正文超链接"}
                    </div>
                    {ed.enabled && ed.next_run_at && (
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
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => openEditEdition(ed)}
                      className="p-1.5 rounded-lg border cursor-pointer"
                      style={{
                        borderColor: "var(--border-hairline)",
                        color: "var(--fg-secondary)",
                      }}
                      title="编辑刊物"
                    >
                      <Pencil className="w-3.5 h-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => deleteEdition(ed)}
                      className="p-1.5 rounded-lg cursor-pointer"
                      style={{ color: "var(--fg-muted)" }}
                      title="删除刊物"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
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
                  onChange={(e) => setEdCoverStyle(e.target.value as CoverStyle)}
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
              <label className="flex items-center gap-1.5">
                <span>留存(天)：</span>
                <input
                  type="number"
                  min={1}
                  max={365}
                  value={edRetentionDays}
                  onChange={(e) => setEdRetentionDays(Number(e.target.value))}
                  className="w-16 rounded border px-2 py-1"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={edStripLinks}
                  onChange={(e) => setEdStripLinks(e.target.checked)}
                />
                <span>剥离正文超链接（防 Kindle 误触）</span>
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
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-sm">{d.title}</span>
                    <span
                      className="text-[11px] px-2 py-0.5 rounded-full font-medium border"
                      style={{
                        backgroundColor:
                          d.status === "sent"
                            ? "var(--fg-primary)"
                            : d.status === "failed"
                              ? "var(--bg-danger-soft)"
                              : "var(--bg-subtle)",
                        color:
                          d.status === "sent"
                            ? "var(--bg-canvas)"
                            : d.status === "failed"
                              ? "var(--fg-danger)"
                              : "var(--fg-secondary)",
                        borderColor:
                          d.status === "failed" ? "var(--border-danger)" : "transparent",
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
                    <div className="text-xs mt-1" style={{ color: "var(--fg-danger)" }}>
                      {d.error_message}
                    </div>
                  )}
                </div>

                {d.status === "failed" && (
                  <button
                    type="button"
                    onClick={async () => {
                      try {
                        await apiRequest(`/api/v1/deliveries/${d.id}/retry`, { method: "POST" });
                        notify("已重新加入投递队列", "success");
                      } catch (err) {
                        notify(err instanceof Error ? err.message : "重试失败", "error");
                      }
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
          {health && (
            <div
              className="rounded-2xl border p-5"
              style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
            >
              <div className="flex items-center justify-between gap-2 mb-3">
                <h2 className="font-editorial text-xl font-semibold">基础设施与 AI-Ops 健康状态</h2>
                <button
                  type="button"
                  onClick={() => healthQuery.refetch()}
                  className="inline-flex items-center gap-1 text-xs px-2.5 py-1 rounded-lg border cursor-pointer"
                  style={{ borderColor: "var(--border-hairline)", color: "var(--fg-secondary)" }}
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${healthQuery.isFetching ? "animate-spin" : ""}`} />
                  <span>刷新探针</span>
                </button>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
                {healthCards.map((item) => (
                  <div
                    key={item.label}
                    className="rounded-lg border px-3 py-2.5 flex items-start gap-2"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  >
                    {item.ok ? (
                      <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0 mt-0.5" />
                    ) : (
                      <XCircle
                        className="w-4 h-4 shrink-0 mt-0.5"
                        style={{ color: "var(--fg-danger)" }}
                      />
                    )}
                    <div className="min-w-0">
                      <div className="truncate font-medium">{item.label}</div>
                      {item.sub && (
                        <div className="text-[11px] truncate mt-0.5" style={{ color: "var(--fg-muted)" }}>
                          {item.sub}
                        </div>
                      )}
                    </div>
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
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  SMTP 服务器
                </label>
                <input
                  type="text"
                  value={smtpHost}
                  onChange={(e) => setSmtpHost(e.target.value)}
                  placeholder="smtp.gmail.com"
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  端口
                </label>
                <input
                  type="number"
                  value={smtpPort}
                  onChange={(e) => setSmtpPort(Number(e.target.value))}
                  placeholder="465"
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div className="flex items-end pb-2">
                <label className="flex items-center gap-2 text-xs cursor-pointer">
                  <input
                    type="checkbox"
                    checked={smtpSsl}
                    onChange={(e) => setSmtpSsl(e.target.checked)}
                  />
                  <span>使用 SSL/TLS（465 端口勾选）</span>
                </label>
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  SMTP 认证账号
                </label>
                <input
                  type="text"
                  value={smtpUser}
                  onChange={(e) => setSmtpUser(e.target.value)}
                  placeholder="发件认证用户名或邮箱"
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  SMTP 密码 / 授权码
                </label>
                <input
                  type="password"
                  value={smtpPass}
                  onChange={(e) => setSmtpPass(e.target.value)}
                  placeholder={
                    settingsQuery.data?.smtp.password_set
                      ? "已设置（留空保持不变）"
                      : "应用专用密码 / SMTP 授权码"
                  }
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  发件人邮箱（From）
                </label>
                <input
                  type="text"
                  value={smtpFrom}
                  onChange={(e) => setSmtpFrom(e.target.value)}
                  placeholder="sender@example.com（留空默认同账号）"
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div className="sm:col-span-3">
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  默认 Kindle 收件邮箱（支持英文逗号分隔多台 Kindle 设备）
                </label>
                <input
                  type="text"
                  value={kindleEmail}
                  onChange={(e) => setKindleEmail(e.target.value)}
                  placeholder="your_device@kindle.com, second_device@kindle.com"
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
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
            <div className="flex flex-wrap items-center justify-between gap-2">
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

          {/* Network & Proxy Settings */}
          <form
            onSubmit={saveNetwork}
            className="rounded-2xl border p-6 space-y-4"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div className="flex items-center gap-2">
              <Globe className="w-5 h-5" style={{ color: "var(--accent)" }} />
              <h2 className="font-editorial text-xl font-semibold">网络与代理设置</h2>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  外网代理地址（Outbound Proxy）
                </label>
                <input
                  type="text"
                  value={netProxy}
                  onChange={(e) => setNetProxy(e.target.value)}
                  placeholder="http://127.0.0.1:7890"
                  className="w-full rounded-lg border px-3 py-2 font-mono text-xs"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  NewsNow Sidecar 地址
                </label>
                <input
                  type="text"
                  value={netNewsnowUrl}
                  onChange={(e) => setNetNewsnowUrl(e.target.value)}
                  placeholder="http://newsnow:4444"
                  className="w-full rounded-lg border px-3 py-2 font-mono text-xs"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  RSSHub 内网地址
                </label>
                <input
                  type="text"
                  value={netRsshubUrl}
                  onChange={(e) => setNetRsshubUrl(e.target.value)}
                  placeholder="http://rsshub:1200"
                  className="w-full rounded-lg border px-3 py-2 font-mono text-xs"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  Byparr 反爬浏览器地址
                </label>
                <input
                  type="text"
                  value={netByparrUrl}
                  onChange={(e) => setNetByparrUrl(e.target.value)}
                  placeholder="http://byparr:8191"
                  className="w-full rounded-lg border px-3 py-2 font-mono text-xs"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
            </div>
            <div>
              <button
                type="submit"
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                保存网络设置
              </button>
            </div>
          </form>

          {/* Change Login Password */}
          <form
            onSubmit={handleChangePassword}
            className="rounded-2xl border p-6 space-y-4"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div className="flex items-center gap-2">
              <Lock className="w-5 h-5" style={{ color: "var(--accent)" }} />
              <h2 className="font-editorial text-xl font-semibold">修改登录密码</h2>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  当前密码
                </label>
                <input
                  type="password"
                  required
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  placeholder="输入当前密码"
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  新密码（至少 8 位）
                </label>
                <input
                  type="password"
                  required
                  minLength={8}
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  placeholder="至少 8 位字符"
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: "var(--fg-secondary)" }}>
                  确认新密码
                </label>
                <input
                  type="password"
                  required
                  minLength={8}
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="再次输入新密码"
                  className="w-full rounded-lg border px-3 py-2"
                  style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
                />
              </div>
            </div>
            <div>
              <button
                type="submit"
                disabled={passwordSaving}
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-50"
                style={{ backgroundColor: "var(--fg-primary)", color: "var(--bg-canvas)" }}
              >
                {passwordSaving ? "提交中…" : "更新登录密码"}
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
              端点：
              <code className="px-1.5 py-0.5 rounded" style={{ backgroundColor: "var(--bg-subtle)" }}>
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
                    onClick={() => handleCopyToken(createdToken)}
                    className="p-1.5 rounded border cursor-pointer shrink-0"
                    style={{ borderColor: "var(--border-hairline)" }}
                    title="复制 Token"
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
                    <span
                      className="ml-2 px-1.5 py-0.5 rounded"
                      style={{ backgroundColor: "var(--bg-subtle)" }}
                    >
                      {t.readonly ? "只读" : "读写"}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={async () => {
                      try {
                        await apiRequest(`/api/v1/settings/mcp-tokens/${t.id}`, { method: "DELETE" });
                        await qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
                        notify("已吊销令牌", "success");
                      } catch (err) {
                        notify(err instanceof Error ? err.message : "吊销失败", "error");
                      }
                    }}
                    className="cursor-pointer font-medium hover:underline"
                    style={{ color: "var(--fg-danger)" }}
                  >
                    吊销
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Edit Edition Modal */}
      {editingEdition && (
        <div className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center p-4">
          <form
            onSubmit={saveEditedEdition}
            className="max-w-2xl w-full max-h-[88vh] flex flex-col rounded-2xl border overflow-hidden"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div
              className="px-6 py-4 border-b flex items-center justify-between"
              style={{ borderColor: "var(--border-hairline)" }}
            >
              <h3 className="font-editorial text-xl font-semibold">
                编辑刊物 · {editingEdition.name}
              </h3>
              <button
                type="button"
                onClick={() => setEditingEdition(null)}
                className="p-1 cursor-pointer"
                style={{ color: "var(--fg-muted)" }}
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-6 overflow-y-auto space-y-4 text-xs">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    刊物名称
                  </label>
                  <input
                    type="text"
                    required
                    value={editEdName}
                    onChange={(e) => setEditEdName(e.target.value)}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    副标题
                  </label>
                  <input
                    type="text"
                    value={editEdSubtitle}
                    onChange={(e) => setEditEdSubtitle(e.target.value)}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    Cron 定时表达式
                  </label>
                  <input
                    type="text"
                    required
                    value={editEdCron}
                    onChange={(e) => setEditEdCron(e.target.value)}
                    className="w-full rounded-lg border px-3 py-2 font-mono text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    封面风格
                  </label>
                  <select
                    value={editEdCoverStyle}
                    onChange={(e) => setEditEdCoverStyle(e.target.value as CoverStyle)}
                    className="w-full rounded-lg border px-2.5 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  >
                    <option value="editorial">纸墨经典</option>
                    <option value="dark">暗夜黑金</option>
                    <option value="minimal">极简白底</option>
                  </select>
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    抓取窗口（小时）
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={168}
                    value={editEdLookback}
                    onChange={(e) => setEditEdLookback(Number(e.target.value))}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    去重窗口（天）
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={90}
                    value={editEdDedupDays}
                    onChange={(e) => setEditEdDedupDays(Number(e.target.value))}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    单期最大篇数
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={120}
                    value={editEdMaxArticles}
                    onChange={(e) => setEditEdMaxArticles(Number(e.target.value))}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    归档留存（天）
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={365}
                    value={editEdRetentionDays}
                    onChange={(e) => setEditEdRetentionDays(Number(e.target.value))}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
              </div>

              <div>
                <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                  专属收件邮箱（留空则使用默认 Kindle 邮箱，多设备用逗号分隔）
                </label>
                <input
                  type="text"
                  value={editEdTargets}
                  onChange={(e) => setEditEdTargets(e.target.value)}
                  placeholder="your_device@kindle.com"
                  className="w-full rounded-lg border px-3 py-2 text-sm"
                  style={{
                    backgroundColor: "var(--bg-canvas)",
                    borderColor: "var(--border-hairline)",
                  }}
                />
              </div>

              <div className="flex flex-wrap gap-4 pt-1">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={editEdEnabled}
                    onChange={(e) => setEditEdEnabled(e.target.checked)}
                  />
                  <span>启用定时自动推送</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={editEdStripLinks}
                    onChange={(e) => setEditEdStripLinks(e.target.checked)}
                  />
                  <span>剥离正文超链接（防 Kindle 误触）</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={editEdAiBrief}
                    onChange={(e) => setEditEdAiBrief(e.target.checked)}
                  />
                  <span>生成 AI 今日头版综述</span>
                </label>
              </div>

              <div>
                <div className="font-medium mb-2" style={{ color: "var(--fg-secondary)" }}>
                  绑定订阅源（已选 {editEdSourceIds.length} 个）：
                </div>
                <div className="flex flex-wrap gap-2">
                  {sources.map((src) => {
                    const checked = editEdSourceIds.includes(src.id);
                    return (
                      <button
                        key={src.id}
                        type="button"
                        onClick={() =>
                          setEditEdSourceIds((prev) =>
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
              </div>
            </div>

            <div
              className="px-6 py-4 border-t flex justify-end gap-2"
              style={{ borderColor: "var(--border-hairline)" }}
            >
              <button
                type="button"
                onClick={() => setEditingEdition(null)}
                className="px-4 py-2 rounded-lg text-xs border cursor-pointer"
                style={{ borderColor: "var(--border-hairline)" }}
              >
                取消
              </button>
              <button
                type="submit"
                disabled={editEdSaving}
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-50"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                {editEdSaving ? "保存中…" : "保存刊物配置"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
