import { marked } from 'marked';
import DOMPurify from 'dompurify';

/**
 * Markdown 渲染 —— 把 LLM 输出的 markdown 转为安全的 HTML
 *
 * 为什么需要净化：LLM 输出经 markdown 转 HTML 再 v-html 是 XSS 面。
 * 知识库文档可能含恶意内容，模型可能复述其中的 <script> 或
 * onerror 属性。不能因为"来源是自己的模型"就跳过净化 ——
 * 内容的真正来源是用户上传的文档。
 *
 * 为什么要占位符：正文里的 [n] 引用角标需要保持可点击（绑定事件、
 * 高亮联动），而 v-html 渲染出的 DOM 无法挂 Vue 事件。
 * 因此把角标先换成占位标记，渲染后由调用方拆分成 Vue 组件片段。
 */

// 角标占位符。用私有区字符包裹，避免与正文内容冲突 ——
// 若用 {{1}} 这类可见形式，文档里恰好出现同样字符串就会被误认。
const CITATION_OPEN = '';
const CITATION_CLOSE = '';

marked.setOptions({
  gfm: true,        // 表格、删除线、任务列表
  breaks: true,     // 单换行也渲染为 <br>，符合聊天场景的直觉
});

/** 允许的标签白名单。不含 script / iframe / form / style。 */
const ALLOWED_TAGS = [
  'p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
  'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'span', 'a',
];

const ALLOWED_ATTR = ['href', 'title', 'target', 'rel', 'class'];

/**
 * 把 [n] 角标替换为占位符，避免 markdown 渲染破坏它们。
 *
 * 注意 markdown 会把 [1] 当成链接语法的一部分（[text](url) 的残片），
 * 不做保护会出现角标丢失或格式错乱。
 */
export function protectCitations(text: string): string {
  return text.replace(/\[(\d+)\]/g, `${CITATION_OPEN}$1${CITATION_CLOSE}`);
}

/**
 * 渲染 markdown 为安全 HTML。
 *
 * @param text 原始 markdown（角标应已被 protectCitations 处理）
 * @returns 净化后的 HTML 字符串
 */
export function renderMarkdown(text: string): string {
  const raw = marked.parse(text, { async: false }) as string;
  return DOMPurify.sanitize(raw, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // 外链统一新窗口打开且断开 referrer
    ADD_ATTR: ['target', 'rel'],
  });
}

/** HTML 片段与引用角标交替的序列 */
export type RenderedPart =
  | { type: 'html'; html: string }
  | { type: 'citation'; number: number };

/**
 * 渲染 markdown 并把角标占位符拆成独立片段。
 *
 * 调用方据此渲染：html 片段用 v-html，citation 片段用 Vue 组件
 * （从而保留点击与高亮联动）。
 */
export function renderWithCitations(text: string): RenderedPart[] {
  const html = renderMarkdown(protectCitations(text));
  const parts: RenderedPart[] = [];
  const regex = new RegExp(`${CITATION_OPEN}(\\d+)${CITATION_CLOSE}`, 'g');

  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(html)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ type: 'html', html: html.slice(lastIndex, match.index) });
    }
    parts.push({ type: 'citation', number: parseInt(match[1], 10) });
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < html.length) {
    parts.push({ type: 'html', html: html.slice(lastIndex) });
  }

  return parts;
}
