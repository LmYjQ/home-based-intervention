<script setup>
// 一轮追问的作答界面。
//
// **交互**：所有问题一起出来，但一次只显示一题，左右切换。
// 家长逐题作答，**最后一次性提交** —— 一轮追问 = 一条消息，
// 和后端的图结构对齐（中断点仍然是"一轮"）。
//
// **选项 vs 自由文本**：和 Claude Code 一样，
// 在输入框里打字就让这一题的选项作废 —— 只提交文字。
// 家长写了"在园更自在，因为老师换了"的时候，
// 不应该再被算成两条重复信息。
//
// 提交的形状是 [{question, answer}, ...]，
// 每条都带原问题 —— 不带的话后端分不清哪段话回答的是哪道题。
import { computed, ref, watch } from 'vue'

const props = defineProps({
  // 后端返回的 questions：[{question, options, why_asking}, ...]
  questions: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['submit'])

const current = ref(0)
// 每题的作答：{ [index]: { text, option } }
const answers = ref({})

// 切题时把当前输入存起来
const draft = ref('')
const activeOption = ref('')

const total = computed(() => props.questions.length)
// 家长写了字 —— 选项作废。
//
// **为什么不能直接用 draft.trim()**：Vue 对布尔属性用
// ``includeBooleanAttr``，空字符串 ``''`` 会被当成 true ——
// ``:disabled="''"`` 渲染成 ``disabled=""``，按钮直接点不动。
// 所以这里必须转成真正的布尔值。
const hasDraft = computed(() => draft.value.trim().length > 0)
const q = computed(() => props.questions[current.value] || null)
const options = computed(() => q.value?.options || [])

// 切题前先存草稿，切回来再取回来
watch(current, (next, prev) => {
  saveDraft(prev)
  loadDraft(next)
})

function saveDraft(idx) {
  if (idx == null || idx < 0) return
  answers.value = {
    ...answers.value,
    [idx]: { text: draft.value, option: activeOption.value },
  }
}

function loadDraft(idx) {
  const saved = answers.value[idx]
  draft.value = saved?.text || ''
  activeOption.value = saved?.option || ''
}

function pickOption(opt) {
  // 已经写了字 —— 选项作废（Claude Code 的行为）。
  // 反过来也一样：选了选项就把输入框清掉。
  if (draft.value.trim()) return
  activeOption.value = activeOption.value === opt ? '' : opt
}

function onInput(e) {
  draft.value = e.target.value
  // 一旦开始打字，选项就作废了
  if (draft.value.trim()) activeOption.value = ''
}

function answerOf(idx) {
  // 当前这题还没存进 answers（存的是切题前的草稿），
  // 所以先看正在编辑的这一题 —— 不然点了选项，进度条上的勾不会立刻出现。
  if (idx === current.value) {
    if (draft.value.trim()) return draft.value.trim()
    if (activeOption.value) return activeOption.value
  }
  const a = answers.value[idx]
  if (!a) return ''
  // 写了字就只用字，忽略选项
  if (a.text?.trim()) return a.text.trim()
  return a.option || ''
}

const answeredCount = computed(
  () => props.questions.filter((_, i) => answerOf(i)).length,
)

const allAnswered = computed(
  () => total.value > 0 && answeredCount.value === total.value,
)

function go(delta) {
  const next = current.value + delta
  if (next < 0 || next >= total.value) return
  // **这里不要再存/取草稿** —— watch(current) 已经做了。
  // 两处都做的话，watch 里 saveDraft(prev) 存的是**新一题**的输入框内容
  // （loadDraft 已经把 draft 换成新题的了），
  // 于是上一题刚选好的答案会被空草稿覆盖掉，进度条上的勾也会消失。
  current.value = next
}

function submit() {
  saveDraft(current.value)
  const payload = []
  props.questions.forEach((item, i) => {
    const ans = answerOf(i)
    if (ans) payload.push({ question: item.question, answer: ans })
  })
  if (!payload.length) return
  emit('submit', payload)
}

defineExpose({ reset })
function reset() {
  current.value = 0
  answers.value = {}
  draft.value = ''
  activeOption.value = ''
}
</script>

<template>
  <div class="card stepper">
    <header>
      <h3>要回答的问题</h3>
      <span class="badge">第 {{ current + 1 }} / {{ total }} 题</span>
      <span class="badge" :class="allAnswered ? 'ok' : ''">
        已答 {{ answeredCount }} / {{ total }}
      </span>
    </header>

    <!-- 进度条：一眼看出还剩几题 -->
    <div class="dots">
      <button
        v-for="(item, i) in questions"
        :key="i"
        class="dot"
        :class="{ active: i === current, done: answerOf(i) }"
        :title="item.question"
        @click="go(i - current)"
      >
        <span class="dot-num">{{ i + 1 }}</span>
        <span v-if="answerOf(i)" class="dot-check">✓</span>
      </button>
    </div>

    <div v-if="q" class="qbox">
      <div class="q-text">{{ q.question }}</div>
      <div v-if="q.why_asking" class="small muted why">（{{ q.why_asking }}）</div>

      <div v-if="options.length" class="opts">
        <button
          v-for="opt in options"
          :key="opt"
          class="opt"
          :class="{ picked: activeOption === opt, disabled: hasDraft }"
          :disabled="hasDraft"
          @click="pickOption(opt)"
        >
          {{ opt }}
        </button>
      </div>
      <div v-else class="small muted">（这题请直接写）</div>

      <div class="field" style="margin:10px 0 0">
        <label>或者自己写</label>
        <textarea
          :value="draft"
          @input="onInput"
          placeholder="Enter 换行，答完切到下一题"
          style="min-height:56px"
        />
      </div>
    </div>

    <div class="row nav">
      <button :disabled="current === 0" @click="go(-1)">← 上一题</button>
      <button v-if="current < total - 1" class="primary" @click="go(1)">下一题 →</button>
      <button v-else class="primary" :disabled="busy || !allAnswered" @click="submit">
        {{ busy ? '提交中…' : '提交回答' }}
      </button>
      <span v-if="!allAnswered" class="small muted">还有 {{ total - answeredCount }} 题没答</span>
    </div>
  </div>
</template>

<style scoped>
.stepper { background: var(--color-surface-soft); }

.dots { display: flex; gap: 8px; margin-bottom: 14px; }
.dot {
  width: 30px; height: 30px; border-radius: 50%; padding: 0;
  position: relative; font-size: 13px; line-height: 1;
  display: flex; align-items: center; justify-content: center;
}
.dot.active { border-color: var(--color-brand); color: var(--color-brand-text); background: var(--color-green-soft); }
.dot.done { border-color: var(--color-ok); color: var(--color-ok); background: var(--color-ok-soft); }
.dot-check { position: absolute; right: -3px; bottom: -3px; font-size: 11px; }

.qbox { border: 1px solid var(--color-border); border-radius: var(--radius-panel); padding: 16px; background: var(--color-surface); }
.q-text { font-size: 16px; margin-bottom: 4px; }
.why { margin-bottom: 12px; }

.opts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.opt { padding: 8px 14px; border-radius: var(--radius-pill); }
.opt.picked { border-color: var(--color-brand); background: var(--color-green-soft); color: var(--color-brand-text); font-weight: 600; }
.opt.disabled { opacity: 0.5; }

.nav { margin-top: 14px; }
</style>
