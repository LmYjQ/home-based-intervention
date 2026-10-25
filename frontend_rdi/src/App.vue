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
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { api, streamMessage } from './api.js'
import PlanCard from './components/PlanCard.vue'
import EvidencePanel from './components/EvidencePanel.vue'
import FeedbackForm from './components/FeedbackForm.vue'
import FollowupStepper from './components/FollowupStepper.vue'

// 快捷填入的演示回答。只问一轮（后端 MAX_FOLLOWUP_ROUNDS = 1），
// 所以一条回答就把场景、行为、试过什么、目标都补齐了。
const DEMO_ANSWERS = [
  '早上从家门口走到楼下大概五分钟，一直抱着我腿不说话。到幼儿园门口就大哭，抱起来能好。在家不这样，只有去幼儿园才哭。试过提前十分钟出门，也试过抱着她走过去，都没什么用。目标是能正常入园、别在门口哭半小时，晚十分钟到也接受。',
]

// ---- 状态 ----
const health = ref(null)
// 会话 id 存一下 —— 刷新页面后它就没了，
// 而 canSend 要求有 conversation_id，于是发送按钮一直是灰的。
// 调试时刷新页面很常见（看报错、看原始响应），不能因此把会话丢了。
const CONV_KEY = 'rdi.conversation_id'
const conversationId = ref(localStorage.getItem(CONV_KEY) || '')
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
// 追问作答组件的实例 —— 新一轮追问来时要重置它的内部状态
const stepper = ref(null)

// 进度（方案 A：流式推进度，不流半截答案）
//
// 记的是**已经走过的步骤**，不只是当前那一步 —— 家长能看到
// "理解 → 判断 → 检索 → 整理建议"这条线，知道系统在往哪儿走。
const progressSteps = ref([])

// 建议卡引用到的证据 id 集合 —— 右侧证据面板靠它标"已引用"。
//
// **为什么需要它**：卡上的引用和右侧的证据说的是同一段东西，
// 但分属两个组件，谁也不认识谁。把 id 收集上来传下去，
// 右侧才能标出"这 8 条里有 2 条真被用了"。
const citedIds = ref([])

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

// 跑一轮带进度提示的请求。
//
// 走 SSE 端点：每收到一条 progress 就更新提示条。
// 出错时退回非流式请求 —— 流式端点不该是唯一的路，
// 万一代理不支持 SSE，流程还得能走通。
async function runStreaming(fn) {
  busy.value = true
  error.value = ''
  progressSteps.value = []
  try {
    const data = await fn()
    lastRaw.value = data
    return data
  } catch (e) {
    error.value = e.message
    lastRaw.value = { error: e.message }
    return null
  } finally {
    busy.value = false
    // 提示条留一会儿再消失 —— 立刻消失的话家长根本来不及看。
    setTimeout(() => {
      progressSteps.value = []
    }, 1200)
  }
}

function onProgress(ev) {
  // 同一个 stage 可能出现多次（重试）—— 只记一条，但把重试次数带上。
  const existing = progressSteps.value.find((s) => s.stage === ev.stage)
  if (existing) {
    existing.attempt = ev.attempt
    return
  }
  progressSteps.value.push({
    stage: ev.stage,
    label: ev.label,
    attempt: ev.attempt,
  })
}

async function createConversation() {
  const data = await run(() =>
    api.createConversation({ display_label: displayLabel.value, age_band: ageBand.value }),
  )
  if (!data) return
  conversationId.value = data.conversation_id
  localStorage.setItem(CONV_KEY, data.conversation_id)
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

// 收集每张卡引用到的证据 id。
//
// 卡组件用 defineExpose 把 citedIds 露出来 —— 它比父组件自己
// 再算一遍靠谱：那边已经解析过引用和证据的对应关系了。
const planRefs = new Map()

function registerPlan(plan, el) {
  if (!el) {
    planRefs.delete(plan.plan_id)
    return
  }
  planRefs.set(plan.plan_id, el)
  syncCitedIds()
}

function syncCitedIds() {
  const ids = new Set()
  for (const el of planRefs.values()) {
    for (const id of el?.citedIds || []) ids.add(id)
  }
  const next = [...ids]
  // **必须比对再赋值**：每次都赋一个新数组的话，
  // citedIds 变化 → 重渲染 → registerPlan → syncCitedIds → 又赋新数组
  // → 无限递归。Vue 会报 "Maximum recursive updates exceeded"。
  const prev = citedIds.value
  if (prev.length === next.length && next.every((id, i) => prev[i] === id)) return
  citedIds.value = next
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
    // 消息区只留个引子 —— 具体题目由 FollowupStepper 渲染，
    // 不然消息区会有一份、stepper 又有一份，重复得看不清。
    push('bot', `（${questions.value.length} 个问题）`)
    stepper.value?.reset()
  } else if (data.message) {
    push('bot', data.message)
  }
}

async function send(text) {
  const msg = (text ?? input.value).trim()
  if (!msg || !canSend.value) return
  input.value = ''
  push('user', msg)

  const data = await runStreaming(() =>
    streamMessage(conversationId.value, { message: msg }, { onProgress }),
  )
  if (data) applyTurn(data)
}

// 提交一轮追问的回答。
//
// answers 是 [{question, answer}, ...] —— 每条带原问题，
// 后端拼成给图看的文本时不会把多题串味。
async function submitAnswers(answers) {
  if (!answers?.length) return
  const summary = answers
    .map((a) => `${a.question}\n${a.answer}`)
    .join('\n\n')
  push('user', summary)

  const data = await runStreaming(() =>
    streamMessage(
      conversationId.value,
      { message: '', answers },
      { onProgress },
    ),
  )
  if (data) applyTurn(data)
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
  trialId.value = data.trial_id || ''
  selectedPlanId.value = data.selected_plan_id || ''
  // GET 返回的 plans 是数据库里的已落库方案，和本轮的候选不是一回事
  if (data.plans?.length && !plans.value.length) plans.value = data.plans
}

// 刷新页面后把上次那个会话接回来 ——
// 光有 conversation_id 不够，还要把状态、消息、追问题重新填上。
async function restoreSession() {
  if (!conversationId.value) return
  // 这里不用 run()：恢复失败不是"操作出错"，不该在页面上弹红条。
  let data = null
  try {
    data = await api.getConversation(conversationId.value)
  } catch {
    data = null
  }
  if (!data) {
    // 存的 id 后端认不出来（比如换了数据库、或者是从 mock 那边留下的）——
    // 清掉它，不然这个页面会一直卡在"发送按钮是灰的"状态。
    localStorage.removeItem(CONV_KEY)
    conversationId.value = ''
    status.value = ''
    return
  }
  status.value = data.status
  evidence.value = data.evidence || []
  evidenceDecision.value = data.evidence_decision || {}
  trialId.value = data.trial_id || ''
  selectedPlanId.value = data.selected_plan_id || ''
  if (data.plans?.length) plans.value = data.plans
  // 把历史对话显示出来，家长刷新一次还能看到自己说过什么
  for (const it of data.interactions || []) {
    push(it.role === 'user' ? 'user' : 'bot', it.content)
  }
  // 停在追问中断上时，题目要从 pending_interrupt 里取出来重新渲染
  const pending = data.pending_interrupt
  if (pending?.kind === 'followup') {
    questions.value = pending.questions || []
  }
}

onMounted(() => {
  checkHealth()
  restoreSession()
})

// 证据或方案卡变了，"哪些证据被引用了"要重新算一遍。
//
// **为什么要 watch**：卡组件的 citedIds 是从 props 算出来的，
// 而证据面板的序号是按 evidence 数组的位置算的 ——
// 证据晚到一步（先出卡后出证据）的话，卡里的"第 N 条"就指错了。
// 所以两边都变了就得重算。
watch([evidence, plans], () => {
  nextTick(syncCitedIds)
}, { deep: true })
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

          <!-- 进度（方案 A）：推的是"在做什么"，不是半截答案 -->
          <div v-if="busy && progressSteps.length" class="progress">
            <div v-for="(s, i) in progressSteps" :key="s.stage" class="prog-step"
                 :class="{ last: i === progressSteps.length - 1 }">
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

          <!-- 追问：逐题作答，左右切换，最后一次性提交 -->
          <FollowupStepper
            v-if="awaitingFollowup && questions.length"
            ref="stepper"
            :questions="questions"
            :busy="busy"
            @submit="submitAnswers"
          />

          <div v-else class="field">
            <label>描述孩子的情况</label>
            <textarea v-model="input"
                      @keydown.enter.exact.prevent="send()"
                      placeholder="Enter 直接发送" />
          </div>

          <div v-if="!awaitingFollowup" class="row">
            <button class="primary"
                    :disabled="!canSend || !input.trim()"
                    @click="send()">
              {{ busy ? '处理中…' : '发送' }}
            </button>
            <span v-if="busy" class="small muted">图在跑，别重复点</span>
          </div>

          <details v-if="!awaitingFollowup" style="margin-top:10px">
            <summary>快捷填入演示答案（回车逐条提交）</summary>
            <button v-for="(a, i) in DEMO_ANSWERS" :key="i" class="small"
                    style="margin:2px 4px 2px 0"
                    @click="input = a; send()">
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
                    :ref="(el) => registerPlan(p, el)"
                    :evidence="evidence"
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
        <EvidencePanel :evidence="evidence" :decision="evidenceDecision" :gaps="evidenceGaps"
                       :cited-ids="citedIds" />

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