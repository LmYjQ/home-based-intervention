<script setup>
// 反馈录入。
//
// 契约刻意把"执行了什么"和"结果如何"分开（schemas/feedback.py）：
// attempted 只表示家长做了没有，孩子"今天没哭"要填进 child_response
// 或 parent_observations —— 它是一条观察，不等于"上一轮方案有效"。
import { ref } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  trialId: { type: String, required: true },
})
const emit = defineEmits(['done'])

const attempted = ref(true)
const implementationNotes = ref('')
const behavior = ref('')
const affect = ref('')
const words = ref('')
const parentObservations = ref('')
const difficulties = ref('')
const busy = ref(false)
const error = ref('')

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
      turn_id: `turn_fb_${Date.now()}`,
      attempted: attempted.value,
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
      <label>这轮真的做了吗</label>
      <div class="row">
        <label class="row" style="margin:0">
          <input type="radio" style="width:auto" :value="true" v-model="attempted" /> 做了
        </label>
        <label class="row" style="margin:0">
          <input type="radio" style="width:auto" :value="false" v-model="attempted" /> 没做
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