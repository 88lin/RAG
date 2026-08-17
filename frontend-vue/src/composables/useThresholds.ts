import { ref, onMounted, type Ref } from 'vue';
import { apiService } from '@/services/api';
import type { ThresholdConfig } from '@/types';

/**
 * 相关性阈值 Composable
 *
 * 阈值的唯一真相源是后端 `config.py`，启动时拉一次。
 *
 * **为什么不写成常量**：此前 BrainPanel 硬编码 50、ChatPanel 硬编码 0.75，
 * 而后端实际生效的是 .env 里的第三个值。同一个 relevance = 0.60 会让
 * 仪表盘显示蓝色"足以支撑基于文档的回答"、引用卡片显示橙色警告，
 * 而后端判定不可答、根本没用这些证据 —— 界面上两处互相矛盾且都与
 * 实际行为不符。跨进程的常量副本没有编译器会检查。
 *
 * 模块级单例：多个组件共享同一份，避免各拉一次请求。
 */

// 请求失败时的兜底值。取自 config.py 的默认值，注释里标明它是副本 ——
// 这是唯一允许出现阈值字面量的地方，架构测试对本文件例外。
//
// 为什么不直接不上色：拿不到阈值就让整个界面失去分档，比用一份可能
// 过期的兜底值更糟。而且拉取失败通常意味着后端没起来，那时也没有
// 真实的 relevance 需要上色。
const FALLBACK: ThresholdConfig = {
  retrieval_min: 0.35,
  answerable_min: 0.75,
};

const thresholds: Ref<ThresholdConfig> = ref({ ...FALLBACK });
const loaded = ref(false);
let inflight: Promise<void> | null = null;

async function fetchOnce(): Promise<void> {
  if (loaded.value) return;
  // 多个组件同时挂载时只发一次请求
  if (inflight) return inflight;

  inflight = (async () => {
    try {
      thresholds.value = await apiService.getThresholds();
      loaded.value = true;
    } catch (error) {
      // 不向上抛：阈值拉不到不该让整个界面报错，用兜底值继续
      console.warn('[thresholds] 拉取失败，使用兜底值', error);
    } finally {
      inflight = null;
    }
  })();

  return inflight;
}

export function useThresholds() {
  onMounted(fetchOnce);

  /**
   * 相关性的三档判定。颜色由调用方决定，这里只回答"属于哪一档"。
   *
   * answerable：足以支撑基于文档的回答
   * context   ：可进 prompt 上下文，但不足以判定可答
   * insufficient：视为知识库无答案
   */
  const tierOf = (relevance: number): 'answerable' | 'context' | 'insufficient' => {
    if (relevance >= thresholds.value.answerable_min) return 'answerable';
    if (relevance >= thresholds.value.retrieval_min) return 'context';
    return 'insufficient';
  };

  return { thresholds, loaded, tierOf, refresh: fetchOnce };
}
