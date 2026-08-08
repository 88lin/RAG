import { marked } from 'marked';
import DOMPurify from 'dompurify';

/**
 * Markdown 渲染 —— 把 LLM 输出的 markdown 转为安全的 HTML
 *
 * 为什么需要净化：LLM 输出经 markdown 转 HTML 再 v-html 是 XSS 面。
 * 知识库文档可能含恶意内容，模型可能复述其中的 <script> 或 onerror。
 * 不能因为"来源是自己的模型"就跳过净化 —— 内容的真正来源是用户上传的文档。
 *
 * 引用角标为什么内联进 HTML 而不是拆成 Vue 片段：
 *   曾经的做法是按占位符切分 HTML，让调用方用 v-html + 组件交替渲染。
 *   但角标通常位于 <p> 内部，切分会产出未闭合的 `<p>文字`，
 *   浏览器自动闭合后角标落在块级元素之外，导致每个角标强制换行。
 *   改为把角标渲染成带 data 属性的 <sup>，由调用方做事件委托。
 */

marked.setOptions({
  gfm: true,        // 表格、删除线、任务列表
  breaks: true,     // 单换行渲染为 <br>，符合聊天场景直觉
});

/** 允许的标签白名单。不含 script / iframe / form / style / img。 */
const ALLOWED_TAGS = [
  'p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
  'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'span', 'a', 'sup',
];

const ALLOWED_ATTR = ['href', 'title', 'target', 'rel', 'class', 'data-citation'];

/** 引用角标的 class，调用方据此做事件委托 */
export const CITATION_MARKER_CLASS = 'citation-marker';

/**
 * 渲染 markdown 为安全 HTML，并把 [n] 转为可交互角标。
 *
 * 处理顺序很重要：先转 HTML 再替换角标。
 * 反过来做的话，marked 会把 <sup> 里的方括号当链接语法残片处理。
 *
 * @param text 原始 markdown
 * @returns 净化后的 HTML，角标形如
 *          `<sup class="citation-marker" data-citation="1">[1]</sup>`
 */
export function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false }) as string;

  // 只匹配纯数字方括号，避免误伤正文里的 [注]、[见附录] 等
  const withMarkers = html.replace(
    /\[(\d{1,3})\]/g,
    (_m, n) =>
      `<sup class="${CITATION_MARKER_CLASS}" data-citation="${n}">[${n}]</sup>`
  );

  return DOMPurify.sanitize(withMarkers, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ADD_ATTR: ['target', 'rel'],
  });
}

/**
 * 从点击事件中取出角标编号。
 *
 * 用事件委托而非给每个角标绑监听：v-html 插入的 DOM 挂不上 Vue 事件，
 * 且流式输出时 DOM 反复重建，逐个绑定会泄漏监听器。
 *
 * @returns 角标编号，点击处不是角标时返回 null
 */
export function citationNumberFromEvent(event: Event): number | null {
  const target = event.target as HTMLElement | null;
  const marker = target?.closest?.(`.${CITATION_MARKER_CLASS}`);
  if (!marker) return null;
  const raw = marker.getAttribute('data-citation');
  const parsed = raw ? parseInt(raw, 10) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

// ─── 证据原文的关键词高亮 ──────────────────────────────────────────────────────

/** 高亮片段：命中查询词的部分与普通文本交替 */
export type HighlightPart = { text: string; hit: boolean };

/** 中文停用词。这些词在任何文档里都能命中，高亮它们只会制造噪声。 */
const STOPWORDS = new Set([
  '的', '了', '是', '在', '和', '与', '或', '有', '为', '被', '将', '把',
  '吗', '呢', '啊', '吧', '什么', '怎么', '如何', '哪些', '哪个', '多少',
  '可以', '能否', '需要', '要求', '关于', '对于', '以及', '还有',
  '一个', '这个', '那个', '我们', '你们', '他们',
  'the', 'a', 'an', 'is', 'are', 'of', 'to', 'in', 'and', 'or', 'for',
  'what', 'how', 'why', 'can', 'do', 'does',
]);

/**
 * 从查询中提取用于高亮的词。
 *
 * 这只是展示层的启发式，用来帮用户在长 chunk 里快速定位该看哪一段。
 * 它不承担"证据判定"职责 —— 判断某句话是否真被证据支持需要
 * cross-encoder 逐句打分，那是 M3 citation_verify 的事。
 * 正因如此，原文必须完整展示：只截"看起来相关"的片段会丢掉否定词、
 * 例外条件这类改变语义的上下文。
 *
 * 中文没有空格分词，这里用 2-4 字的连续片段做子串匹配。
 * 不引入 jieba 之类的前端分词库：少命中几个词无妨，
 * 多一个依赖要付构建体积与维护成本。
 */
export function extractQueryTerms(query: string): string[] {
  if (!query) return [];

  const blocks = query
    .split(/[\s，。！？、；：""''（）()[\]{}<>~!@#$%^&*+=|\\/?.,'"`-]+/)
    .filter(Boolean);

  const terms = new Set<string>();

  for (const block of blocks) {
    if (STOPWORDS.has(block.toLowerCase())) continue;

    // 英文与数字整体作为一个词
    if (/^[\w.]+$/.test(block)) {
      if (block.length >= 2) terms.add(block.toLowerCase());
      continue;
    }

    // 中文：整块 ≤4 字直接用；更长则滑窗取 2-4 字片段
    if (block.length <= 4) {
      if (block.length >= 2) terms.add(block);
      continue;
    }
    for (let size = 4; size >= 2; size--) {
      for (let i = 0; i + size <= block.length; i++) {
        const piece = block.slice(i, i + size);
        if (!STOPWORDS.has(piece)) terms.add(piece);
      }
    }
  }

  // 长词优先，避免短词把长词切碎（先"敬老卡"再"敬老"）
  return [...terms].sort((a, b) => b.length - a.length).slice(0, 40);
}

/**
 * 把文本按查询词切成高亮片段。
 *
 * 返回片段序列而非 HTML 字符串：调用方用 Vue 模板渲染，
 * 不必再走一遍 sanitize，也就没有注入面。
 */
export function highlightTerms(text: string, terms: string[]): HighlightPart[] {
  if (!text) return [];
  if (!terms.length) return [{ text, hit: false }];

  const lower = text.toLowerCase();
  // 逐字符标记是否落在命中区间内，天然处理词与词的重叠
  const marks = new Array<boolean>(text.length).fill(false);

  for (const term of terms) {
    const needle = term.toLowerCase();
    if (!needle) continue;
    let from = 0;
    while (from <= lower.length - needle.length) {
      const at = lower.indexOf(needle, from);
      if (at === -1) break;
      for (let i = at; i < at + needle.length; i++) marks[i] = true;
      from = at + needle.length;
    }
  }

  const parts: HighlightPart[] = [];
  let start = 0;
  for (let i = 1; i <= text.length; i++) {
    if (i === text.length || marks[i] !== marks[start]) {
      parts.push({ text: text.slice(start, i), hit: marks[start] });
      start = i;
    }
  }
  return parts;
}
