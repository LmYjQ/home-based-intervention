<script setup>
// 手动测试台。
//
// 这是个**调试工具**，不是产品界面，所以：
//   - 后端返回什么就显示什么，不做美化加工（加工会掩盖真实问题）
//   - 每个接口的原始 JSON 都留着，方便对着验收点逐条核
//   - status 直接用后端的五种取值渲染，不自己发明一套状态
//
// 走通的顺序：建会话 → 发消息（可能停在追问）→ 答完追问
// → 停在方案选择 → 选一张 → trial 落库 → 录反馈。
import { computed, onMounted, ref } from 'vue'
import { api } from './api.js'
import PlanCard from './components/PlanCard.vue'
import EvidencePanel from './components/EvidencePanel.vue'
import FeedbackForm from './components/FeedbackForm.vue'

const DEMO_ANSWERS = [
  '早上从家门口走到楼下大概五分钟，一直抱着我腿不说话。到幼儿园门口就大哭，抱起来能好。送进去以后老师说她今天自己吃了早饭。',
  '在家不这样，只有去幼儿园才哭。哭的时候脸涨得通红，说不出话。试过提前十分钟出门，也试过抱着她走过去，都没什么用。',
  '目标是能正常入园、别在门口哭半小时。她喜欢玩积木，在幼儿园午睡也正常。只要别在门口堵着就行，晚十分钟到也接受。',
]

// ---- 状态 ----
const health = ref(null)
const conversationId = ref('')
const displayLabel = ref('孩子')
const ageBand = ref('3-4岁')

const messages = ref([])
const status = ref('')
const questions = ref([])
const plans = ref([])
const selectedPlanId = ref('')
const evidence = ref([])
const evidenceDecision = ref({})
const evidenceGaps = ref([])
const trialId = ref('')
const finalMessage = ref('')
const feedbackResult = ref(null)

const input = ref('')
const busy = ref(false)
const error = ref('')
const lastRaw = ref(null)

// ---- 派生 ----
// 只认后端真的声明过的状态，别猜。
const awaitingFollowup = computed(() => status.value === 'awaiting_followup')
const awaitingPlanSelection = computed(() => status.value === 'awaiting_plan_selection')
const canSend = computed(() => conversationId.value && !busy.value && !awaitingPlanSelection.value)
const needsFeedback = computed(() => Boolean(trialId.value) && !feedbackResult.value)

const STATUS_TEXT = {
  awaiting_followup: ['追问中', 'warn'],
  awaiting_plan_selection: ['待选方案', 'accent'],
  limited_answer: ['有限回答', 'warn'],
  needs_professional_support: ['建议转专业支持', 'err'],
  completed: ['已完成', 'ok'],
  interrupted: ['中断', 'warn'],
}

const STATUS_HINT = {
  awaiting_followup: '图停在 await_user_answer 中断上。家长答完这一轮才会继续往下走。',
  awaiting_plan_selection: '图停在 present_plans 中断上 —— 必须调 select-plan 才会执行 commit_plan_selection，trial 才落库。',
  limited_answer: '证据不够，给了有限回答，没有方案卡。',
  needs_professional_support: '门控判成高风险，建议转专业支持。',
  completed: '本轮跑完了。',
}

// ---- 动作 ----
function push(role, text, meta = {}) {
  messages.value.push({ role, text, at: new Date().toLocaleTimeString(), ...meta })
}

async function checkHealth() {
  health.value = null
  try {
    health.value = await api.health()
  } catch (e) {
    health.value = { status: 'unreachable', llm: { error: e.message } }
  }
}

async function run(fn, { keepMsgs = true } = {}) {
  busy.value = true
  error.value = ''
  try {
    const data = await fn()
    lastRaw.value = data
    return data
  } catch (e) {
    error.value = e.message
    // 错误也留一份，手测时能对着原始响应看
    lastRaw.value = { error: e.message }
    if (!keepMsgs) throw e
    return null
  } finally {
    busy.value = false
  }
}

async function createConversation() {
  const data = await run(() =>
    api.createConversation({ display_label: displayLabel.value, age_band: ageBand.value }),
  )
  if (!data) return
  conversationId.value = data.conversation_id
  messages.value = []
  status.value = ''
  questions.value = []
  plans.value = []
  selectedPlanId.value = ''
  trialId.value = ''
  finalMessage.value = ''
  evidence.value = []
  evidenceDecision.value = {}
  evidenceGaps.value = []
  feedbackResult.value = null
}

function applyTurn(data) {
  status.value = data.status
  questions.value = data.questions || []
  evidence.value = data.evidence || []
  evidenceDecision.value = data.evidence_decision || {}
  evidenceGaps.value = data.evidence_gaps || []
  finalMessage.value = data.message || ''

  // 方案卡在两个地方出现：select-plan 之后从 GET 查，或本轮 plans 里带回来
  if (data.plans?.length) plans.value = data.plans

  if (data.status === 'awaiting_followup') {
    push('bot', (data.questions || []).map((q, i) => `${i + 1}. ${q}`).join('\n'))
  } else if (data.message) {
    push('bot', data.message)
  }
}

async function send(text) {
  const msg = (text ?? input.value).trim()
  if (!msg || !canSend.value) return
  input.value = ''
  push('user', msg)

  const data = await run(() => api.sendMessage(conversationId.value, { message: msg }))
  if (data) applyTurn(data)
}

async function answerFollowups() {
  const msg = input.value.trim()
  if (!msg) return
  const data = await run(() => api.sendMessage(conversationId.value, { message: msg }))
  if (data) {
    input.value = ''
    applyTurn(data)
  }
}

async function selectPlan(planId) {
  selectedPlanId.value = planId
  const data = await run(() =>
    api.selectPlan(conversationId.value, { plan_id: planId }),
  )
  if (!data) return
  status.value = data.status
  trialId.value = data.trial_id || ''
  finalMessage.value = data.message || ''
  push('bot', finalMessage.value || '已选定方案。')
  await refresh()
}

async function submitFeedback(result) {
  feedbackResult.value = result
  finalMessage.value = result.message || finalMessage.value
  push('bot', result.message || '反馈已记录。')
}

async function refresh() {
  if (!conversationId.value) return
  const data = await run(() => api.getConversation(conversationId.value))
  if (!data) return
  status.value = data.status
  evidence.value = data.evidence || []
  evidenceDecision.value = data.evidence_decision || {}
  // GET 返回的 plans 是数据库里的已落库方案，和本轮的候选不是一回事
  if (data.plans?.length && !plans.value.length) plans.value = data.plans
}

onMounted(checkHealth)
</script>

<template>
  <div class="app">
    <div class="topbar">
      <h1>RDI 家长教练 · 手动测试台</h1>

      <span v-if="health" class="badge" :class="health.status === 'ok' ? 'ok' : 'err'">
        后端 {{ health.status }}
      </span>
      <span v-if="health?.llm" class="badge" :class="health.llm.llm_ok ? 'ok' : 'err'">
        LLM {{ health.llm.model }} · {{ health.llm.mode }}
      </span>
      <span v-if="health?.database" class="badge">{{ health.database.server?.split(' on ')[0] }}</span>
      <span v-if="health?.rerank_enabled" class="badge">rerank on</span>

      <span class="spacer"></span>
      <span v-if="status" class="badge" :class="STATUS_TEXT[status]?.[1] || ''">
        {{ STATUS_TEXT[status]?.[0] || status }}
      </span>
      <button @click="checkHealth">重新体检</button>
      <button @click="refresh" :disabled="!conversationId || busy">刷新会话</button>
    </div>

    <div v-if="health && health.status !== 'ok'" class="err-box">
      后端自检没过：{{ JSON.stringify(health, null, 2) }}
    </div>
    <div v-if="error" class="err-box">{{ error }}</div>
    <div v-if="status && STATUS_HINT[status]" class="notice">{{ STATUS_HINT[status] }}</div>

    <div class="cols">
      <!-- 左：对话与方案 -->
      <div>
        <div class="card">
          <header>
            <h3>会话</h3>
            <span v-if="conversationId" class="badge small">{{ conversationId }}</span>
          </header>

          <div class="row" style="margin-bottom:10px">
            <div class="field" style="flex:1;margin:0">
              <label>孩子称呼</label>
              <input v-model="displayLabel" />
            </div>
            <div class="field" style="flex:1;margin:0">
              <label>年龄段</label>
              <input v-model="ageBand" />
            </div>
            <button style="align-self:flex-end" @click="createConversation" :disabled="busy">
              新建会话
            </button>
          </div>

          <div class="msgs">
            <div v-if="!messages.length" class="small muted">
              还没开始。先「新建会话」，再在下面说孩子的表现。
            </div>
            <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
              <div class="who">{{ m.role === 'user' ? '家长' : '教练' }} · {{ m.at }}</div>
              {{ m.text }}
            </div>
          </div>

          <div v-if="awaitingFollowup && questions.length" class="card"
               style="background:var(--panel-2);margin:0 0 12px">
            <h4 style="margin-bottom:6px">要回答的问题</h4>
            <ul style="margin:0;padding-left:18px">
              <li v-for="(q, i) in questions" :key="i">{{ q }}</li>
            </ul>
          </div>

          <div class="field">
            <label>{{ awaitingFollowup ? '回答追问' : '描述孩子的情况' }}</label>
            <textarea v-model="input"
                      @keydown.enter.exact.prevent="awaitingFollowup ? answerFollowups() : send()"
                      :placeholder="awaitingFollowup ? 'Enter 直接提交' : 'Enter 直接发送'" />
          </div>

          <div class="row">
            <button class="primary"
                    :disabled="!canSend || !input.trim()"
                    @click="awaitingFollowup ? answerFollowups() : send()">
              {{ busy ? '处理中…（LLM 可能要 30-90s）' : awaitingFollowup ? '提交回答' : '发送' }}
            </button>
            <span v-if="busy" class="small muted">图在跑，别重复点</span>
          </div>

          <details style="margin-top:10px">
            <summary>快捷填入演示答案（回车逐条提交）</summary>
            <button v-for="(a, i) in DEMO_ANSWERS" :key="i" class="small"
                    style="margin:2px 4px 2px 0"
                    @click="input = a; answerFollowups()">
              第 {{ i + 1 }} 条
            </button>
          </details>
        </div>

        <!-- 方案卡 -->
        <div class="card" v-if="plans.length">
          <header>
            <h3>候选方案</h3>
            <span class="badge">{{ plans.length }} 张</span>
            <span v-if="selectedPlanId" class="badge accent">已选 {{ selectedPlanId }}</span>
          </header>
          <PlanCard v-for="p in plans" :key="p.plan_id" :plan="p"
                    :selectable="awaitingPlanSelection"
                    :selected="p.plan_id === selectedPlanId"
                    @select="selectPlan" />
        </div>

        <div v-if="awaitingPlanSelection" class="notice">
          还没选方案 —— 不选的话图不会往下走，trial 也不会落库。
        </div>

        <div class="card" v-if="finalMessage && !awaitingFollowup && !awaitingPlanSelection">
          <header><h3>教练回复</h3></header>
          <div style="white-space:pre-wrap">{{ finalMessage }}</div>
        </div>

        <FeedbackForm v-if="needsFeedback" :trial-id="trialId" @done="submitFeedback" />

        <div class="card" v-if="feedbackResult">
          <header><h3>反馈结果</h3></header>
          <dl class="kv">
            <dt>实际执行</dt><dd>{{ feedbackResult.analysis?.actual_execution || '—' }}</dd>
            <dt>观察到的变化</dt>
            <dd>
              <div v-for="(c, i) in feedbackResult.analysis?.observed_changes || []" :key="i">{{ c }}</div>
              <span v-if="!feedbackResult.analysis?.observed_changes?.length">—</span>
            </dd>
            <dt>与方案的关系</dt><dd>{{ feedbackResult.analysis?.relation_to_plan || '—' }}</dd>
            <dt>置信说明</dt><dd>{{ feedbackResult.analysis?.confidence_note || '—' }}</dd>
            <dt>下一步</dt>
            <dd>
              <div v-for="(s, i) in feedbackResult.next_step?.next_steps || []" :key="i">{{ s }}</div>
              <span v-if="!feedbackResult.next_step?.next_steps?.length">—</span>
            </dd>
          </dl>
        </div>
      </div>

      <!-- 右：证据与原始响应 -->
      <div>
        <EvidencePanel :evidence="evidence" :decision="evidenceDecision" :gaps="evidenceGaps" />

        <div class="card" v-if="lastRaw">
          <header>
            <h3>上一次接口的原始响应</h3>
            <span class="badge small">对照验收点用</span>
          </header>
          <pre>{{ JSON.stringify(lastRaw, null, 2) }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>