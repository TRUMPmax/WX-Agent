# Changelog

## v1.0.0 - 2026-02-26

Current functionality:
- WeChat public account callback service (`GET /wechat`, `POST /wechat`) with plaintext and encrypted mode signature verification.
- Local LLM reply via Ollama, including chat reply generation for user questions.
- RAG knowledge base using SQLite with chunking and retrieval.
- Knowledge base file upload API (`POST /kb/upload`) for admin.
- Folder-based KB incremental sync (`POST /kb/sync`), sync status (`GET /kb/sync/status`), and source list (`GET /kb/sources`) for admin.
- Public KB query API (`GET /kb/query`) with retrieval hits and generated answer.
- Health check (`GET /healthz`) and WeChat access token query (`GET /wechat/access-token`) for admin.
- KB file format support: `txt`, `md`, `csv`, `json`, `pdf`, `docx`, `png`, `jpg`, `jpeg`, `bmp`, `webp`.
- Startup and operations scripts for app boot, KB sync, and Cloudflare tunnel setup/start.

Version rule:
- Small update: increase by `0.1` (for example: `v1.1`, `v1.2`).
- Functional change update: increase major by `1` (for example: `v2.0`).
