<script setup>
// 状态机 + 日结。**两个接口都是纯 Python / 纯 SQL，秒回，不调模型。**
// 这是这个 demo 唯一的"零 token"部分 —— 判定不该由模型做。
import { ref, onMounted, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({ childId: String })
const emit = defineEmits([])

const prog = ref(null)
const sums = ref([])
const err = ref('')

async function load() {
  if (!props.childId) return
  err.value = ''
  try {
    const [p, s] = await Promise.all([
      api.progress(props.childId), api.summaries(props.childId),
    ])
    prog.value = p; sums.value = s.summaries || []
  } catch (e) { err.value = e.message }
}
onMounted(load); watch(() => props.childId, load)
defineExpose({ load })

const ARROW = { up: '↑', flat: '→', down: '↓' }
const MAG = { none: '无', small: '轻微', medium: '中等', large: '明显' }
</script>

<template>
  <section class="card">
    <h2>状态机与日结 <span class="dim">（纯 SQL / 纯 Python，秒回，不调模型）</span></h2>
    <p v-if="err" class="err">{{ err }}</p>

    <div v-if="prog" class="prog">
      <p class="text">{{ prog.text }}</p>
      <details v-if="Object.keys(prog.states || {}).length">
        <summary>当前各技能状态</summary>
        <table>
          <tr v-for="(v, k) in prog.states" :key="k">
            <td>{{ k }}</td><td class="dim">{{ v.state }}</td><td class="dim">since {{ v.since || '—' }}</td>
          </tr>
        </table>
      </details>
    </div>

    <h3>日结（落 daily_summary 表，明天建候选时会被读）</h3>
    <div v-if="sums.length" class="sums">
      <div v-for="s in sums.slice().reverse()" :key="s.date + Math.random()" class="sum">
        <div class="date">{{ s.date }}<span v-if="s.degraded" class="dim"> · 降级（只报事实，不猜方向）</span></div>
        <div v-for="d in s.domain_updates" :key="d.domain" class="du">
          <b>{{ ARROW[d.direction] }}</b> {{ d.magnitude ? MAG[d.magnitude] : '' }}
          {{ d.rationale }}
        </div>
        <div v-for="(f, i) in s.flags" :key="i" class="flag">⚠ {{ f }}</div>
        <div v-if="s.parent_note" class="note">给家长：{{ s.parent_note }}</div>
      </div>
    </div>
    <p v-else class="dim">还没有日结 —— 提交一天的反馈后再点「收尾（日结）」。</p>
  </section>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 18px; margin-bottom: 18px; }
h2 { margin: 0 0 10px; font-size: 17px; }
h3 { font-size: 14px; margin: 16px 0 6px; }
.dim { color: #888; font-size: 12px; font-weight: 400; }
.prog .text { font-size: 13px; white-space: pre-wrap; background: #f9fafb;
  padding: 10px 12px; border-radius: 6px; margin: 0 0 8px; }
table { border-collapse: collapse; font-size: 12px; }
td { padding: 3px 10px 3px 0; }
.sums { font-size: 13px; }
.sum { border-left: 3px solid #e5e7eb; padding: 6px 0 6px 12px; margin-bottom: 10px; }
.sum .date { color: #6b7280; font-size: 12px; margin-bottom: 2px; }
.du { padding: 1px 0; }
.du b { color: #2563eb; }
.flag { color: #b45309; }
.note { color: #4b5563; margin-top: 3px; }
.err { color: #dc2626; font-size: 13px; }
summary { cursor: pointer; font-size: 12px; color: #6b7280; }
</style>
