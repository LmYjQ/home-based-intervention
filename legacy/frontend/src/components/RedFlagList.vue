<script setup>
// 红旗信号。★ 纯正则规则扫描，零 token，且不依赖模型是否可用 ——
//   这正是不把它做成 LLM schema 字段的第二个理由（见 redflag.py）。
//   urgent 级置顶：blocking_codes 是前端据此强制排到最前面的依据。
defineProps({ report: { type: Object, default: null } })
</script>

<template>
  <div v-if="report?.hits?.length" class="flags">
    <div v-for="h in report.hits" :key="h.code" class="flag" :class="h.severity">
      <span class="tag">{{ h.severity }}</span>
      <b>{{ h.group }}</b>（{{ h.code }}）：{{ h.quote }}
      <div class="ask">建议追问：{{ h.ask }}</div>
    </div>
  </div>
  <p v-else class="clean">未发现红旗信号。</p>
</template>

<style scoped>
.flags { background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px;
  padding: 10px 14px; margin: 12px 0; }
.flag { font-size: 13px; padding: 5px 0; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px;
  margin-right: 6px; color: #fff; background: #9ca3af; }
.urgent .tag { background: #dc2626; }
.high .tag { background: #ea580c; }
.watch .tag { background: #ca8a04; }
.urgent { font-weight: 600; }
.ask { color: #991b1b; margin-left: 4px; }
.clean { color: #16a34a; font-size: 13px; }
</style>
