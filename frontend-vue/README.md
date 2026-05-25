# DeepBlue Brain Frontend

Vue 3 + TypeScript + Vite frontend for DeepBlue Brain, an interactive teaching surface for observing the full RAG pipeline.

## What The UI Makes Visible

- Three-panel workspace: knowledge library, chat, and RAG runtime log.
- Streaming chat over `POST /api/v1/chat/stream`.
- Streaming document ingestion over `POST /api/v1/documents/upload/stream`.
- Real-time retrieval log events for query handling, hybrid search, BM25 indexing, generation, and citations.
- Clickable citation chips backed by the original retrieved chunk content.
- Prompt Inspector for viewing the exact prompt sent to the LLM.
- Live retrieval status, top-hit score, upload progress, and generation state.

The frontend is intentionally built as an inspection surface: users can see retrieval logs, cited chunks, and the final prompt instead of treating RAG as a hidden black box.

## Local Development

```bash
cd frontend-vue
npm install
npm run dev
```

The Vite dev server runs on `http://localhost:3000` and proxies `/api` to `http://localhost:8000`.

## Build

```bash
npm run build
npm run preview
```

## Key Files

```text
src/App.vue                         # Three-panel shell
src/components/LibraryPanel.vue     # Document upload/list/chunk highlight
src/components/ChatPanel.vue        # Streaming answer and citation cards
src/components/BrainPanel.vue       # Runtime log, similarity gauge, Prompt Inspector
src/composables/useChat.ts          # Chat state and SSE event handling
src/composables/useDocuments.ts     # Document API and upload stream handling
src/composables/useSSEStream.ts     # Shared SSE log/status conversion
src/services/api.ts                 # Fetch client for FastAPI endpoints
src/types/index.ts                  # Shared TypeScript types
```

## Notes

- Supported upload extensions are `.md`, `.txt`, `.pdf`, and `.docx`.
- `node_modules/` and `dist/` are local generated directories and are ignored by Git.
- Backend model/provider settings live in the root `.env`; see the root README for current model examples.
