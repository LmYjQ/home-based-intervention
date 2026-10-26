<script setup>
// 计划审批（Phase 3）
//
// ★ 这个面板是「技术方案 §5.1 的审批 interrupt」在前端的落点 ——
//   家长的计划第一次要由人拍板，而不是系统单方面排完就端出来。
//
// ★ 三个按钮的语义完全不同，别把它们做成同一个操作的三种说法：
//   批准 —— 这份安排我认，照这个做
//   修改 —— 安排的方向不对（note 写清哪里不对），系统重新排一版
//   拒绝 —— 这份安排我们做不到（note 是给教练看的，为什么做不到）
//
// ★ 为什么显示「变了什么」而不只是问「同意吗」：
//   一个光秃秃的「同意/不同意」按钮等于让人盲签。
//   理由是后端算的（`validate_plan` 的 `changes`），前端只显示 ——
//   与 quality 那条规矩一样：判定在服务端，前端不自己推。
import { ref, computed } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  CHILD: String,
  date: String,
  // 后端给的整份结果（`/api/plan/status` 的返回）
  plan: Object,
})
const emit = defineEmits(['reviewed'])

const busy = ref(false)
const err = ref('')
const note = ref('')

const pending = computed(() => props.plan?.phase === 'review')
const changes = computed(() => props.plan?.changes || [])
const acts = computed(() => props.plan?.activities || [])

async function review(action) {
  if (busy.value) return
  busy.value = true; err.value = ''
  try {
    // ★ 连续性靠 (child_id, date) → thread_id，不靠 job_id ——
    //   前端不持有任何 interrupt 状态，那是服务端的事（刷新就没了）。
    const r = await api.planReview({
      child_id: props.CHILD, date: props.date,
      action, note: note.value,
    })
    note.value = ''
    emit('reviewed', r)
  } catch (e) {
    // ★ 409 = 没有待审批的计划（可能已经批过了，或压根没跑过）。
    //   这不是"出错了"，前端不弹错，只是刷新一次状态。
    if (String(e.message).includes('没有待审批')) {
      emit('reviewed', null)
    } else {
      err.value = e.message
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section v-if="pending" class="review">
    <h4>⚡ 今天的安排变了，先看一眼</h4>
    <p class="why">
      系统重新排了一遍，和上一版比有这些地方不一样 —— 你认一下，我们再开始。
    </p>
    <ul v-if="changes.length" class="changes">
      <li v-for="(c, i) in changes" :key="i">{{ c }}</li>
    </ul>
    <p v-else class="hint">（没有具体变化，但这一版需要你确认）</p>

    <p v-if="acts.length" class="acts">
      今天排的是：<b>{{ acts.join('、') }}</b>
      <span class="dim">（{{ plan.total_minutes }} 分钟）</span>
    </p>
    <p v-if="plan.focus_reason" class="hint">重点：{{ plan.focus_reason }}</p>

    <textarea v-model="note" rows="2"
      placeholder="要改的话写一句（可选）：哪里不合适、为什么做不到"></textarea>

    <div class="row">
      <button class="mini-btn" :disabled="busy" @click="review('approve')">
        {{ busy ? '处理中…' : '就这样，开始' }}
      </button>
      <button class="ghost" :disabled="busy" @click="review('edit')">
        换一版
      </button>
      <button class="ghost danger" :disabled="busy" @click="review('reject')">
        这个我们做不到
      </button>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
  </section>

  <!-- 已批完：只留一行说明，不占地方 -->
  <section v-else-if="plan?.phase === 'done' && plan?.plan_status" class="done">
    <span class="dim">
      今天的计划已确认
      <template v-if="plan.plan_status === 'rejected'">（你说过这个安排做不到，已记下）</template>
      <template v-if="plan.plan_status === 'paused'">（因为有红旗还没处理，今天先不排）</template>
    </span>
  </section>
</template>

<style scoped>
.review { border: 1px solid #bfdbfe; background: #eff6ff;
  border-radius: 8px; padding: 12px 14px; margin-top: 12px; }
.review h4 { margin: 0 0 6px; color: #1d4ed8; }
.why { font-size: 13px; color: #374151; margin: 4px 0; }
.changes { margin: 8px 0; padding-left: 20px; font-size: 13px; color: #1e40af; }
.changes li { margin: 2px 0; }
.acts { font-size: 13px; margin: 8px 0 4px; }
.hint { font-size: 12px; color: #888; margin: 4px 0; }
.dim { color: #888; font-size: 12px; }
textarea { width: 100%; box-sizing: border-box; font-family: inherit;
  font-size: 13px; padding: 8px 10px; border: 1px solid #d1d5db;
  border-radius: 6px; margin-top: 6px; }
.row { display: flex; gap: 10px; align-items: center; margin-top: 8px; }
.mini-btn { background: #2563eb; color: #fff; border: 0; border-radius: 6px;
  padding: 8px 16px; font-size: 13px; cursor: pointer; }
.mini-btn:disabled { background: #9ca3af; }
.ghost { background: none; border: none; color: #2563eb;
  font-size: 12px; cursor: pointer; text-decoration: underline; }
.ghost.danger { color: #b91c1c; }
.err { color: #dc2626; font-size: 13px; }
.done { margin-top: 10px; }
</style>
