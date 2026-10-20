<script setup>
// ★ 一条活动的反馈（阶段 4b 的核心形态）。
//
// 家长**逐条点**，不是写一段话。所以：
//   · outcome 由家长点，不让模型猜 —— 模型猜会把一个确定的答案变成
//     一个可能有错的答案（家长点了「不配合」，模型可能判成 partial）
//   · quality 不在这里算，★ 前端绝不算 ★ —— 后端 feedback.quality_for()
//     是唯一的 quality 来源。状态机的升级闸门拿平均 quality 比 0.5/0.75，
//     两套口径必然打架。
//   · 「跳过」和「不配合」是两件事：跳过 = 卡片发了没来得及做；
//     不配合 = 想做但孩子不配合。前者不该扣活动的能力分。
import { ref, computed, watch } from 'vue'

const props = defineProps({
  activity: { type: Object, required: true },   // {activity_id,title,difficulty,duration_min}
  nSteps: { type: Number, default: 3 },
  value: { type: Object, required: true },
  // 后端渲染的表单说明文字（record_prompt 等）。前端不自己拼活动步骤。
  formText: { type: String, default: '' },
})
const emit = defineEmits(['update'])

const OUTCOMES = [
  { k: 'success', label: '做出来了', hint: '孩子基本独立完成' },
  { k: 'partial', label: '做了一部分', hint: '中途停下或只做了几步' },
  { k: 'refused', label: '不配合', hint: '想做但孩子不配合 —— 这条会被记一次抗拒' },
  { k: 'skipped', label: '跳过', hint: '卡片发了没来得及做 —— 不计入抗拒' },
]
const PROMPT_LEVELS = ['独立做到', '手势或口头提示', '示范给他看', '动手辅助']

// v-model 到父组件：直接改 props.value 的字段并 emit，避免本地副本和提交值走散
function set(patch) { emit('update', { ...props.value, ...patch }) }

const showLadder = computed(() => props.value.outcome === 'success' || props.value.outcome === 'partial')
const showSteps = computed(() => props.value.outcome === 'partial')

watch(() => props.value.outcome, (o) => {
  // 选了 refused/skipped 还留着"用到第几级"是没意义的输入。
  // 清掉而不是留着 —— 留着会让后端拿到一对互相矛盾的答案，
  // 而 quality_for 对非 success 恒返回 0，这个字段此时是纯噪音。
  if (o === 'refused' || o === 'skipped') set({ prompt_level: 0, steps_done: 0 })
})
</script>

<template>
  <div class="item">
    <div class="head">
      <span class="id">{{ activity.activity_id }}</span>
      <b>{{ activity.title }}</b>
      <span class="dim">难度{{ activity.difficulty }} · 约 {{ activity.duration_min }} 分钟</span>
    </div>

    <pre v-if="formText" class="form">{{ formText }}</pre>

    <div class="q">结果（点一个）</div>
    <div class="outcomes">
      <button v-for="o in OUTCOMES" :key="o.k"
              class="ob" :class="[o.k, { on: value.outcome === o.k }]"
              :title="o.hint" @click="set({ outcome: o.k })">
        {{ o.label }}
      </button>
    </div>
    <div class="hint">{{ (OUTCOMES.find(o => o.k === value.outcome) || {}).hint || '' }}</div>

    <div v-if="showLadder" class="q">用到第几级提示？（提示越重，quality 越低）</div>
    <div v-if="showLadder" class="ladder">
      <button v-for="(p, i) in PROMPT_LEVELS" :key="i"
              class="lv" :class="{ on: value.prompt_level === i }"
              @click="set({ prompt_level: i })">
        {{ i + 1 }}. {{ p }}
      </button>
    </div>

    <div v-if="showSteps" class="q">做到第几步（共 {{ nSteps }} 步）</div>
    <div v-if="showSteps" class="steps">
      <button v-for="i in nSteps" :key="i" class="lv"
              :class="{ on: value.steps_done === i }" @click="set({ steps_done: i })">{{ i }}</button>
    </div>

    <div class="q">补充一句（选填）<span class="dim">—— 只有写了这里才会调模型抽事件</span></div>
    <input :value="value.note" placeholder="例如：他今天一坐下就跑开了"
           @input="set({ note: $event.target.value })" />
  </div>
</template>

<style scoped>
.item { border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; background: #fff; }
.head { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; margin-bottom: 8px; }
.id { font-family: ui-monospace, monospace; font-size: 12px; color: #6b7280; }
.dim { color: #888; font-size: 12px; }
.form { background: #f9fafb; border: 1px solid #eef2f7; border-radius: 6px;
  padding: 8px 10px; font-size: 12px; color: #555; white-space: pre-wrap;
  font-family: inherit; margin: 0 0 10px; }
.q { font-size: 13px; color: #374151; margin: 10px 0 5px; }
.outcomes, .ladder, .steps { display: flex; gap: 6px; flex-wrap: wrap; }
.ob, .lv { border: 1px solid #d1d5db; background: #fff; border-radius: 6px;
  padding: 6px 14px; font-size: 13px; cursor: pointer; }
.ob.on, .lv.on { color: #fff; }
.ob.success.on { background: #16a34a; border-color: #16a34a; }
.ob.partial.on { background: #ca8a04; border-color: #ca8a04; }
.ob.refused.on { background: #dc2626; border-color: #dc2626; }
.ob.skipped.on { background: #6b7280; border-color: #6b7280; }
.lv.on { background: #2563eb; border-color: #2563eb; }
.lv { padding: 5px 11px; font-size: 12px; }
.hint { font-size: 12px; color: #888; margin-top: 5px; min-height: 16px; }
input { width: 100%; box-sizing: border-box; padding: 7px 9px; font-size: 13px;
  border: 1px solid #d1d5db; border-radius: 6px; }
</style>
