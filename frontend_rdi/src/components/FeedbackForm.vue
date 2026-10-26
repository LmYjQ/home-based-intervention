<script setup>
// 反馈录入。
//
// 契约刻意把"执行了什么"和"结果如何"分开（schemas/feedback.py）：
// attempted 只表示家长做了没有，孩子"今天没哭"要填进 child_response
// 或 parent_observations —— 它是一条观察，不等于"上一轮方案有效"。
import { ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  trialId: { type: String, required: true },
})
const emit = defineEmits(['done'])

// attempted / stopped 是**两个字段**，但对家长是一个三选一：
// 做了 / 没做 / 做了但中途停了。
// 中途停止比"没做"更有信息量：说明门槛不在第一步，而是执行不下去。
const execution = ref('attempted')
const implementationNotes = ref('')
const behavior = ref('')
const affect = ref('')
const words = ref('')
const parentObservations = ref('')
const difficulties = ref('')
const busy = ref(false)
const error = ref('')

// **turn_id 必须稳定**：它是后端的幂等键，也决定反馈追问停在
// 哪条 checkpoint 线程上。如果每次点提交都重新生成，网络抖一下重试
// 就会变成"两次不同的反馈"，追问也接不回来。
const turnId = ref(`turn_fb_${props.trialId}_${Date.now()}`)
watch(
  () => props.trialId,
  (next) => {
    turnId.value = `turn_fb_${next}_${Date.now()}`
  },
)

function toList(text) {
  return text.split('\n').map((s) => s.trim()).filter(Boolean)
}

async function submit() {
  busy.value = true
  error.value = ''
  try {
    const result = await api.submitFeedback(props.trialId, {
      // 后端 FeedbackSubmission 要求 trial_id 也在 body 里
      trial_id: props.trialId,
      turn_id: turnId.value,
      attempted: execution.value !== 'not_attempted',
      stopped: execution.value === 'stopped',
      implementation_notes: implementationNotes.value,
      child_response: {
        behavior: behavior.value,
        affect: affect.value,
        words: words.value,
      },
      parent_observations: toList(parentObservations.value),
      difficulties: toList(difficulties.value),
    })
    emit('done', result)
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card">
    <header><h3>记录这轮尝试的反馈</h3><span class="badge">trial {{ trialId }}</span></header>

    <div class="err-box" v-if="error">{{ error }}</div>

    <div class="field">
      <label>这轮执行到哪一步</label>
      <div class="row">
        <label class="row" style="margin:0">
          <input type="radio" style="width:auto" value="attempted" v-model="execution" /> 做了
        </label>
        <label class="row" style="margin:0">
          <input type="radio" style="width:auto" value="stopped" v-model="execution" /> 做了但中途停了
        </label>
        <label class="row" style="margin:0">
          <input type="radio" style="width:auto" value="not_attempted" v-model="execution" /> 没做
        </label>
      </div>
    </div>

    <div class="field">
      <label>执行情况（做了哪些、实际怎么做）</label>
      <textarea v-model="implementationNotes" placeholder="例如：只在门口抱了 3 分钟就放下，没坚持到平时回家的时长" />
    </div>

    <div class="field">
      <label>孩子的反应</label>
      <div class="grid3">
        <div>
          <label class="small muted">行为</label>
          <input v-model="behavior" placeholder="蹲下、拍手" />
        </div>
        <div>
          <label class="small muted">情绪</label>
          <input v-model="affect" placeholder="平静、焦虑" />
        </div>
        <div>
          <label class="small muted">说了什么</label>
          <input v-model="words" placeholder="原话" />
        </div>
      </div>
    </div>

    <div class="field">
      <label>家长的观察（一行一条）</label>
      <textarea v-model="parentObservations" placeholder="今天没有哭&#10;但抱着我的手抓得更紧了" />
    </div>

    <div class="field">
      <label>执行困难（一行一条）</label>
      <textarea v-model="difficulties" placeholder="老人帮忙送，无法控制流程" />
    </div>

    <button class="primary" :disabled="busy" @click="submit">
      {{ busy ? '提交中…' : '提交反馈' }}
    </button>
  </div>
</template>

<style scoped>
.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
@media (max-width: 620px) { .grid3 { grid-template-columns: 1fr; } }
label.row { cursor: pointer; }
</style>