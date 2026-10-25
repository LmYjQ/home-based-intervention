<script setup>
// llm.LLMTrace 的展示面板。
// ★ 这一屏是给面试官看"降级路径不是残次品"的：模型挂了、重试过、
//   截断过、引文没核对过 —— 每一条都明写出来，而不是静悄悄地兜底。
defineProps({ traces: { type: Array, default: () => [] } })
</script>

<template>
  <details v-if="traces?.length" class="trace">
    <summary>模型调用轨迹（{{ traces.length }} 次）</summary>
    <table>
      <thead><tr><th>阶段</th><th>模型</th><th>耗时</th><th>token</th><th>来源</th><th>备注</th></tr></thead>
      <tbody>
        <tr v-for="(t, i) in traces" :key="i" :class="{ bad: t.degraded || t.retries?.length }">
          <td>{{ t.stage }}</td>
          <td class="dim">{{ t.model }}</td>
          <td class="num">{{ t.latency_ms }}ms</td>
          <td class="num">{{ t.prompt_tokens }}/{{ t.completion_tokens }}</td>
          <td>{{ t.source }}</td>
          <td class="note">
            <span v-if="t.degraded">已降级（不是模型给的）</span>
            <span v-if="t.finish_reason === 'length'">输出被截断 · </span>
            <span v-if="t.retries?.length">{{ t.retries.join('; ') }}</span>
            <span v-if="!t.degraded && !t.retries?.length && t.finish_reason !== 'length'">正常</span>
          </td>
        </tr>
      </tbody>
    </table>
  </details>
</template>

<style scoped>
.trace { margin-top: 12px; font-size: 12px; }
summary { cursor: pointer; color: #6b7280; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; }
th, td { padding: 4px 8px; border-bottom: 1px solid #f3f4f6; text-align: left; }
th { color: #9ca3af; font-weight: 500; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.dim, td.note { color: #9ca3af; }
tr.bad td.note { color: #b45309; }
</style>
