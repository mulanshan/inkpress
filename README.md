# InkPress

> **Editorial Reading Website & Zero-E999 Kindle Publishing Studio**
>
> A modern self-hosted reading sanctuary and automated Kindle newspaper, magazine, and book publishing engine.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.13-3776AB.svg)
![React](https://img.shields.io/badge/react-19-61DAFB.svg)

## Features

- **Editorial Paper Reading Website**: Serene, typography-first web reader with 4 themes (**Paper**, **Sepia**, **Charcoal Dark**, **E-Ink**), keyboard navigation (`j`/`k`/`/`/`Esc`), built-in sanitized EPUB chapter reader, SSRF-hardened anti-hotlink media proxy, and ParadeDB BM25 (`pdb.jieba`) CJK/English full-text search.
- **NewsNow Sidecar Integration (40+ Live Sources)**: Runs `ghcr.io/ourongxing/newsnow` internally with `undici` `EnvHttpProxyAgent` proxy preload to bypass rate limits and reach overseas sources cleanly, supporting full-text extraction and structured digests.
- **RSS, RSSHub, Webpage & Webpage List Extraction**: Native `rsshub://` shortcut resolution, automatic full-text RSS detection, `webpage_list` CSS-selector scraping with optional Byparr headless browser fallback, per-source title regex include/exclude filters, direct/proxy split routing, and strict URL + content hash deduplication.
- **Optional AI Editorial Engine (3-Tier Illustrated Architecture)**: Works with any OpenAI-compatible gateway to compile daily issues into a **3-tier editorial flow**:
  1. **Anchor Full-Issue Chapters** (e.g. *喷嚏图卦* preserved intact and extracted as deduplication anchors)
  2. **AI Curated Illustrated Digest** (cross-source topic clustering, multi-source merging, and candidate-verified inline photo/chart curation)
  3. **Deep Long-Form Reads** (round-robin non-duplicate long-form essays with AI TL;DR cards and optional stacked bilingual translation)
  - Gracefully falls back to deterministic local clustering when AI is disabled.
- **Zero-E999 Kindle EPUB3 & E-Ink Publishing Engine**:
  - Mandatory UTF-8 XML declarations on every XHTML spine document (prevents Amazon Latin-1 mojibake)
  - Dual navigation: EPUB3 `nav.xhtml` (with `landmarks` & per-chapter reading time `（X 分钟）`) + EPUB2 `toc.ncx`
  - **Automatic Tall Infographic Slicing**: Long vertical charts/strips (`height > 2×width` and `> 1600px`) are sliced with `48px` overlap into crisp page-sized sequential plates for effortless Kindle reading
  - Optional e-ink grayscale conversion with automatic histogram contrast stretching (`autocontrast`) and QR source footers
  - Hyperlink stripping enabled by default (`strip_links = true`) to prevent accidental Kindle browser taps
  - Rust-powered `nh3` HTML sanitization + Pillow image optimization with domain-aware anti-hotlink `Referer` headers
  - Automatic multi-volume splitting (< 18 MB/vol) with volume-scoped navigation links and stateful multi-volume retry
- **Email-Free Direct E-Ink Ecosystem**:
  - **OPDS 1.2 Catalog (`/api/v1/opds`)**: Standard Atom OPDS navigation, acquisition, and OpenSearch feeds (HTTP Basic Auth / Token / Session) for KOReader, Onyx Boox, PocketBook, and Foliate
  - **`/kindle` Minimal Direct-Download Page**: Zero-JS, high-contrast black-on-white HTML bookshelf for one-tap EPUB downloads from Kindle Experimental Browser
- **Personal Book & Magazine Library**: Upload `EPUB`, `MOBI`, `AZW3`, `TXT`, `Markdown`, `HTML`, `DOCX`, `PDF`, `FB2`, `RTF`, or `CBZ` (streaming upload up to 100 MB) — automatic Kindle EPUB normalization/repair (with in-place `toc.ncx` injection), built-in web reading, immediate push, or scheduled delivery.
- **AI-Ops Native (MCP Server)**: Built-in Streamable HTTP `POST /api/v1/mcp` JSON-RPC 2.0 server with Bearer Token authentication, `Origin` verification, read-only toggle, and operational tools (`get_health`, `search_articles`, `clip_url`, `update_source`, `update_edition`, `trigger_edition_now`, `retry_delivery`, `validate_book_epub`, etc.).

## Quick Start (Docker Compose)

```bash
cp .env.example .env
# Edit .env: set a strong random APP_SECRET_KEY, POSTGRES_PASSWORD, etc.
docker compose -f deploy/compose.yaml up -d
```

Open `http://localhost:8060` to complete the 1-minute admin setup and configure your SMTP / Kindle email.

For environments with existing PostgreSQL (ParadeDB), Redis, RSSHub, and Byparr networks, copy [`deploy/compose.external-infra.yaml`](deploy/compose.external-infra.yaml) and [`deploy/newsnow-proxy-preload.mjs`](deploy/newsnow-proxy-preload.mjs) into your deployment directory alongside `.env`.

## Architecture

- **Backend**: Python 3.13, FastAPI, SQLAlchemy 2 (async `psycopg`), Alembic, ParadeDB `pg_search` (`pdb.jieba`), Redis Streams Worker (`XREADGROUP` / `XAUTOCLAIM`) & Scheduler, Trafilatura, `nh3`, Pillow, PyMuPDF, Pandoc, Calibre CLI (`ebook-convert`), `aiosmtplib`, `structlog`.
- **Frontend**: React 19, TypeScript 5.9, Vite 7, TanStack Query v5, Tailwind CSS v4, Lucide Icons.
- **License**: MIT
