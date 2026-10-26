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
import { api, streamMessage } from './api.js'
import PlanCard from './components/PlanCard.vue'
import FeedbackForm from './components/FeedbackForm.vue'
import FollowupStepper from './components/FollowupStepper.vue'
import MemoryPanel from './components/MemoryPanel.vue'
import MonthPicker from './components/MonthPicker.vue'
import DebugPanel from './components/DebugPanel.vue'

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
// 家庭/孩子 id 也存下来：长期记忆挂在**孩子**上，
// 换会话不换孩子才能看到上一轮记住的东西。
const FAMILY_KEY = 'rdi.family_id'
const CHILD_KEY = 'rdi.child_id'
// 每个孩子记住自己上次用的会话 id —— 切账号回来能接着上次的上下文，
// 而不是从一张白纸重新开始。
const CHILD_CONV_PREFIX = 'rdi.conversation_id.child.'
// 每个 trial 最后一次反馈的 turn_id —— 刷新页面后靠它把反馈结果接回来
const FEEDBACK_TURN_PREFIX = 'rdi.feedback_turn.'
const conversationId = ref(localStorage.getItem(CONV_KEY) || '')
const familyId = ref(localStorage.getItem(FAMILY_KEY) || '')
const childId = ref(localStorage.getItem(CHILD_KEY) || '')

// 测试账号列表（新建的在前）。切换它 = 切换"当前在看哪个孩子的数据"。
const children = ref([])
const loadingChildren = ref(false)
// 新建账号要填的东西：**姓名和出生年月都必填**。
// 年龄不存 —— 只存出生日期，显示和给模型用的年龄都由后端按当天现算。
// 姓名不限长度：这里实际被当"孩子的描述"用。
const newName = ref('')
// 日历控件选出来的年月（YYYY-MM）
const newBirth = ref('')

const messages = ref([])
const status = ref('')
const questions = ref([])
const plans = ref([])
const selectedPlanId = ref('')
const evidence = ref([])
const evidenceDecision = ref({})
// 安全审查的结论（含被逐卡丢弃的卡）—— 前端要能说清"为什么少了一张"
const safetyReview = ref({})
const evidenceGaps = ref([])
const trialId = ref('')
const finalMessage = ref('')
const feedbackResult = ref(null)

// 长期记忆（阶段 3）
const memories = ref([])
const memoryPending = ref(0)

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

// ---- 派生 ----
// 只认后端真的声明过的状态，别猜。
const awaitingFollowup = computed(() => status.value === 'awaiting_followup')
const awaitingPlanSelection = computed(() => status.value === 'awaiting_plan_selection')
const canSend = computed(() => conversationId.value && !busy.value && !awaitingPlanSelection.value)
// 反馈不足时图会停在 feedback_followup 中断上 —— 这时要把表单
// 换成追问，而不是继续显示"记录反馈"。
const feedbackFollowup = computed(
  () => feedbackResult.value?.status === 'awaiting_feedback_followup',
)
const feedbackQuestions = computed(() =>
  feedbackFollowup.value ? feedbackResult.value?.questions || [] : [],
)
const needsFeedback = computed(
  () => Boolean(trialId.value) && !feedbackResult.value,
)
// 新建账号：姓名和出生年月都必须填
const canCreateAccount = computed(
  () => Boolean(newName.value.trim()) && Boolean(newBirth.value) && !busy.value,
)
// 按钮为什么是灰的 —— 直接写出来，别让家长猜。
const missingAccountFields = computed(() => {
  const missing = []
  if (!newName.value.trim()) missing.push('姓名 / 孩子的描述')
  if (!newBirth.value) missing.push('出生年月（点「选择年月」）')
  if (busy.value) missing.push('上一个请求还在处理')
  return missing
})

// 下拉选项。当前孩子如果不在列表里（老会话、换过库），也要能显示出来 ——
// 否则 select 会变成"没有任何选项被选中"，看起来像坏了。
const accountOptions = computed(() => {
  if (childId.value && !children.value.some((c) => c.child_id === childId.value)) {
    return [
      {
        child_id: childId.value,
        family_id: familyId.value,
        display_label: '当前孩子（不在列表中）',
        age_display: '',
        age_band: '',
      },
      ...children.value,
    ]
  }
  return children.value
})

// 下拉里的显示名。
//
// **要截断**：姓名/描述可能是一长串（"四岁男孩，确诊孤独症，合并语言迟缓……"），
// 全塞进下拉选项会把整个下拉撑成一行行长文本。
// 只取第一行、超过 40 字截断 —— 完整内容还在档案里。
function shortLabel(text) {
  const first = String(text || '').split('\n')[0].trim()
  return first.length > 40 ? `${first.slice(0, 40)}…` : first
}

// 日历控件的上限：不允许选到未来
const today = computed(() => {
  const now = new Date()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${m}-${d}`
})

const STATUS_TEXT = {
  awaiting_followup: ['追问中', 'warn'],
  awaiting_plan_selection: ['待选方案', 'accent'],
  awaiting_feedback_followup: ['反馈追问中', 'warn'],
  adjustment: ['证据调整建议', 'accent'],
  limited_answer: ['有限回答', 'warn'],
  // 建议卡生成了，但全被安全审查拦下 —— 和"证据不够"是两回事
  safety_blocked: ['安全复核未通过', 'err'],
  needs_professional_support: ['建议转专业支持', 'err'],
  completed: ['已完成', 'ok'],
  interrupted: ['中断', 'warn'],
}

// 反馈后四选一的说法。给家长看的话，不是枚举名。
const FEEDBACK_DECISION_TEXT = {
  continue: ['继续当前方案', 'ok'],
  adjust: ['根据指南调整这一步', 'accent'],
  followup: ['还需要补充信息', 'warn'],
  stop: ['先停一停', 'err'],
}

const STATUS_HINT = {
  awaiting_followup: '图停在 await_user_answer 中断上。家长答完这一轮才会继续往下走。',
  awaiting_plan_selection: '图停在 present_plans 中断上 —— 必须调 select-plan 才会执行 commit_plan_selection，trial 才落库。',
  limited_answer: '证据不够，给了有限回答，没有方案卡。',
  safety_blocked: '安全审查把所有建议卡拦下了，给了有限回答。',
  needs_professional_support: '门控判成高风险，建议转专业支持。',
  completed: '本轮跑完了。',
}

// 安全审查的说明**要带数量和原因** —— 静态文案说不清
// "少了几张、为什么少"。少一张卡，家长得知道少在哪。
const statusHint = computed(() => {
  const dropped = safetyReview.value?.dropped_plans || []
  if (dropped.length) {
    const reasons = (safetyReview.value.findings || [])
      .filter((f) => dropped.some((id) => String(f).startsWith(`[${id}]`)))
      .map((f) => String(f).replace(/^\[[^\]]+\]\s*/, ''))
    const reason = reasons.length ? ` 原因：${reasons.join('；')}` : ''
    if (status.value === 'safety_blocked') {
      return `安全审查把全部 ${dropped.length} 张建议卡拦下了，给了有限回答。${reason}`
    }
    return `安全审查拦下了 ${dropped.length} 张建议卡，剩下 ${plans.value.length} 张照常给出。${reason}`
  }
  return STATUS_HINT[status.value] || ''
})

// 证据门控的结论。右侧的证据清单取消之后，这个状态挪到方案卡头上 ——
// 家长还是要知道"这次是有依据的，还是没找到依据"。
const EVIDENCE_STATUS_TEXT = {
  direct: ['有直接依据', 'ok'],
  indirect: ['只有间接参考', 'warn'],
  conflicting: ['证据互相矛盾', 'warn'],
  none: ['没找到足够依据', 'warn'],
  risk: ['需要专业支持', 'err'],
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

// ---- 测试账号（一键新建 + 下拉切换）----
//
// 一个账号 = 一个新家庭 + 一个新孩子。长期记忆挂在孩子上，
// 所以不同账号之间天然隔离，适合并排对比几种情况。

async function loadChildren() {
  loadingChildren.value = true
  try {
    // **不过滤前缀**：名字是家长自己写的（可能是一长串描述），
    // 按"测试账号"前缀过滤会把它们全藏掉。
    // 老演示数据里那些叫"孩子"的也会出现，但新账号排在最前面
    // （后端按创建时间倒序），当前正在用的孩子由 accountOptions 兜底。
    const data = await api.listChildren()
    children.value = data.children || []
  } catch {
    // 账号列表拉不到不该挡住主流程 —— 当前孩子照样能对话
    children.value = []
  } finally {
    loadingChildren.value = false
  }
}

// 新建账号，并直接切过去开一个新会话。
//
// **姓名和出生年月都必须填**：
//   - 姓名不填 → 自动命名出来的账号在列表里长得一模一样，分不清谁是谁
//   - 出生年月不填 → 年龄只能靠 age_band 兜底，算出来的段位经常是错的
//
// 姓名**不限制长度**：这个字段实际被当"孩子的描述"用。
async function createAccount() {
  if (!canCreateAccount.value) return
  const data = await run(() =>
    api.createChild({
      display_label: newName.value.trim(),
      // 日历选的就是 'YYYY-MM'，后端按当月 1 号存
      // （见 rdi_agent/age.py::parse_birth_date）
      birth_date: newBirth.value,
    }),
  )
  if (!data) return
  newName.value = ''
  newBirth.value = ''
  await loadChildren()
  childId.value = data.child_id
  familyId.value = data.family_id
  localStorage.setItem(FAMILY_KEY, data.family_id)
  localStorage.setItem(CHILD_KEY, data.child_id)
  await createConversation()
}

async function switchChild(nextChildId) {
  if (!nextChildId || nextChildId === childId.value) return
  const target = accountOptions.value.find((c) => c.child_id === nextChildId)
  if (!target) return

  childId.value = target.child_id
  familyId.value = target.family_id
  localStorage.setItem(FAMILY_KEY, target.family_id)
  localStorage.setItem(CHILD_KEY, target.child_id)

  // 先把上一个孩子的界面清掉，免得网络慢时看起来像两套数据串在一起
  resetTurnState()
  conversationId.value = ''
  localStorage.removeItem(CONV_KEY)

  // 这个孩子上次的会话还在就接回来；接不回来（换过库/被清了）就开新的
  const saved = localStorage.getItem(CHILD_CONV_PREFIX + nextChildId)
  if (saved && (await hydrateConversation(saved))) return
  if (saved) localStorage.removeItem(CHILD_CONV_PREFIX + nextChildId)
  await createConversation()
}

// 把一个已有会话接回界面。返回 false 表示后端不认这个 id。
async function hydrateConversation(targetId) {
  let data = null
  try {
    data = await api.getConversation(targetId)
  } catch {
    return false
  }
  if (!data) return false

  resetTurnState()
  conversationId.value = targetId
  localStorage.setItem(CONV_KEY, targetId)
  status.value = data.status
  familyId.value = data.family_id || familyId.value
  childId.value = data.child_id || childId.value
  if (familyId.value) localStorage.setItem(FAMILY_KEY, familyId.value)
  if (childId.value) {
    localStorage.setItem(CHILD_KEY, childId.value)
    localStorage.setItem(CHILD_CONV_PREFIX + childId.value, targetId)
  }
  evidence.value = data.evidence || []
  evidenceDecision.value = data.evidence_decision || {}
  trialId.value = data.trial_id || ''
  selectedPlanId.value = data.selected_plan_id || ''
  if (data.plans?.length) plans.value = data.plans
  memories.value = data.memories || []
  memoryPending.value = data.memory_pending || 0
  for (const it of data.interactions || []) {
    push(it.role === 'user' ? 'user' : 'bot', it.content)
  }
  // 停在追问中断上时，题目要从 pending_interrupt 里取出来重新渲染
  const pending = data.pending_interrupt
  if (pending?.kind === 'followup') {
    questions.value = pending.questions || []
  }
  await restoreFeedback()
  return true
}

// 清掉"这一轮/这个会话"的所有展示状态。切账号和开新会话都要用，
// 漏一个字段就会出现"上一个孩子的方案卡还挂在下面"这种事。
function resetTurnState() {
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
  safetyReview.value = {}
  feedbackResult.value = null
  memories.value = []
  memoryPending.value = 0
  progressSteps.value = []
  lastRaw.value = null
  input.value = ''
  stepper.value?.reset?.()
}

// 开新会话：**永远复用当前孩子** —— 长期记忆挂在孩子上，
// 换会话不换孩子才能看到上一轮记住的东西。
async function createConversation() {
  if (!childId.value) return
  const data = await run(() =>
    api.createConversation({
      family_id: familyId.value || undefined,
      child_id: childId.value,
    }),
  )
  if (!data) return
  conversationId.value = data.conversation_id
  familyId.value = data.family_id
  childId.value = data.child_id
  localStorage.setItem(CONV_KEY, data.conversation_id)
  localStorage.setItem(FAMILY_KEY, data.family_id)
  localStorage.setItem(CHILD_KEY, data.child_id)
  localStorage.setItem(CHILD_CONV_PREFIX + data.child_id, data.conversation_id)
  resetTurnState()
  await refreshMemories()
}

// ---- 长期记忆 ----
function memoryActionPayload() {
  return {
    family_id: familyId.value,
    conversation_id: conversationId.value || null,
    turn_id: null,
  }
}

async function refreshMemories() {
  if (!childId.value || !familyId.value) {
    memories.value = []
    memoryPending.value = 0
    return
  }
  try {
    const data = await api.listMemories(childId.value, familyId.value)
    memories.value = data.memories || []
    memoryPending.value = data.pending_confirmation || 0
  } catch {
    // 记忆面板读不出来不该影响主流程 —— 家长的问题是主线。
    memories.value = []
    memoryPending.value = 0
  }
}

async function onMemoryConfirm(item) {
  const data = await run(() =>
    api.confirmMemory(childId.value, item.memory_id, memoryActionPayload()),
  )
  if (data) await refreshMemories()
}

async function onMemoryCorrect({ memory, content }) {
  const data = await run(() =>
    api.correctMemory(childId.value, memory.memory_id, {
      ...memoryActionPayload(),
      content,
    }),
  )
  if (data) await refreshMemories()
}

async function onMemoryDelete(item) {
  const data = await run(() =>
    api.deleteMemory(childId.value, item.memory_id, memoryActionPayload()),
  )
  if (data) await refreshMemories()
}

// 建议卡的"依据"由卡片自己渲染（片段带编号，点编号展开），
// 右侧不再单独列一份证据清单 —— 同一件事显示两遍，家长会不知道该看哪边。

function applyTurn(data) {
  status.value = data.status
  questions.value = data.questions || []
  evidence.value = data.evidence || []
  evidenceDecision.value = data.evidence_decision || {}
  evidenceGaps.value = data.evidence_gaps || []
  safetyReview.value = data.safety_review || {}
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

  // 这一轮可能新记/纠正/删了记忆，面板跟着刷一次
  refreshMemories()
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

function applyFeedbackResult(result) {
  feedbackResult.value = result
  // 记住 turn_id：刷新页面后 GET /feedback/{turn_id} 能拿回结果，
  // 也不会让家长以为"上次的反馈没提交"。
  if (trialId.value && result.turn_id) {
    localStorage.setItem(FEEDBACK_TURN_PREFIX + trialId.value, result.turn_id)
  }
  finalMessage.value = result.message || finalMessage.value
  if (result.status === 'awaiting_feedback_followup') {
    // 题目由 FollowupStepper 渲染，消息区只留一句引子。
    push('bot', `（反馈还缺 ${result.questions?.length || 0} 个信息）`)
  } else if (result.message) {
    push('bot', result.message)
  }
  refreshMemories()
}

async function submitFeedback(result) {
  applyFeedbackResult(result)
}

// 刷新/切回会话时，把已经提交过的反馈结果接回来。
// 接不回来（清过库、turn_id 不认）就当没提交过，表单照常出现。
async function restoreFeedback() {
  if (!trialId.value || feedbackResult.value) return
  const turn = localStorage.getItem(FEEDBACK_TURN_PREFIX + trialId.value)
  if (!turn) return
  try {
    feedbackResult.value = await api.getFeedback(trialId.value, turn)
  } catch {
    localStorage.removeItem(FEEDBACK_TURN_PREFIX + trialId.value)
  }
}

// 回答反馈追问。turn_id 沿用第一次提交时的 —— 后端用它找到
// 停在 interrupt 上的那条 checkpoint 线程。
async function submitFeedbackAnswers(answers) {
  if (!answers?.length || !feedbackResult.value?.turn_id) return
  const summary = answers.map((a) => `${a.question}\n${a.answer}`).join('\n\n')
  push('user', summary)
  const data = await run(() =>
    api.resumeFeedback(trialId.value, {
      turn_id: feedbackResult.value.turn_id,
      answers,
    }),
  )
  if (data) applyFeedbackResult(data)
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
  memories.value = data.memories || []
  memoryPending.value = data.memory_pending || 0
  await restoreFeedback()
}

// 刷新页面后把上次那个会话接回来。
// 不弹红条：恢复失败不是"操作出错"，只是没有可恢复的会话。
async function restoreSession() {
  if (!conversationId.value) return
  const stored = conversationId.value
  if (await hydrateConversation(stored)) return
  // 存的 id 后端认不出来（清过库、换了数据库）——
  // 连同 family/child 一起清掉，不然下拉里会挂着一个已经不存在的
  // "当前孩子（不在列表中）"，点什么都报 404。
  localStorage.removeItem(CONV_KEY)
  localStorage.removeItem(FAMILY_KEY)
  localStorage.removeItem(CHILD_KEY)
  conversationId.value = ''
  familyId.value = ''
  childId.value = ''
  status.value = ''
  memories.value = []
  memoryPending.value = 0
}

onMounted(async () => {
  checkHealth()
  await loadChildren()
  await restoreSession()
  // 没有可恢复的会话，但记住了孩子 —— 记忆面板也该显示这个孩子的数据
  if (!conversationId.value && childId.value && familyId.value) {
    await refreshMemories()
  }
})
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
    <div v-if="status && statusHint" class="notice">{{ statusHint }}</div>

    <div class="cols">
      <!-- 左：对话与方案 -->
      <div>
        <div class="card">
          <header>
            <h3>会话</h3>
            <span v-if="conversationId" class="badge small">{{ conversationId }}</span>
          </header>

          <div class="row" style="margin-bottom:6px">
            <div class="field" style="flex:2;margin:0">
              <label>测试账号（每个账号一套独立记忆）</label>
              <select
                :value="childId"
                :disabled="busy || loadingChildren"
                @change="switchChild($event.target.value)"
              >
                <option value="" disabled>
                  {{ loadingChildren ? '加载中…' : '选择账号…' }}
                </option>
                <option v-for="c in accountOptions" :key="c.child_id" :value="c.child_id">
                  {{ shortLabel(c.display_label) }}{{ c.age_display ? ` · ${c.age_display}` : '' }}
                </option>
              </select>
            </div>
            <button
              style="align-self:flex-end"
              :disabled="busy || !childId"
              :title="childId ? '同一个孩子开新会话，长期记忆会带过来' : '先新建或选择一个账号'"
              @click="createConversation"
            >
              新会话
            </button>
          </div>

          <div class="row" style="margin-bottom:10px;align-items:flex-end">
            <div class="field" style="flex:1;margin:0">
              <label>姓名 / 孩子的描述</label>
              <!--
                用 textarea 而不是 input：这个字段实际是当"孩子的描述"用的，
                有人会写一长串背景情况（诊断、语言状况、平时怎么沟通）。
                **不限制长度** —— 后端这一列是 TEXT。
              -->
              <textarea
                v-model="newName"
                rows="2"
                placeholder="写名字，或者一句话描述这个孩子的情况（不限制长度）"
              ></textarea>
            </div>
            <div class="field" style="flex:0 0 170px;margin:0">
              <label>出生年月</label>
              <!--
                自己画的年月日历：点开是年份 + 12 个月的格子。
                不用原生 input[type=date] —— 出生日期只精确到月，
                让家长去挑一个"具体哪天"是假精度。
              -->
              <MonthPicker v-model="newBirth" :max="today" placeholder="选择年月" />
            </div>
            <button
              style="align-self:flex-end"
              :disabled="!canCreateAccount"
              :title="canCreateAccount ? '' : '姓名和出生年月都要填'"
              @click="createAccount"
            >
              ＋ 新建账号
            </button>
          </div>
          <div v-if="!canCreateAccount" class="small muted" style="margin:-4px 0 10px">
            还差：{{ missingAccountFields.join('、') }}。
          </div>

          <div class="msgs">
            <div v-if="!messages.length" class="small muted">
              还没开始。先「新建账号」或从下拉选一个，再在下面说孩子的表现。
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
            <span
              v-if="evidenceDecision.evidence_status"
              class="badge"
              :class="EVIDENCE_STATUS_TEXT[evidenceDecision.evidence_status]?.[1] || ''"
            >
              {{ EVIDENCE_STATUS_TEXT[evidenceDecision.evidence_status]?.[0] || evidenceDecision.evidence_status }}
            </span>
          </header>
          <PlanCard v-for="p in plans" :key="p.plan_id" :plan="p"
                    :evidence="evidence"
                    :selectable="awaitingPlanSelection"
                    :selected="p.plan_id === selectedPlanId"
                    @select="selectPlan" />
          <!-- 证据缺口：家长该知道"哪一部分我们没找到依据" -->
          <div v-if="evidenceGaps.length" class="small muted" style="margin-top:8px">
            这次没找到依据的地方：{{ evidenceGaps.join('；') }}
          </div>
        </div>

        <div v-if="awaitingPlanSelection" class="notice">
          还没选方案 —— 不选的话图不会往下走，trial 也不会落库。
        </div>

        <div class="card" v-if="finalMessage && !awaitingFollowup && !awaitingPlanSelection">
          <header><h3>教练回复</h3></header>
          <div style="white-space:pre-wrap">{{ finalMessage }}</div>
        </div>

        <FeedbackForm v-if="needsFeedback" :trial-id="trialId" @done="submitFeedback" />

        <!-- 反馈不足：图停在 feedback_followup 中断上 -->
        <FollowupStepper
          v-if="feedbackFollowup"
          :questions="feedbackQuestions"
          :busy="busy"
          @submit="submitFeedbackAnswers"
        />

        <div class="card" v-if="feedbackResult && !feedbackFollowup">
          <header>
            <h3>反馈结果</h3>
            <span
              v-if="feedbackResult.next_step?.decision"
              class="badge"
              :class="FEEDBACK_DECISION_TEXT[feedbackResult.next_step.decision]?.[1] || ''"
            >
              {{ FEEDBACK_DECISION_TEXT[feedbackResult.next_step.decision]?.[0] || feedbackResult.next_step.decision }}
            </span>
            <span class="badge" :class="STATUS_TEXT[feedbackResult.status]?.[1] || ''">
              {{ STATUS_TEXT[feedbackResult.status]?.[0] || feedbackResult.status }}
            </span>
          </header>
          <div class="small muted" v-if="feedbackResult.next_step?.decision_reason">
            {{ feedbackResult.next_step.decision_reason }}
          </div>
          <dl class="kv">
            <dt>实际执行</dt><dd>{{ feedbackResult.analysis?.actual_execution || '—' }}</dd>
            <dt>观察到的变化</dt>
            <dd>
              <div v-for="(c, i) in feedbackResult.analysis?.observed_changes || []" :key="i">{{ c }}</div>
              <span v-if="!feedbackResult.analysis?.observed_changes?.length">—</span>
            </dd>
            <dt>执行障碍</dt>
            <dd>
              <div v-for="(c, i) in feedbackResult.analysis?.execution_barriers || []" :key="i">{{ c }}</div>
              <span v-if="!feedbackResult.analysis?.execution_barriers?.length">—</span>
            </dd>
            <dt>与方案的关系</dt><dd>{{ feedbackResult.analysis?.relation_to_plan || '—' }}</dd>
            <dt>置信说明</dt><dd>{{ feedbackResult.analysis?.confidence_note || '—' }}</dd>
            <dt>下一次</dt>
            <dd>
              <div v-for="(s, i) in feedbackResult.next_step?.next_steps || []" :key="i">{{ s }}</div>
              <span v-if="!feedbackResult.next_step?.next_steps?.length">—</span>
            </dd>
          </dl>

          <!-- 历史对比：这次和过去几次像在哪、差在哪 -->
          <div v-if="feedbackResult.analysis?.history_comparison?.length" class="field">
            <label>和过去的尝试比</label>
            <div
              v-for="(c, i) in feedbackResult.analysis.history_comparison"
              :key="i"
              class="small"
              style="margin-bottom:6px"
            >
              <strong>{{ c.previous_plan_title || c.previous_trial_id }}</strong>
              <span class="muted">（{{ c.previous_trial_status }}）</span>
              <div v-if="c.similarity">相同：{{ c.similarity }}</div>
              <div v-if="c.difference">不同：{{ c.difference }}</div>
            </div>
          </div>

          <!-- 证据支撑的调整建议（adjust 分支） -->
          <div v-if="feedbackResult.feedback_adjustment" class="field">
            <label>根据指南的调整</label>
            <div style="white-space:pre-wrap">{{ feedbackResult.feedback_adjustment.headline }}</div>
            <div class="small" v-if="feedbackResult.feedback_adjustment.what_changed">
              {{ feedbackResult.feedback_adjustment.what_changed }}
            </div>
            <div class="small" v-for="(a, i) in feedbackResult.feedback_adjustment.adjust_actions || []" :key="'a'+i">
              · {{ a }}
            </div>
            <div class="small" v-for="(a, i) in feedbackResult.feedback_adjustment.observe || []" :key="'o'+i">
              留意：{{ a }}
            </div>
            <div class="small" v-for="(a, i) in feedbackResult.feedback_adjustment.stop_if || []" :key="'s'+i">
              停止：{{ a }}
            </div>
            <div class="small muted" v-if="feedbackResult.feedback_adjustment.evidence_gap_note">
              {{ feedbackResult.feedback_adjustment.evidence_gap_note }}
            </div>
          </div>

          <div v-if="feedbackResult.evidence?.length" class="field">
            <label>这次的依据（{{ feedbackResult.evidence.length }} 条）</label>
            <div v-for="(e, i) in feedbackResult.evidence" :key="i" class="small">
              [{{ i + 1 }}] 《{{ e.section_title || e.title }}》
              <span v-if="e.pdf_page_start">PDF 第 {{ e.pdf_page_start }}-{{ e.pdf_page_end }} 页</span>
              ：{{ e.summary }}
            </div>
          </div>
        </div>
      </div>

      <!-- 右：记忆、证据与原始响应 -->
      <div>
        <MemoryPanel
          :memories="memories"
          :pending="memoryPending"
          :busy="busy"
          @confirm="onMemoryConfirm"
          @correct="onMemoryCorrect"
          @delete="onMemoryDelete"
        />

        <DebugPanel :conversation-id="conversationId" />

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