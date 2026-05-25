# DeepBlue Brain

深蓝智脑————将 RAG 黑箱打开的“白盒化”交互式教学平台

在线体验: [http://deepblue-brain.cn/](http://deepblue-brain.cn/)

DeepBlue Brain 不是一个只负责“给答案”的问答壳子，而是一套可以被观察、被追踪、被复盘的 RAG 学习环境。它把文档上传、分块、Embedding、检索、重排、Prompt 组装、LLM 生成和引用回溯这些环节都摊开给你看，让你能真正理解一个 RAG 系统是怎么从文档走到答案的。

如果说传统 RAG 像一个黑盒，那 DeepBlue Brain 更像一张可交互的解剖图。

## 它适合做什么

- 课堂演示 RAG 全链路，而不是只展示最终答案。
- 帮助初学者理解向量检索、BM25、chunk、citation、Prompt 之间的关系。
- 在答辩、课程设计或作品集里展示“可解释性”和“白盒化”特征。
- 排查知识库问答项目中的问题到底出在文档、分块、检索、提示词，还是生成阶段。

它强调的是学习、观察和复盘，而非把自己包装成生产级系统。

## 你能看到什么

### 实时检索日志流

右侧运行面板通过 SSE 持续展示后台事件。你不需要等整段回答结束再猜发生了什么，页面会实时告诉你：

- 问题如何进入会话
- 是否触发了指代消解
- 检索走的是向量、BM25，还是混合模式
- 命中了哪些文档，分数如何
- LLM 什么时候开始生成
- 上传文档时，解析、分块、Embedding、入库各走到哪一步

这一条“实时检索日志流”是项目最核心的白盒化入口。

### 提示词原文溯源

每次回答结束后，你都可以展开 `Prompt Inspector`，直接看到后端实际送入模型的完整 Prompt，包括：

- 用户原始问题
- 指代消解后的问题
- 被拼入上下文的原始文档片段
- 引用要求
- 对话历史上下文

这会让“模型为什么这么答”从猜测变成可验证的事实。

### 原文证据卡片

回答中的来源标记可以点击。点击后会弹出对应的原文片段，展示：

- 来源文件名
- 引用顺序
- 原始 chunk 内容
- chunk ID

这样一来，答案、引用、检索命中、原始证据就连成了一条完整链路。

### 流式文档摄入

上传文档不是一个静默等待的黑盒。你会看到文档从接收、解析、分块、Embedding 到入库的全过程。支持的文件格式包括：

- `.md`
- `.txt`
- `.pdf`
- `.docx`

## 功能一览

- 文档上传、删除、列表查看和 chunk 展示
- RAG 模式与通用知识模式切换
- SSE 流式问答
- SSE 流式上传进度
- 多轮对话与简单指代消解
- 引用追踪与原文弹窗
- Prompt Inspector
- 实时检索质量与相似度展示
- FastAPI Swagger 接口文档
- Docker Compose 启动

## 技术栈

前端

- Vue 3
- TypeScript
- Vite
- Tailwind CSS
- lucide-vue-next

后端

- FastAPI
- Pydantic v2
- StreamingResponse

RAG 核心

- ChromaDB
- sentence-transformers
- rank-bm25
- jieba

文档解析

- Markdown / TXT
- pypdf
- python-docx

LLM

- OpenAI
- Anthropic Claude
- 智谱 GLM
- 通义千问

部署

- Docker
- Docker Compose
- Nginx

## 架构速览

```text
Browser
  |
  |  Vue 3 SPA
  v
frontend-vue/
  - LibraryPanel: 上传、删除、查看文档 chunk
  - ChatPanel: 流式对话、引用标记、原文证据卡片
  - BrainPanel: 实时日志流、相似度仪表、Prompt Inspector
  |
  |  /api/*
  v
backend/
  - main.py: FastAPI 应用入口
  - api/sse.py: 对话 SSE 接口
  - api/upload.py: 文档上传接口
  - api/routes.py: 文档列表、删除、统计、非流式对话
  - services/chat_service.py: 会话、检索、生成编排
  - adapters/streaming_ingestion.py: 文档摄入事件流
  |
  v
rag/
  - ingestion.py: 文档读取、分块、入库
  - chunker.py: Markdown/段落感知分块
  - embedder.py: SentenceTransformer 封装
  - vectordb.py: ChromaDB 访问层
  - retriever.py: 向量检索、BM25、Hybrid、Rerank
  - citation.py: 引用 Prompt 与解析
  - llm.py: 多 LLM 提供商封装
  - conversation.py: 多轮对话和指代消解
```

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/xiyue188/RAG.git
cd RAG
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

推荐先用智谱作为默认演示模型：

```env
LLM_PROVIDER=zhipu
ZHIPU_API_KEY=your-zhipu-api-key
ZHIPU_MODEL=glm-4-flash-250414
```

如果你想切换到 OpenAI，可以这样写：

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-your-openai-key
OPENAI_MODEL=gpt-4.1-mini
```

### 3. 本地开发启动

后端：

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

前端：

```bash
cd frontend-vue
npm install
npm run dev
```

访问地址：

- 前端: `http://localhost:3000`
- 后端接口文档: `http://localhost:8000/docs`
- 健康检查: `http://localhost:8000/health`

### 4. Docker 启动

```bash
docker compose up -d --build
```

启动后访问：

- 前端: `http://localhost`
- 后端接口文档: `http://localhost:8000/docs`
- 健康检查: `http://localhost:8000/health`

## 推荐体验路径

1. 打开页面。
2. 在左侧知识库上传文档。
3. 等待右侧日志流显示解析、分块、Embedding、入库完成。
4. 在中间对话框发问。
5. 观察右侧实时检索日志流，确认命中文档和生成状态。
6. 点击回答中的来源标记，查看原文证据卡片。
7. 展开 Prompt Inspector，查看真实发送给模型的提示词。

## 配置说明

常用配置都集中在 `.env` 中，由 `config.py` 统一读取。

```env
LLM_PROVIDER=zhipu
ZHIPU_API_KEY=your-key
ZHIPU_MODEL=glm-4-flash-250414

OPENAI_MODEL=gpt-4.1-mini
ANTHROPIC_MODEL=claude-sonnet-4-20250514
QWEN_MODEL=qwen3.6-plus

TOP_K_RESULTS=3
SIMILARITY_THRESHOLD=0.7
ENABLE_HYBRID=true
BM25_WEIGHT=0.3
VECTOR_WEIGHT=0.7
HYBRID_TOP_K=20

CHUNK_SIZE=500
CHUNK_OVERLAP=100

LOG_LEVEL=INFO
ALLOWED_ORIGINS=http://localhost,http://localhost:3000
```

### 支持的文档格式

- `.md`
- `.txt`
- `.pdf`
- `.docx`

### 常见补充项

- `HF_ENDPOINT=https://hf-mirror.com`，适合国内环境下载模型较慢时使用。
- `.env` 不要提交到 Git。
- `chroma_db/`、`logs/`、`frontend-vue/dist/`、`node_modules/`、`venv/` 都是本地运行或构建产物。

## 主要接口

- `POST /api/v1/chat/stream`: SSE 流式问答。
- `GET /api/v1/chat/stream-get`: 兼容 EventSource 的流式问答。
- `POST /api/v1/chat/message`: 非流式问答。
- `POST /api/v1/documents/upload/stream`: SSE 流式文档上传。
- `POST /api/v1/documents/upload`: 同步文档上传。
- `GET /api/v1/documents?include_chunks=true`: 查看文档与 chunk 列表。
- `DELETE /api/v1/documents/{file_name}`: 删除文档。
- `GET /api/v1/stats`: 查看系统统计。
- `GET /health`: 健康检查。


## 目录说明

```text
rag-project/
  backend/              FastAPI Web 层
  rag/                  RAG 核心逻辑
  frontend-vue/         Vue 前端
  data/documents/       示例/本地文档目录
  chroma_db/            本地 ChromaDB 数据目录
  scripts/              调试和验证脚本
  config.py             配置入口
  requirements.txt      Python 依赖
  docker-compose.yml    Docker 编排
  Dockerfile.backend    后端镜像
```


## License

MIT
