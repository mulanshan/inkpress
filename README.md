# InkPress

> **Editorial Reading Website & Zero-E999 Kindle Publishing Studio**
>
> A modern self-hosted reading sanctuary and automated Kindle newspaper, magazine, and book publishing engine.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.13-3776AB.svg)
![React](https://img.shields.io/badge/react-19-61DAFB.svg)

## Features

- **Editorial Paper Reading Website**: Serene, typography-first web reader with 4 themes (**Paper**, **Sepia**, **Charcoal Dark**, **E-Ink**), keyboard navigation (`j`/`k`/`o`/`/`), built-in chapter EPUB reader, and ParadeDB BM25 full-text search.
- **NewsNow Sidecar Integration (46+ Live Sources)**: Runs `ghcr.io/ourongxing/newsnow` internally to bypass public rate limits and Cloudflare blocks, supporting smart full-text extraction for articles and structured digests for real-time tickers.
- **RSS, RSSHub & Smart Anti-Bot Fetching**: Native `rsshub://` shortcut resolution, automatic full-text RSS detection, direct/proxy split routing, optional FlareSolverr/Byparr headless fallback, and strict URL + content hash deduplication.
- **Optional AI Editorial Engine (OpenAI-Compatible)**: Works with any OpenAI-compatible gateway to generate daily frontpage executive briefings, per-article TL;DR cards, and paragraph-by-paragraph bilingual translations. Gracefully degrades when disabled.
- **Zero-E999 Kindle EPUB3 Publishing Engine**:
  - Mandatory UTF-8 XML declarations on every XHTML spine document (prevents Amazon Latin-1 mojibake)
  - Dual navigation: EPUB3 `nav.xhtml` + EPUB2 `toc.ncx` with heading-anchored TOCs
  - Rust-powered `nh3` HTML sanitization + Pillow image optimization (JPEG/resize/optional grayscale)
  - Automatic multi-volume splitting (< 18 MB/vol) to stay safely under Gmail & Send-to-Kindle caps
  - Dynamic CJK magazine cover generation & built-in structural EPUB validator
- **Personal Book & Magazine Library**: Upload `EPUB`, `MOBI`, `AZW3`, `TXT`, `Markdown`, `HTML`, `DOCX`, `PDF`, `FB2`, or `CBZ` — automatic Kindle EPUB normalization/repair, built-in web reading, immediate push, or scheduled delivery.
- **AI-Ops Native (MCP Server)**: Built-in Streamable HTTP `POST /api/v1/mcp` JSON-RPC 2.0 server with Bearer Token authentication and read-only toggle for AI-assisted operations and reading workflows.

## Quick Start (Docker Compose)

```bash
cp .env.example .env
# Edit .env: set APP_SECRET_KEY, POSTGRES_PASSWORD, etc.
docker compose -f deploy/compose.yaml up -d
```

Open `http://localhost:8060` to complete the 1-minute admin setup and configure your SMTP / Kindle email.

For environments with existing PostgreSQL (ParadeDB), Redis, RSSHub, and Byparr networks, use [`deploy/compose.external-infra.yaml`](deploy/compose.external-infra.yaml).

## Architecture

- **Backend**: Python 3.13, FastAPI, SQLAlchemy 2 (async `psycopg`), Alembic, Taskiq + Redis, Trafilatura, `nh3`, EbookLib, Pillow, PyMuPDF, Pandoc, Calibre CLI (`ebook-convert`), `aiosmtplib`, `structlog`.
- **Frontend**: React 19, TypeScript 5.9, Vite 7, TanStack Query v5, Tailwind CSS v4, Lucide Icons.
- **License**: MIT
