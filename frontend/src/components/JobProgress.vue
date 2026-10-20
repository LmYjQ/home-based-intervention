<script setup>
// 建档 / 出卡片的进度。llm.py 的 on_step 回调在这里变成一行行字。
defineProps({ steps: { type: Array, default: () => [] }, done: Boolean })
</script>

<template>
  <ul class="steps">
    <li v-for="(s, i) in steps" :key="i" :class="{ last: i === steps.length - 1 && !done }">
      <span class="t">+{{ (s.t / 1000).toFixed(1) }}s</span>{{ s.text }}
    </li>
    <li v-if="done" class="last done">— 完成</li>
  </ul>
</template>

<style scoped>
.steps { list-style: none; padding: 0; margin: 8px 0; font-size: 13px; color: #666; }
.steps li { padding: 2px 0; border-left: 2px solid #eee; padding-left: 10px; margin-left: 2px; }
.steps li.last { border-left-color: #3b82f6; }
.steps li.done { border-left-color: #22c55e; }
.t { display: inline-block; min-width: 52px; color: #aaa; font-variant-numeric: tabular-nums; }
</style>
