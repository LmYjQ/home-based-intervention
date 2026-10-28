<script setup>
// 反馈录入。
//
// **只保留一个对话框**（2026-10-27 改）。
//
// 原来这里有 6 个输入框（执行情况、行为、情绪、说了什么、家长的观察、
// 执行困难），家长要先想清楚每句话该填进哪一栏 —— 负担比"把情况说清楚"
// 本身还大。反馈本来就是一段话，让家长一口气说完，剩下的由模型拆。
//
// **还留着执行状态那三个 radio**：它不是打字，是三个按钮，
// 而且直接决定 trial 的状态（做了 / 中途停了 / 没做 ——
// "能开始但停下来"比"没开始"更有信息量，见 schemas/feedback.py）。
//
// 契约那边没动：`implementation_notes` 接这一整段，
// `child_response` / `parent_observations` / `difficulties` 留空。
// 下游（分析、记忆抽取）读的都是"家长写了什么"，不关心分了哪几栏。
import { ref, watch } from 'vue'
import { streamFeedback } from '../api.js'

const props = defineProps({
  trialId: { type: String, required: true },
})
const emit = defineEmits(['done'])

// attempted / stopped 是**两个字段**，但对家长是一个三选一：
// 做了 / 没做 / 做了但中途停了。
// 中途停止比"没做"更有信息量：说明门槛不在第一步，而是执行不下去。
const execution = ref('attempted')
const notes = ref('')
const busy = ref(false)
const error = ref('')
// 进度：反馈链上有 3 次 LLM 调用，最坏要等好几分钟。
// 只显示"提交中…"的话，家长只能猜是卡了还是在算。
const progressSteps = ref([])

function onProgress(ev) {
  const existing = progressSteps.value.find((s) => s.stage === ev.stage)
  if (existing) {
    existing.attempt = ev.attempt
    return
  }
  progressSteps.value.push({ stage: ev.stage, label: ev.label, attempt: ev.attempt })
}

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

async function submit() {
  busy.value = true
  error.value = ''
  progressSteps.value = []
  try {
    // 走**流式**接口：边跑边推进度。
    //
    // 非流式的时候这里只有一个转圈的按钮，而反馈链最坏要跑 6 分钟
    // （3 次 LLM 调用，每次超时 120 秒、重试 3 次）——
    // 家长看着那个按钮没法判断"是卡了还是在算"。
    const result = await streamFeedback(
      props.trialId,
      {
        // 后端 FeedbackSubmission 要求 trial_id 也在 body 里
        trial_id: props.trialId,
        turn_id: turnId.value,
        attempted: execution.value !== 'not_attempted',
        stopped: execution.value === 'stopped',
        // 一整段都放这儿。下游拼"家长写了什么"时就是这一段。
        implementation_notes: notes.value.trim(),
        child_response: { behavior: '', affect: '', words: '' },
        parent_observations: [],
        difficulties: [],
      },
      { onProgress },
    )
    emit('done', result)
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
    // 提示条留一会儿再消失 —— 立刻消失的话家长根本来不及看。
    setTimeout(() => {
      progressSteps.value = []
    }, 1200)
  }
}
</script>

<template>
  <div class="card">
    <header><h3>记录这轮尝试的反馈</h3><span class="badge truncate" :title="trialId">trial {{ trialId }}</span></header>

    <div class="err-box" v-if="error">{{ error }}</div>

    <div class="field">
      <label>这周进行到哪一步</label>
      <div class="row">
        <label class="row" style="margin:0">
          <input type="radio" style="width:auto" value="attempted" v-model="execution" /> 做了
        </label>
        <label class="row" style="margin:0">
          <input type="radio" style="width:auto" value="stopped" v-model="execution" /> 做了但中途停了
        </label>
        <label class="row" style="margin:0">
          <input type="radio" style="width:auto" value="not_attempted" v-model="execution" /> 这周没机会做
        </label>
      </div>
      <div class="small muted">没机会做也是一种情况，记录下来我们就调整建议。</div>
    </div>

    <!--
      一个框子说完。
      **不做成多栏**：家长要先判断"这句话算执行情况还是算观察"，
      判断的力气比打字还大。该算什么由模型拆。
    -->
    <div class="field">
      <label>这周做了什么、看到了什么</label>
      <textarea
        v-model="notes"
        rows="6"
        placeholder="例如：只在门口抱了 3 分钟就放下，没坚持到平时回家的时长&#10;今天没有哭，但抱着我的手抓得更紧了&#10;还是老人帮忙送的，我控制不了流程"
      />
      <div class="small muted">
        想到什么说什么，不用分栏 —— 做了什么、孩子什么反应、哪里卡住了，
        写在一段里就行。
      </div>
    </div>

    <!-- 进度：正在做什么，而不是一个转圈的按钮 -->
    <div v-if="busy && progressSteps.length" class="progress">
      <div
        v-for="(s, i) in progressSteps"
        :key="s.stage"
        class="prog-step"
        :class="{ last: i === progressSteps.length - 1 }"
      >
        <span class="dot"></span>
        <span>{{ s.label }}</span>
        <span v-if="s.attempt > 1" class="small muted">（第 {{ s.attempt }} 次尝试）</span>
      </div>
    </div>
    <div v-else-if="busy" class="progress">
      <div class="prog-step last">
        <span class="dot"></span>
        <span>正在开始…</span>
      </div>
    </div>

    <button class="primary" :disabled="busy" @click="submit">
      {{ busy ? '处理中…' : '提交反馈' }}
    </button>
  </div>
</template>

<style scoped>
label.row { cursor: pointer; }
</style>
