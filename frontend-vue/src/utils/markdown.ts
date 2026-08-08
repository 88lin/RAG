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
