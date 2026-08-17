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

          <!-- AI 消息：整块 v-html 渲染 markdown，角标已内联在 HTML 中。
               不拆成多个片段渲染 —— 角标通常在 <p> 内部，拆分会产出
               未闭合的块级标签，浏览器自动闭合后角标被挤到下一行。
               点击与悬停用事件委托处理。 -->
          <div
            v-else
            class="text-sm leading-relaxed markdown-body"
            v-html="renderAiMessage(msg)"
            @click="handleMarkdownClick($event, msg)"
            @mouseover="handleMarkdownHover($event, msg)"
            @mouseout="$emit('citationHover', null)"
          />

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
        <!-- flex-col + 内容区 flex-1 是关键：卡片有 maxHeight 约束时，
             只有这样内容区才会收缩并出现滚动条，否则内容溢出被裁掉。 -->
        <div
          v-if="activeCitation"
          class="fixed z-[9999] w-96 max-w-[calc(100vw-2rem)] flex flex-col bg-deep-900 border border-neon-blue/40 rounded-xl shadow-2xl shadow-black/60 overflow-hidden"
          :style="cardPositionStyle"
          @click.stop
        >
          <!-- 卡片顶部：来源信息。shrink-0 防止被内容区挤压 -->
          <div class="shrink-0 flex items-center justify-between px-4 py-3 bg-neon-blue/10 border-b border-neon-blue/20">
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
            class="shrink-0 px-4 py-3 border-b border-deep-800 bg-black/20 space-y-2"
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

          <!-- 卡片主体：文档内容。flex-1 + min-h-0 让它吸收剩余高度并可滚动。
               min-h-0 是必需的 —— flex 子项默认 min-height:auto，
               不设置会导致内容撑开父容器而非出现滚动条。 -->
          <div class="flex-1 min-h-0 overflow-y-auto p-4">
            <div class="flex items-center justify-between mb-2">
              <span class="text-[0.7rem] uppercase tracking-wider text-slate-600 font-mono">
                原文片段（完整 chunk）
              </span>
              <span v-if="highlightedParts.some(p => p.hit)" class="text-[0.65rem] text-slate-600">
                <span class="bg-amber-400/25 text-amber-200 px-1 rounded">高亮</span>
                = 命中提问词
              </span>
            </div>
            <!-- 完整展示 chunk，不截取"最相关的一段"：
                 截取会丢掉否定词与例外条件这类改变语义的上下文，
                 而相关性分数是针对整个 chunk 算的，只展示局部又会造成口径不一致。
                 用高亮代替截取 —— 用户仍能快速定位该看哪里。

                 pre-wrap 保留原文换行（表格与列表靠它对齐），
                 但不用等宽字体：中文等宽渲染差且行长参差。 -->
            <p class="text-[0.8rem] text-slate-300 leading-relaxed whitespace-pre-wrap break-words">
              <template v-for="(part, i) in highlightedParts" :key="i">
                <mark
                  v-if="part.hit"
                  class="bg-amber-400/25 text-amber-200 rounded px-0.5"
                >{{ part.text }}</mark>
                <span v-else>{{ part.text }}</span>
              </template>
            </p>
          </div>

          <!-- 卡片底部：chunk ID -->
          <div class="shrink-0 px-4 py-2 bg-black/30 border-t border-deep-800">
            <span class="text-xs text-slate-600 font-mono break-all">chunk: {{ activeCitation.chunkId }}</span>
          </div>

          <!-- 三角箭头。卡片脱离角标居中显示时不画箭头 ——
               指向一个不相邻的位置反而造成误解。 -->
          <div
            v-if="cardPlacement === 'below'"
            class="absolute -top-2 border-8 border-transparent border-b-neon-blue/40"
            :style="{ left: `${arrowLeft}px` }"
          />
          <div
            v-else-if="cardPlacement === 'above'"
            class="absolute -bottom-2 border-8 border-transparent border-t-neon-blue/40"
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
import {
  renderMarkdown,
  citationNumberFromEvent,
  extractQueryTerms,
  highlightTerms,
  type HighlightPart,
} from '@/utils/markdown';
import { useThresholds } from '@/composables/useThresholds';

// 阈值的唯一真相源是后端 config.py，启动时拉一次
const { tierOf } = useThresholds();

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
/** 卡片与角标之间的间距 */
const CARD_OFFSET_Y = 10;
/** 卡片与视口边缘的最小留白 */
const VIEWPORT_MARGIN = 12;

/** 卡片期望高度（px）。原文至少要能展示十几行，否则失去查证价值。 */
const CARD_PREFERRED_HEIGHT = 460;
/** 卡片最低高度。低于此值原文区只剩一两行，不如不展示。 */
const CARD_MIN_HEIGHT = 300;

/**
 * 卡片定位。
 *
 * 两次迭代的教训：
 *   一版用硬编码 300px 估算高度决定放上/下，内容变多后溢出且无法滚动。
 *   二版改为"贴着角标、取上下空间较大的一侧"，但高度完全受角标位置
 *   摆布 —— 角标在页面顶部时上方空间几乎为零，原文区只剩一两行。
 *
 * 现在的策略：先确定卡片需要多高（CARD_PREFERRED_HEIGHT），
 * 再找能容纳它的位置。贴着角标只是首选，空间不够就脱离角标、
 * 在视口内垂直居中 —— 可读性优先于"紧贴触发点"这个美观偏好。
 */
const cardPositionStyle = computed(() => {
  if (!activeCitation.value) return {};
  const r = activeCitation.value.triggerRect;

  // 水平：优先居中于角标，超出视口则贴边
  let left = r.left + r.width / 2 - CARD_WIDTH / 2;
  left = Math.max(
    VIEWPORT_MARGIN,
    Math.min(left, window.innerWidth - CARD_WIDTH - VIEWPORT_MARGIN)
  );

  const spaceBelow = window.innerHeight - r.bottom - CARD_OFFSET_Y - VIEWPORT_MARGIN;
  const spaceAbove = r.top - CARD_OFFSET_Y - VIEWPORT_MARGIN;
  const viewportHeight = window.innerHeight - VIEWPORT_MARGIN * 2;
  const wanted = Math.min(CARD_PREFERRED_HEIGHT, viewportHeight);

  // 优先贴着角标：下方能放下就放下方，否则看上方
  if (spaceBelow >= Math.min(wanted, CARD_MIN_HEIGHT)) {
    return {
      left: `${left}px`,
      top: `${r.bottom + CARD_OFFSET_Y}px`,
      maxHeight: `${Math.min(wanted, spaceBelow)}px`,
    };
  }
  if (spaceAbove >= Math.min(wanted, CARD_MIN_HEIGHT)) {
    // 向上展开用 bottom 定位，卡片增高不会盖住角标
    return {
      left: `${left}px`,
      bottom: `${window.innerHeight - r.top + CARD_OFFSET_Y}px`,
      maxHeight: `${Math.min(wanted, spaceAbove)}px`,
    };
  }

  // 两侧都放不下期望高度 —— 脱离角标，在视口内居中。
  // 此时不画指向箭头（见 cardPlacement），因为卡片已不与角标相邻。
  const height = Math.min(wanted, viewportHeight);
  return {
    left: `${left}px`,
    top: `${Math.max(VIEWPORT_MARGIN, (window.innerHeight - height) / 2)}px`,
    maxHeight: `${height}px`,
  };
});

/**
 * 卡片相对角标的位置：'below' | 'above' | 'detached'。
 * 与 cardPositionStyle 用同一套判断，箭头据此决定朝向或不显示。
 */
const cardPlacement = computed<'below' | 'above' | 'detached'>(() => {
  if (!activeCitation.value) return 'below';
  const r = activeCitation.value.triggerRect;
  const spaceBelow = window.innerHeight - r.bottom - CARD_OFFSET_Y - VIEWPORT_MARGIN;
  const spaceAbove = r.top - CARD_OFFSET_Y - VIEWPORT_MARGIN;
  const viewportHeight = window.innerHeight - VIEWPORT_MARGIN * 2;
  const threshold = Math.min(
    Math.min(CARD_PREFERRED_HEIGHT, viewportHeight),
    CARD_MIN_HEIGHT
  );

  if (spaceBelow >= threshold) return 'below';
  if (spaceAbove >= threshold) return 'above';
  return 'detached';
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

/**
 * 触发本次引用的用户提问。
 *
 * 取最后一条 user 消息而非新增 prop：卡片总是在最新一轮回答里点开的，
 * 而 messages 已经在 props 中。多传一个 prop 只会多一处需要同步的状态。
 */
const activeQuery = computed(() => {
  for (let i = props.messages.length - 1; i >= 0; i--) {
    if (props.messages[i].role === 'user') return props.messages[i].content;
  }
  return '';
});

/** 原文按提问词切分出的高亮片段 */
const highlightedParts = computed<HighlightPart[]>(() => {
  if (!activeCitation.value) return [];
  return highlightTerms(
    activeCitation.value.content,
    extractQueryTerms(activeQuery.value)
  );
});

/** 按编号取 citation 明细 */
const detailByNumber = (msg: Message, n: number): CitationDetail | undefined =>
  msg.citationDetails?.find((d, i) => (d.number ?? i + 1) === n);

/**
 * 渲染 AI 消息为 HTML（已净化），角标内联为 <sup data-citation>。
 *
 * 流式过程中 content 不断增长，每次重新渲染。marked 对这个量级足够快，
 * 不做缓存以避免流式时显示滞后。
 */
const renderAiMessage = (msg: Message): string => renderMarkdown(msg.content);

/** 事件委托：点击正文中的角标 */
const handleMarkdownClick = (event: MouseEvent, msg: Message) => {
  const n = citationNumberFromEvent(event);
  if (n === null) return;
  event.stopPropagation();
  const detail = detailByNumber(msg, n);
  if (detail) handleSourceListClick(event, detail);
};

/** 事件委托：悬停角标时高亮左侧对应 chunk */
const handleMarkdownHover = (event: MouseEvent, msg: Message) => {
  const n = citationNumberFromEvent(event);
  if (n === null) return;
  const chunkId = detailByNumber(msg, n)?.chunkId;
  if (chunkId) emit('citationHover', chunkId);
};

/**
 * 相关性配色。**阈值从后端拉取，不在此处硬编码** ——
 * 此前这里写死 0.75/0.35，BrainPanel 写死 0.50/0.35，两个组件对同一个
 * relevance 给出不同颜色。
 *
 * 阈值本身由评测校准（docs/eval/threshold.md），且报告已说明单一阈值
 * 只能识别约两成无答案查询，M3 会引入独立的 answerability 判断。
 */
const relevanceColorClass = (relevance: number): string => {
  switch (tierOf(relevance)) {
    case 'answerable': return 'text-neon-blue';
    case 'context':    return 'text-amber-400';
    default:           return 'text-red-400';
  }
};

const relevanceBarClass = (relevance: number): string => {
  switch (tierOf(relevance)) {
    case 'answerable': return 'bg-neon-blue';
    case 'context':    return 'bg-amber-400';
    default:           return 'bg-red-400';
  }
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

/* 引用角标。样式必须写在这里而非 Tailwind class ——
   角标由 markdown.ts 生成的 HTML 携带，Tailwind 的 JIT
   扫不到动态字符串里的类名，写在模板里的 class 不会生效。 */
.markdown-body :deep(.citation-marker) {
  display: inline;
  margin: 0 0.1em;
  padding: 0 0.15em;
  border-radius: 0.2rem;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.7em;
  font-weight: 700;
  line-height: 1;
  color: #22d3ee;
  cursor: pointer;
  transition: background-color 0.15s ease;
  /* vertical-align 用 super 而非默认，避免影响行高造成行距不均 */
  vertical-align: super;
}
.markdown-body :deep(.citation-marker:hover) {
  background: rgba(6, 182, 212, 0.25);
}

/* 表格：模型常用表格作对比。
   此前设了 white-space: nowrap 且 display: block，
   导致长内容把列宽撑爆、各行对不齐。
   改为 table-layout: fixed + 允许换行，列宽由浏览器均分。 */
.markdown-body :deep(table) {
  width: 100%;
  margin: 0.7em 0;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 0.88em;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: 0.4em 0.6em;
  border: 1px solid rgba(148, 163, 184, 0.22);
  text-align: left;
  vertical-align: top;
  /* 长英文/URL 不换行会撑破布局 */
  overflow-wrap: break-word;
  word-break: break-word;
}
.markdown-body :deep(th) {
  background: rgba(6, 182, 212, 0.08);
  font-weight: 600;
  color: #e2e8f0;
}
/* 表格内的段落不要额外外边距，否则单元格高度参差 */
.markdown-body :deep(td p),
.markdown-body :deep(th p) {
  margin: 0;
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
