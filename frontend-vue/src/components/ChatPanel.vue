<template>
  <div class="flex flex-col h-full bg-deep-950 relative overflow-hidden" @click="closeCitationCard">
    <!-- Background Grid Effect -->
    <div
      class="absolute inset-0 opacity-10 pointer-events-none"
      style="
        background-image: linear-gradient(#1e293b 1px, transparent 1px),
          linear-gradient(90deg, #1e293b 1px, transparent 1px);
        background-size: 40px 40px;
      "
    />

    <!-- Header / Mode Switch -->
    <div class="relative z-10 p-4 border-b border-deep-800 flex justify-between items-center bg-deep-950/80 backdrop-blur">
      <div class="flex items-center gap-3">
        <div class="w-2 h-2 rounded-full bg-neon-green animate-pulse" />
        <h1 class="font-bold text-lg text-slate-100 tracking-tight">深蓝智脑</h1>
      </div>

      <!-- RAG Toggle -->
      <button
        @click.stop="$emit('toggleRag')"
        :class="[
          'flex items-center gap-3 px-4 py-2 rounded-full border transition-all duration-300',
          isRagEnabled
            ? 'bg-neon-blue/10 border-neon-blue text-neon-blue shadow-[0_0_15px_rgba(6,182,212,0.3)]'
            : 'bg-deep-800 border-deep-700 text-slate-400 hover:border-slate-500'
        ]"
      >
        <span class="text-sm font-mono font-bold">
          {{ isRagEnabled ? 'RAG系统：在线' : 'RAG系统：离线' }}
        </span>
        <component :is="isRagEnabled ? Zap : Shield" :size="16" :class="{ 'fill-current': isRagEnabled }" />
      </button>
    </div>

    <!-- Messages Area -->
    <div ref="messagesContainer" class="flex-1 overflow-y-auto p-6 space-y-6 relative z-10">
      <div
        v-for="msg in messages"
        :key="msg.id"
        :class="['flex gap-4', msg.role === 'user' ? 'justify-end' : 'justify-start']"
      >
        <!-- AI Avatar -->
        <div
          v-if="msg.role === 'ai'"
          class="w-8 h-8 rounded bg-deep-800 border border-deep-700 flex items-center justify-center shrink-0"
        >
          <Bot :size="20" class="text-neon-blue" />
        </div>

        <!-- Message Bubble -->
        <div
          :class="[
            'max-w-[80%] rounded-lg p-4 border shadow-xl',
            msg.role === 'user'
              ? 'bg-deep-800 border-deep-700 text-slate-200'
              : 'bg-black/40 border-deep-800 text-slate-300'
          ]"
        >
          <!-- AI Status Badge -->
          <div
            v-if="msg.role === 'ai' && !msg.ragEnabled"
            class="flex items-center gap-2 mb-2 text-amber-500/80 text-xs font-mono border-b border-amber-500/20 pb-1"
          >
            <AlertTriangle :size="12" />
            通用知识
          </div>
          <div
            v-if="msg.role === 'ai' && msg.ragEnabled"
            class="flex items-center gap-2 mb-2 text-neon-blue/80 text-xs font-mono border-b border-neon-blue/20 pb-1"
          >
            <Shield :size="12" />
            知识库检索
          </div>

          <!-- 用户消息：纯文本。不渲染用户输入的 markdown ——
               用户打的 * 或 # 应当原样显示，那是内容不是格式。 -->
          <div
            v-if="msg.role === 'user'"
            class="text-sm leading-relaxed whitespace-pre-wrap"
          >{{ msg.content }}</div>

          <!-- AI 消息：渲染 markdown，并把 [n] 角标还原为可点击元素 -->
          <div v-else class="text-sm leading-relaxed markdown-body">
            <template
              v-for="(part, i) in renderAiMessage(msg)"
              :key="i"
            >
              <span v-if="part.type === 'html'" v-html="part.html" />
              <!-- 来源角标：紧凑上标数字。
                   同一 chunk 被引多次共享同一编号（后端按 chunk_id 去重）。 -->
              <sup
                v-else
                :class="[
                  'inline-block mx-0.5 px-0.5 font-mono font-bold',
                  'text-[0.7rem] leading-none cursor-pointer rounded',
                  'transition-colors duration-200 select-none',
                  activeCitation?.index === part.number
                    ? 'text-deep-950 bg-neon-blue'
                    : 'text-neon-blue hover:bg-neon-blue/25'
                ]"
                @click.stop="handleNumberClick($event, msg, part.number)"
                @mouseenter="emitHoverByNumber(msg, part.number)"
                @mouseleave="$emit('citationHover', null)"
              >[{{ part.number }}]</sup>
            </template>
          </div>

          <!-- 末尾来源列表：正文只留紧凑角标，文件名集中在这里，
               避免同一来源在正文中反复出现长文本 -->
          <div
            v-if="msg.role === 'ai' && msg.citationDetails?.length"
            class="mt-3 pt-2 border-t border-deep-800/80 space-y-1"
          >
            <div class="text-[0.65rem] uppercase tracking-wider text-slate-600 font-mono mb-1">
              引用来源
            </div>
            <div
              v-for="detail in msg.citationDetails"
              :key="detail.number ?? detail.chunkId"
              class="flex items-center gap-2 text-xs cursor-pointer group"
              @click="handleSourceListClick($event, detail)"
              @mouseenter="detail.chunkId && $emit('citationHover', detail.chunkId)"
              @mouseleave="$emit('citationHover', null)"
            >
              <span class="font-mono text-neon-blue text-[0.7rem] font-bold shrink-0">
                [{{ detail.number }}]
              </span>
              <span class="font-mono text-slate-400 group-hover:text-neon-blue transition-colors truncate">
                {{ detail.file }}
              </span>
              <span
                v-if="detail.relevance !== undefined"
                :class="['font-mono text-[0.65rem] shrink-0', relevanceColorClass(detail.relevance)]"
              >
                ({{ Math.round(detail.relevance * 100) }}%)
              </span>
            </div>
          </div>
        </div>

        <!-- User Avatar -->
        <div
          v-if="msg.role === 'user'"
          class="w-8 h-8 rounded bg-slate-700 border border-slate-600 flex items-center justify-center shrink-0"
        >
          <User :size="20" class="text-slate-300" />
        </div>
      </div>

      <!-- Thinking Indicator -->
      <div v-if="isThinking" class="flex gap-4 justify-start animate-pulse">
        <div class="w-8 h-8 rounded bg-deep-800 border border-deep-700 flex items-center justify-center shrink-0">
          <Bot :size="20" class="text-neon-blue" />
        </div>
        <div class="bg-black/40 border border-deep-800 rounded-lg p-4 flex items-center gap-2">
          <div
            v-for="i in 3"
            :key="i"
            class="w-2 h-2 bg-neon-blue rounded-full animate-bounce"
            :style="{ animationDelay: `${(i - 1) * 150}ms` }"
          />
        </div>
      </div>
    </div>

    <!-- Input Area -->
    <div class="p-4 bg-deep-900 border-t border-deep-800 relative z-10">
      <form @submit.prevent="handleSubmit" class="flex gap-3 relative">
        <input
          v-model="inputText"
          type="text"
          :disabled="isThinking"
          :placeholder="isRagEnabled ? '请提问关于文档的问题...' : '请提问通用问题...'"
          class="flex-1 bg-deep-950 border border-deep-700 rounded-lg px-4 py-3 text-base text-slate-200 focus:outline-none focus:border-neon-blue focus:shadow-[0_0_15px_rgba(6,182,212,0.1)] transition-all placeholder:text-slate-600"
        />
        <button
          type="submit"
          :disabled="!inputText.trim() || isThinking"
          class="bg-neon-blue hover:bg-cyan-400 text-deep-950 font-bold p-3 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Send :size="20" />
        </button>
      </form>
    </div>

    <!-- 来源悬浮卡片（Teleport 到 body，避免被 overflow:hidden 裁切） -->
    <Teleport to="body">
      <Transition name="citation-card">
        <div
          v-if="activeCitation"
          class="fixed z-[9999] w-96 max-w-[calc(100vw-2rem)] bg-deep-900 border border-neon-blue/40 rounded-xl shadow-2xl shadow-black/60 overflow-hidden"
          :style="cardPositionStyle"
          @click.stop
        >
          <!-- 卡片顶部：来源信息 -->
          <div class="flex items-center justify-between px-4 py-3 bg-neon-blue/10 border-b border-neon-blue/20">
            <div class="flex items-center gap-2 min-w-0">
              <FileText :size="14" class="text-neon-blue shrink-0" />
              <span class="text-xs font-mono text-neon-blue truncate" :title="activeCitation.sourceName">
                {{ activeCitation.sourceName }}
              </span>
              <span class="text-xs text-slate-500 shrink-0">·</span>
              <span class="text-xs font-mono text-slate-500 shrink-0">
                [{{ activeCitation.index }}]
              </span>
            </div>
            <button
              @click="closeCitationCard"
              class="w-6 h-6 flex items-center justify-center rounded hover:bg-deep-700 transition-colors text-slate-400 hover:text-slate-200 shrink-0 ml-2"
              title="关闭"
            >
              <X :size="14" />
            </button>
          </div>

          <!-- 相关性说明区。
               不用 tooltip：一个百分比脱离口径说明是不可解释的，
               而 title 属性既不能常驻也无法排版。
               语义边界要写清 —— 这是"该 chunk 与查询有多相关"，
               不是"这句话被该 chunk 支持"（后者需 M3 的 citation_verify）。 -->
          <div
            v-if="activeCitation.relevance !== undefined"
            class="px-4 py-3 border-b border-deep-800 bg-black/20 space-y-2"
          >
            <div class="flex items-baseline justify-between gap-2">
              <span class="text-[0.7rem] uppercase tracking-wider text-slate-500 font-mono">
                {{ relevanceBasisLabel }}
              </span>
              <span :class="['text-lg font-bold font-mono', relevanceColorClass(activeCitation.relevance)]">
                {{ Math.round(activeCitation.relevance * 100) }}%
              </span>
            </div>

            <!-- 进度条：数值的视觉锚点，比裸数字更快读出高低 -->
            <div class="h-1 rounded-full bg-deep-800 overflow-hidden">
              <div
                class="h-full rounded-full transition-all duration-500"
                :class="relevanceBarClass(activeCitation.relevance)"
                :style="{ width: `${Math.round(activeCitation.relevance * 100)}%` }"
              />
            </div>

            <p class="text-[0.7rem] text-slate-500 leading-relaxed">
              {{ relevanceExplain }}
              <span class="text-slate-600">
                原始值 {{ activeCitation.relevance.toFixed(3) }}
              </span>
            </p>
          </div>

          <!-- 卡片主体：文档内容 -->
          <div class="p-4 max-h-64 overflow-y-auto">
            <div class="text-[0.7rem] uppercase tracking-wider text-slate-600 font-mono mb-2">
              原文片段
            </div>
            <p class="text-sm text-slate-300 leading-relaxed font-mono whitespace-pre-wrap">{{ activeCitation.content }}</p>
          </div>

          <!-- 卡片底部：chunk ID -->
          <div class="px-4 py-2 bg-black/30 border-t border-deep-800">
            <span class="text-xs text-slate-600 font-mono">chunk: {{ activeCitation.chunkId }}</span>
          </div>

          <!-- 向上三角箭头（指向角标） -->
          <div
            class="absolute -top-2 border-8 border-transparent border-b-neon-blue/40"
            :style="{ left: `${arrowLeft}px` }"
          />
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue';
import { Send, Zap, Shield, User, Bot, AlertTriangle, FileText, X } from 'lucide-vue-next';
import type { Message, CitationDetail } from '@/types';
import { renderWithCitations, type RenderedPart } from '@/utils/markdown';

// ─── Types ────────────────────────────────────────────────────────────────────

interface ChunkInfo {
  content: string;
  sourceName: string;
}

interface Props {
  messages: Message[];
  isRagEnabled: boolean;
  isThinking: boolean;
  /** chunkId → { content, sourceName } 映射（降级用，主要路径是 citationDetails） */
  chunkMap?: Record<string, ChunkInfo>;
  /** relevance 的计算依据，决定引用卡片里那个百分比的物理含义 */
  relevanceBasis?: 'rerank' | 'cosine' | null;
}

interface Emits {
  (e: 'toggleRag'): void;
  (e: 'sendMessage', text: string): void;
  (e: 'citationHover', id: string | null): void;
}

interface ContentPart {
  type: 'text' | 'citation';
  /** 标记原文，如 "[来源: filename.md]" 或 "[1]" */
  content: string;
  chunkId?: string;
  /** 角标序号（从1开始） */
  index?: number;
  /** 引用内容（直接来自后端 citationDetails，精准对应每条引用） */
  citationContent?: string;
  /** 引用文件名（来自后端 citationDetails） */
  citationFile?: string;
  /** 该证据与查询的相关性，[0,1]（来自后端 citationDetails） */
  citationRelevance?: number;
}

interface ActiveCitation {
  chunkId: string;
  content: string;
  sourceName: string;
  index: number;
  /** 该证据与查询的相关性，[0,1]。undefined 表示后端未提供（如纯 BM25 检索） */
  relevance?: number;
  triggerRect: DOMRect;
}

// ─── Props & Emits ─────────────────────────────────────────────────────────────

const props = defineProps<Props>();
const emit = defineEmits<Emits>();

// ─── State ────────────────────────────────────────────────────────────────────

const inputText = ref('');
const messagesContainer = ref<HTMLDivElement>();
const activeCitation = ref<ActiveCitation | null>(null);

// ─── 卡片定位计算（fixed，相对于 viewport）────────────────────────────────────

/** 卡片宽度（px），需与模板宽度 w-96(384px) 对应 */
const CARD_WIDTH = 384;
/** 卡片出现在角标下方的间距 */
const CARD_OFFSET_Y = 10;

const cardPositionStyle = computed(() => {
  if (!activeCitation.value) return {};
  const r = activeCitation.value.triggerRect;

  // 水平：优先居中于角标，超出屏幕右侧则靠右
  let left = r.left + r.width / 2 - CARD_WIDTH / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - CARD_WIDTH - 8));

  // 垂直：默认在角标下方；空间不足时改为上方
  const spaceBelow = window.innerHeight - r.bottom;
  const top = spaceBelow > 280
    ? r.bottom + CARD_OFFSET_Y
    : r.top - CARD_OFFSET_Y - 300; // 大约 max-h-64 + header + footer

  return { left: `${left}px`, top: `${Math.max(8, top)}px` };
});

/** 三角箭头在卡片内的水平偏移 */
const arrowLeft = computed(() => {
  if (!activeCitation.value) return 16;
  const r = activeCitation.value.triggerRect;
  const cardLeft = parseFloat(cardPositionStyle.value.left || '0');
  return Math.max(8, r.left + r.width / 2 - cardLeft - 8);
});

// ─── Citation 点击处理 ─────────────────────────────────────────────────────────

const handleCitationClick = (event: MouseEvent, part: ContentPart) => {
  const el = event.currentTarget as HTMLElement;
  const triggerRect = el.getBoundingClientRect();
  const chunkId = part.chunkId || '';

  // 点击同一个角标 → 关闭
  if (activeCitation.value?.chunkId === chunkId && chunkId) {
    closeCitationCard();
    return;
  }

  // 优先1：后端直接发来的 citationDetails（精准、不受路径格式影响）
  let content = part.citationContent || '';
  let sourceName = part.citationFile || '';

  // 优先2：chunkMap（降级，通过 chunkId 查找）
  if (!content && chunkId && props.chunkMap?.[chunkId]) {
    const info = props.chunkMap[chunkId];
    content = info.content;
    sourceName = sourceName || info.sourceName;
  }

  if (!content) {
    // 无内容可显示，只触发高亮
    if (chunkId) emit('citationHover', chunkId);
    return;
  }

  activeCitation.value = {
    chunkId,
    content,
    sourceName,
    index: part.index ?? 0,
    relevance: part.citationRelevance,
    triggerRect,
  };

  // 同步触发左侧面板高亮
  if (chunkId) emit('citationHover', chunkId);
};

const closeCitationCard = () => {
  activeCitation.value = null;
};

/** 从末尾来源列表点击，与点击正文角标等效 */
const handleSourceListClick = (event: MouseEvent, detail: CitationDetail) => {
  handleCitationClick(event, {
    type: 'citation',
    content: String(detail.number ?? ''),
    chunkId: detail.chunkId,
    index: detail.number ?? 0,
    citationContent: detail.content,
    citationFile: detail.file,
    citationRelevance: detail.relevance,
  });
};

// ─── AI 消息渲染（markdown + 角标） ────────────────────────────────────────────

/** 按编号取 citation 明细 */
const detailByNumber = (msg: Message, n: number): CitationDetail | undefined =>
  msg.citationDetails?.find((d, i) => (d.number ?? i + 1) === n);

/**
 * 渲染 AI 消息：markdown 转 HTML（已净化）并把 [n] 拆成独立片段。
 *
 * 流式过程中 content 会不断增长，每次都重新渲染。marked 对这个量级的
 * 文本足够快，不做缓存以避免流式时显示滞后。
 */
const renderAiMessage = (msg: Message): RenderedPart[] => renderWithCitations(msg.content);

const handleNumberClick = (event: MouseEvent, msg: Message, n: number) => {
  const detail = detailByNumber(msg, n);
  if (!detail) return;
  handleSourceListClick(event, detail);
};

const emitHoverByNumber = (msg: Message, n: number) => {
  const chunkId = detailByNumber(msg, n)?.chunkId;
  if (chunkId) emit('citationHover', chunkId);
};

const titleForNumber = (msg: Message, n: number): string => {
  const detail = detailByNumber(msg, n);
  return detail ? `来源：${detail.file}（点击查看原文）` : '';
};

/**
 * 相关性配色。阈值与后端 config 对齐：
 *   ANSWERABLE_MIN_RELEVANCE = 0.75  足以支撑基于文档的回答
 *   RETRIEVAL_MIN_RELEVANCE  = 0.35  可进上下文但不足以判定可答
 * 注意 0.75 这个值由评测校准得出（docs/eval/threshold.md），
 * 且报告已说明单一阈值只能识别约两成无答案查询。
 */
const relevanceColorClass = (relevance: number): string => {
  if (relevance >= 0.75) return 'text-neon-blue';
  if (relevance >= 0.35) return 'text-amber-400';
  return 'text-red-400';
};

const relevanceBarClass = (relevance: number): string => {
  if (relevance >= 0.75) return 'bg-neon-blue';
  if (relevance >= 0.35) return 'bg-amber-400';
  return 'bg-red-400';
};

/** 标题写明这个百分比是什么量，而不是含糊的"相关性" */
const relevanceBasisLabel = computed(() =>
  props.relevanceBasis === 'rerank' ? '精排相关概率' : '余弦相似度'
);

/**
 * 口径解释。两条映射的物理依据不同，必须分别说明：
 *   rerank —— bge-reranker 以二分类交叉熵训练，logit 过 sigmoid
 *             就是模型自身估计的相关概率，是模型原生语义
 *   cosine —— 归一化向量的余弦距离线性映射到 [0,1]
 */
const relevanceExplain = computed(() => {
  if (props.relevanceBasis === 'rerank') {
    return '由 cross-encoder 对（问题, 片段）联合打分后取 sigmoid，即模型估计的相关概率。';
  }
  return '由问题与片段的向量余弦距离换算（1 - d/2）。衡量语义接近程度，不等于该片段支持了这句话。';
});


// ─── 自动滚动 ──────────────────────────────────────────────────────────────────

// 深度监听消息内容变化（流式输出时内容在变但数量不变）
watch(() => props.messages, async () => {
  await nextTick();
  const el = messagesContainer.value;
  if (el) el.scrollTop = el.scrollHeight;
}, { deep: true });

// ─── 输入处理 ──────────────────────────────────────────────────────────────────

const handleSubmit = () => {
  if (inputText.value.trim() && !props.isThinking) {
    closeCitationCard();
    emit('sendMessage', inputText.value);
    inputText.value = '';
  }
};
</script>

<style scoped>
/* 悬浮卡片入场/离场动画 */
.citation-card-enter-active,
.citation-card-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}
.citation-card-enter-from,
.citation-card-leave-to {
  opacity: 0;
  transform: translateY(-6px) scale(0.97);
}

/* Markdown 渲染样式。
   用 :deep() 因为内容由 v-html 插入，不带 scoped 属性选择器。
   此前 AI 回答用 whitespace-pre-wrap 纯文本渲染，模型输出的
   ##、-、** 原样显示且缩进错乱。 */
.markdown-body :deep(p) {
  margin: 0 0 0.6em;
}
.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}

/* 标题：聊天气泡里不需要 h1 的巨大字号，压到接近正文 */
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 0.9em 0 0.45em;
  font-weight: 600;
  line-height: 1.35;
  color: #e2e8f0;
}
.markdown-body :deep(h1) { font-size: 1.15em; }
.markdown-body :deep(h2) { font-size: 1.08em; }
.markdown-body :deep(h3) { font-size: 1em; }
.markdown-body :deep(h4) { font-size: 0.95em; }
.markdown-body :deep(h1:first-child),
.markdown-body :deep(h2:first-child),
.markdown-body :deep(h3:first-child) {
  margin-top: 0;
}

/* 列表：padding-left 给足，否则中文项目符号与文字挤在一起 */
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 0.5em 0;
  padding-left: 1.5em;
}
.markdown-body :deep(li) {
  margin: 0.25em 0;
}
.markdown-body :deep(li > ul),
.markdown-body :deep(li > ol) {
  margin: 0.25em 0;
}

.markdown-body :deep(strong) {
  font-weight: 600;
  color: #f1f5f9;
}

.markdown-body :deep(code) {
  padding: 0.12em 0.4em;
  border-radius: 0.25rem;
  background: rgba(6, 182, 212, 0.12);
  color: #67e8f9;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 0.9em;
}
.markdown-body :deep(pre) {
  margin: 0.6em 0;
  padding: 0.75rem;
  border-radius: 0.5rem;
  background: rgba(0, 0, 0, 0.45);
  border: 1px solid rgba(148, 163, 184, 0.15);
  overflow-x: auto;
}
.markdown-body :deep(pre code) {
  padding: 0;
  background: none;
  color: #cbd5e1;
}

.markdown-body :deep(blockquote) {
  margin: 0.6em 0;
  padding: 0.1em 0 0.1em 0.8em;
  border-left: 2px solid rgba(6, 182, 212, 0.4);
  color: #94a3b8;
}

/* 表格：模型常用表格作对比，不设边框会完全对不齐 */
.markdown-body :deep(table) {
  margin: 0.6em 0;
  border-collapse: collapse;
  font-size: 0.92em;
  display: block;
  overflow-x: auto;
  max-width: 100%;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: 0.35em 0.7em;
  border: 1px solid rgba(148, 163, 184, 0.22);
  text-align: left;
  vertical-align: top;
}
.markdown-body :deep(th) {
  background: rgba(6, 182, 212, 0.08);
  font-weight: 600;
  color: #e2e8f0;
  white-space: nowrap;
}

.markdown-body :deep(hr) {
  margin: 0.9em 0;
  border: none;
  border-top: 1px solid rgba(148, 163, 184, 0.18);
}

.markdown-body :deep(a) {
  color: #22d3ee;
  text-decoration: underline;
  text-underline-offset: 2px;
}
</style>
