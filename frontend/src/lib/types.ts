export type ReadingTheme = "paper" | "sepia" | "charcoal" | "eink";
export type ReadingFont = "serif" | "sans";

export interface UserPreferences {
  theme?: ReadingTheme;
  font_family?: ReadingFont;
  font_size?: number;
  line_height?: number;
}

export interface User {
  id: number;
  username: string;
  display_name: string;
  kindle_email: string;
  preferences: UserPreferences;
  created_at: string;
}

export type SourceKind = "newsnow" | "rss" | "rsshub" | "webpage" | "webpage_list";
export type FetchStrategy = "fulltext_rss" | "extract_web" | "newsnow_digest" | "byparr_browser";
export type ProxyMode = "auto" | "direct" | "proxy";

export type EditorialRole = "" | "anchor" | "digest" | "deep";

export interface SourceAiOptions {
  summarize?: boolean;
  bilingual?: boolean;
  editorial_role?: EditorialRole | null;
  title_include_regex?: string | null;
  title_exclude_regex?: string | null;
  link_selector?: string | null;
  content_selector?: string | null;
  remove_selectors?: string[] | null;
  [key: string]: unknown;
}

export interface SourceItem {
  id: number;
  name: string;
  kind: SourceKind;
  identifier: string;
  category: string;
  fetch_strategy: FetchStrategy;
  use_proxy: ProxyMode;
  ai_options: SourceAiOptions;
  max_items: number;
  enabled: boolean;
  last_fetched_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  article_count: number;
  created_at: string;
}

export interface NewsNowCatalogItem {
  id: string;
  name: string;
  category: string;
  feed_type: "realtime" | "hottest";
  default_strategy: "extract_web" | "newsnow_digest";
  language: string;
  description: string;
  subscribed: boolean;
}

export interface RssPreset {
  name: string;
  kind: "rss" | "rsshub" | "webpage" | "webpage_list";
  identifier: string;
  category: string;
  fetch_strategy: "fulltext_rss" | "extract_web" | "byparr_browser";
  use_proxy: ProxyMode;
  ai_options?: SourceAiOptions;
  description: string;
}

export interface ArticleSummary {
  id: number;
  source_id: number | null;
  source_name: string;
  category: string;
  canonical_url: string;
  title: string;
  translated_title: string | null;
  author: string;
  language: string;
  published_at: string;
  fetched_at: string;
  lead_image_url: string | null;
  excerpt: string;
  ai_summary: string | null;
  has_bilingual: boolean;
  word_count: number;
  reading_time_min: number;
  is_read: boolean;
  is_starred: boolean;
}

export interface ArticleDetail extends ArticleSummary {
  clean_html: string;
  bilingual_html: string | null;
  plain_text: string;
}

export interface EditionSourceLink {
  source_id: number;
  sort_order: number;
  max_items_override: number | null;
  source_name: string;
  source_kind: string;
  source_category: string;
}

export interface EditionItem {
  id: number;
  name: string;
  subtitle: string;
  cover_style: "editorial" | "dark" | "minimal";
  cron_expr: string;
  timezone: string;
  lookback_hours: number;
  dedup_window_days: number;
  max_articles: number;
  max_mb_per_volume: number;
  grayscale_images: boolean;
  include_qr_links: boolean;
  strip_links?: boolean;
  retention_days?: number;
  ai_briefing_enabled: boolean;
  target_emails: string;
  issue_counter: number;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  sources: EditionSourceLink[];
}

export type ExportTargetFormat = "epub" | "pdf" | "mobi" | "txt";

export interface BookExportInfo {
  key: string;
  target_format: ExportTargetFormat | string;
  max_mb: number | null;
  status: "ready" | "converting" | "failed";
  size_bytes: number;
  notes: string;
  error: string | null;
  updated_at: string | null;
}

export interface BookItem {
  id: number;
  kind: "edition_issue" | "uploaded_book" | "magazine" | "web_clip";
  edition_id: number | null;
  title: string;
  author: string;
  description: string;
  language: string;
  original_format: string;
  active_format?: string;
  has_original?: boolean;
  original_size_bytes?: number;
  file_size_bytes: number;
  conversion_status: "ready" | "converting" | "failed";
  conversion_error: string | null;
  metadata: Record<string, unknown>;
  exports?: Record<string, BookExportInfo>;
  has_cover: boolean;
  can_read_online: boolean;
  scheduled_push_at: string | null;
  pushed_count: number;
  last_pushed_at: string | null;
  created_at: string;
}

export interface BookExportResponse {
  status: "ready" | "converting" | "failed";
  export_key: string;
  export?: BookExportInfo;
  download_url: string;
  item: BookItem;
}

export interface DeliveryItem {
  id: number;
  edition_id: number | null;
  book_id: number | null;
  title: string;
  trigger: string;
  target_email: string;
  status: "pending" | "running" | "sending_smtp" | "generated" | "sent" | "skipped" | "failed";
  article_count: number;
  volume_count: number;
  file_size_bytes: number;
  duration_ms: number;
  stage_timings: Record<string, number>;
  error_message: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface SettingsPayload {
  smtp: {
    host: string;
    port: number;
    use_ssl: boolean;
    username: string;
    password_set: boolean;
    from_email: string;
    default_kindle_email: string;
  };
  ai: {
    enabled: boolean;
    base_url: string;
    api_key_set: boolean;
    model: string;
    use_proxy: boolean;
    max_concurrency: number;
    timeout_seconds: number;
  };
  network: {
    outbound_proxy: string;
    newsnow_base_url: string;
    rsshub_base_url: string;
    byparr_base_url: string;
  };
}

export interface McpTokenItem {
  id: number;
  name: string;
  token_prefix: string;
  readonly: boolean;
  enabled: boolean;
  last_used_at: string | null;
  created_at: string;
}

export interface DeepHealth {
  version: string;
  database: { ok: boolean; dialect?: string; error?: string };
  redis: { ok: boolean; error?: string };
  worker?: { ok: boolean; queue_depth: number; last_seen: string | null };
  storage?: { free_gb: number };
  newsnow: { ok: boolean; http_status?: number; error?: string };
  rsshub: { ok: boolean; http_status?: number; error?: string };
  byparr: { ok: boolean; http_status?: number; error?: string };
  smtp_configured: boolean;
  kindle_email_configured: boolean;
  ai_enabled: boolean;
  ai_model: string | null;
  tools: { calibre_ebook_convert: boolean; pandoc: boolean };
}
