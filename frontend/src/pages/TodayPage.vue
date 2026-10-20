<script setup>
// 今天：一天的几条活动，逐条做、逐条填。
//
// ★ 顺序不可颠倒：先出卡片，再收反馈。反过来的话会出现荒谬的一幕 ——
//   卡片刚建议 gro-007，同一天的反馈立刻把它冷却掉，"今天的建议"当场失效。
//   用户要的是「计划执行 → 反馈 → 更新计划」，反馈影响的是**明天**。
//
// ★ 一次生成一整天，而不是"要哪条出哪条"：
//   家长在建档时填的是"每天 30 分钟"，而一次只给一条活动的话他拿到的永远是 6-10 分钟，
//   剩下的时间就空着 —— 界面上写的可用时间和实际给到的东西对不上。
//   所以这里显示当天装箱满的几条，逐条填反馈。
//
// ★ 只生成一次。`/api/card` 命中后端缓存就秒回；「重新生成」才传 force。
//   前端不做"我生成过了就别问了"这种缓存 —— 缓存键必须包含"画像有没有变"，
//   而前端不知道画像什么时候被重新建档过。
import { ref, computed, watch, onMounted, toRef } from 'vue'
import { api } from '../api.js'
import { runJob } from '../api.js'
import JobProgress from '../components/JobProgress.vue'
import AgentTrace from '../components/AgentTrace.vue'
import FeedbackItem from '../components/FeedbackItem.vue'
import OtherNotes from '../components/OtherNotes.vue'
import RedFlagBanner from '../components/RedFlagBanner.vue'
import PlanReview from '../components/PlanReview.vue'
import { useDraft } from '../composables/useDraft.js'

const props = defineProps({ CHILD: String, date: String, hasProfile: Boolean,
                            plan: Object, setPlan: Function })
const emit = defineEmits(['refresh-state'])

const steps = ref([])
const running = ref(false)
const err = ref('')

async function load(force = false) {
  running.value = true; steps.value = []; err.value = ''
  try {
    // 空结果也要清掉上一次那份，否则家长在换日期之后还会看到旧的那天的安排
    const r = await runJob('/api/card',
      { child_id: props.CHILD, date: props.date, force },
      (d) => steps.value.push(d))
    props.setPlan(r.result)
  } catch (e) { err.value = e.message } finally { running.value = false }
}

// ★ 进页面就取一次，但**不带 force** —— 后端命中缓存就是 5ms 直接读，
//   没生成过才真的跑一遍链路（30-45s，有进度条）。
//   所以"自动加载"和"只生成一次"不冲突：自动的是**读取**，不是**生成**。
//   早先每次进页面都 force=true，才是那 30-45s 的来源。
onMounted(() => { if (props.hasProfile) load() })

// 换日期 = 换一天的安排。watch 而不是在 load 里判断，
// 因为日期框是父组件的 v-model，改动从外面进来。
watch(() => props.date, () => {
  if (props.hasProfile) load()
})

// 今天发了几条活动、家长填了哪些。**只存"家长点了什么"**，
// quality 由后端 quality_for() 算，前端绝不自己算 —— 前端算一遍
// 就出现第二套口径，而状态机的闸门拿平均 quality 比 0.5 / 0.75。
//
// ★ 已有反馈行**原样保留**：重新生成会把活动列表换掉，
// 而家长已经点过的结果不该因为刷新一次就没了。
const feedbacks = ref([])
const otherNotes = ref('')
const submitting = ref(false)
const fbResult = ref(null)
const fbErr = ref('')

// 草稿：刷新不丢。存服务端 checkpoint（不是 localStorage）
const draft = useDraft(props.CHILD, toRef(props, 'date'), feedbacks, otherNotes)

const activities = computed(() => props.plan?.activities || [])
const pickedIds = computed(() => feedbacks.value.map(f => f.activity_id))

// 当天安排里**没有**被家长填过的活动 —— 那些是要真去做的。
// 刻意不自动补齐所有行：没填的行提交时会被过滤掉（"没有信息"不是观察结果），
// 自动补齐只是让页面多出几个空框，家长会以为每个都得填。
function syncRows() {
  for (const a of activities.value) {
    const id = a.activity.activity_id
    if (pickedIds.value.includes(id)) continue
    feedbacks.value.push({
      activity_id: id, outcome: '',   // 空 = 还没填。没有记录 ≠ 有一条 skipped
      prompt_level: 0, steps_done: 0, note: '',
    })
  }
}
watch(() => props.plan, syncRows, { immediate: true, deep: true })

function removeRow(i) { feedbacks.value.splice(i, 1) }

// 按 activity_id 找反馈行 —— 不按下标。
//
// ★ 为什么按下标是错的：家长点「不记这条」会把一行 splice 掉，
//   后面所有行的下标都前移一位，于是下一次编辑改到的是**隔壁那条活动**。
//   界面上看不出任何异常（都是四个按钮 + 一个输入框），
//   而 sessions 表里 activity_id 记的是隔壁那条 —— 家长点 cog-001 的"不配合"
//   实际记到了 gro-007 头上。这是阶段 5 那个判据 bug 的同源错误：
//   **按位置绑定，而不是按身份绑定。**
function idxOf(activityId) { return feedbacks.value.findIndex(f => f.activity_id === activityId) }
function rowOf(activityId) {
  const i = idxOf(activityId)
  return i < 0 ? { activity_id: activityId, outcome: '', prompt_level: 0, steps_done: 0, note: '' }
               : feedbacks.value[i]
}
function setRow(activityId, patch) {
  const i = idxOf(activityId)
  if (i < 0) return
  feedbacks.value[i] = { ...feedbacks.value[i], ...patch }
}

// ★ canSubmit 必须看 otherNotes —— 原来的写法是
//   `feedbacks.some(f => f.outcome) && !submitting`，**完全不看**家长写的
//   自由文本，于是「只写观察、没做任何活动」这一天按钮是灰的，
//   而技术方案 §1.1 的第一步恰恰就是「家长用自然语言记录」。
//   那不是"少一个入口"，是整个自然语言入口在界面上不存在。
const canSubmit = computed(() =>
  (feedbacks.value.some(f => f.outcome) || !!otherNotes.value.trim())
  && !submitting.value && !obsRunning.value)
const pending = computed(() => feedbacks.value.filter(f => !f.outcome).length)

// ================================================================ 观察链（Phase 1）
// 「其他观察」不再随 /api/feedbacks 一起发，而是先走图 ——
// 因为图要能在信息不足时**停下来问家长**，而 /api/feedbacks 是一条直筒的
// 收→写链路，结构上问不了。
const obsRunning = ref(false)
const obsSteps = ref([])
const obsResult = ref(null)     // 图跑完的终态 {phase, interrupt, 四层, ...}
const obsErr = ref('')
const clarifyAnswer = ref('')

// ★ 刷新后如果还有个问题没答，追问要**自己回来**。
//   前端不持有任何 interrupt 状态（刷新就没了，服务端不会），
//   所以进页面 / 换日期时要问一次服务端「这一天停在哪」。
//   没有这一步，家长点了提交、页面一刷新，追问就永远消失了 ——
//   而那张 thread 还停在 interrupt 上，只能靠人工去捞。
async function loadObserveStatus() {
  try {
    const st = await api.observeStatus(props.CHILD, props.date)
    obsResult.value = (st.phase === 'idle') ? null : st
  } catch (e) {
    console.warn('[observe] 读状态失败', e.message)
  }
}


// ================================================================ 计划审批（Phase 3）
// ★ 为什么单独一块、而不是塞进"今天的安排"里：
//   "今天的安排"是**已经生效的东西**（卡片发出去就收不回来了），
//   而审批是**还没生效的东西**（等你点头）。混在一起的话，
//   家长会分不清"这些已经排好了"和"这些还在等你确认"。
//
// ★ 刷新后靠 /api/plan/status 找回待批的计划 —— 前端不持有 interrupt 状态
//   （刷新就没了，服务端不会）。
const planState = ref(null)
const planBusy = ref(false)

async function loadPlanStatus() {
  try {
    planState.value = await api.planStatus(props.CHILD, props.date)
  } catch (e) {
    console.warn('[plan] 读状态失败', e.message)
  }
}

// 批完之后：计划变了 → 当天的卡片要跟着重出
async function onPlanReviewed(r) {
  if (r) planState.value = r
  else await loadPlanStatus()
  await load()          // 重新读当天安排（后端会按已生效的计划给）
  await loadPlanStatus()
}

onMounted(() => { draft.restore(); loadObserveStatus(); loadRedFlags(); loadPlanStatus() })
watch(() => props.date, () => {
  draft.restore(); loadObserveStatus(); loadPlanStatus()
})  // 换日期 = 换那天的草稿与追问

// ================================================================ 红旗（Phase 2）
// ★ 与观察链的追问不同：红旗**不是那一天的**，是"目前还没处理的事"。
//   所以它不跟 props.date 走 —— 10-20 的自伤必须让 10-21 也看得见，
//   否则「今天换个日期就没那回事了」，而那正是它最不该发生的时候。
const redFlags = ref([])
const paused = ref(false)
const rfBusy = ref(0)

async function loadRedFlags() {
  try {
    const r = await api.redFlags(props.CHILD)
    redFlags.value = r.flags || []
    paused.value = !!r.paused      // ★ 后端给的，前端不推
  } catch (e) {
    console.warn('[redflags] 读失败', e.message)
  }
}

// 改完状态**重读**，不在前端推算 paused —— 状态一变，"暂停"可能就解除了，
// 而那份判断的依据在服务端（Phase 3 生成计划读的是同一条查询）。
async function setRedFlagStatus({ id, status }) {
  rfBusy.value = id
  try {
    await api.redFlagSetStatus(props.CHILD, id, status)
    await loadRedFlags()
  } catch (e) { console.warn('[redflags] 改状态失败', e.message) } finally { rfBusy.value = 0 }
}

async function submitObservation() {
  obsRunning.value = true; obsErr.value = ''; obsResult.value = null
  try {
    const r = await runJob('/api/observe/run',
      { child_id: props.CHILD, date: props.date, text: otherNotes.value },
      (d) => obsSteps.value.push(d))
    obsResult.value = r.result
    // phase==='clarify' 是**正常结果**：图停在 ⚡ interrupt 上等家长回答。
    // 这时不要往下走 —— 后面的 /api/feedbacks 会触发自动日结，
    // 而日结读的是已落库的行，在澄清完成前收尾会把这段漏掉（顺序不变量）。
    if (r.result?.phase === 'clarify') return
    await afterObservation()
  } catch (e) { obsErr.value = e.message } finally { obsRunning.value = false }
}

async function answerClarify() {
  obsRunning.value = true; obsErr.value = ''
  try {
    const r = await runJob('/api/observe/resume',
      { child_id: props.CHILD, date: props.date,
        answer: clarifyAnswer.value, skip: !clarifyAnswer.value.trim() },
      (d) => obsSteps.value.push(d))
    obsResult.value = r.result
    clarifyAnswer.value = ''
    if (r.result?.phase === 'clarify') return   // 一天只问一次，不该走到这
    await afterObservation()
  } catch (e) { obsErr.value = e.message } finally { obsRunning.value = false }
}

// 观察链收尾之后才提交逐条反馈 —— 见上面的顺序不变量
async function afterObservation() {
  await submitFeedback()
  // ★ 这次提交可能新落了一条 urgent 红旗，横幅要**立刻**出现，
  //   而不是等家长刷新页面。理由同上：那条红旗此刻就该被看见。
  await loadRedFlags()
  // 草稿是「还没提交的东西」，提交完就不该再留着
  if (!fbErr.value) await draft.clear()
}

async function submitFeedback() {
  submitting.value = true; fbErr.value = ''
  try {
    const filled = feedbacks.value
      .filter(f => f.outcome)      // ★ 没点的不发 —— "没有信息"不是一种观察结果
      .map(f => ({
        activity_id: f.activity_id, outcome: f.outcome,
        prompt_level: f.prompt_level, steps_done: f.steps_done, note: f.note,
      }))
    // ★ 不再发 other_notes：它已经由上面的观察链落库了（Phase 1）。
    //   两处都发 = 同一段话抽两次、写两行、付两次 LLM。
    fbResult.value = await api.feedbacks({
      child_id: props.CHILD, date: props.date, feedbacks: filled,
    })
    emit('refresh-state')
  } catch (e) { fbErr.value = e.message } finally { submitting.value = false }
}

defineExpose({ load })
</script>

<template>
  <div>
    <!-- 红旗横幅：**置顶**，在"今天的安排"之前。
         不是排版偏好 —— 暂停是整天的事；如果它排在反馈区，
         家长会先读完"今天练这三条"再看到"今天要停"。 -->
    <RedFlagBanner :flags="redFlags" :paused="paused" :busy="rfBusy"
                   @set-status="setRedFlagStatus" />

    <!-- ⚡ 计划审批（Phase 3）：排在横幅之后、"今天的安排"之前 ——
         暂停比审批更要紧（今天要不要停），审批比安排更要紧（还没生效）。 -->
    <PlanReview :CHILD="props.CHILD" :date="props.date" :plan="planState"
                @reviewed="onPlanReviewed" />

    <section class="card">
      <div class="head">
        <h2>今天的安排</h2>
        <span v-if="plan" class="sub">
          {{ activities.length }} 条 · 共 {{ plan.total_minutes }} 分钟
          <template v-if="plan.budget_minutes">
            / 预算 {{ plan.budget_minutes }} 分钟</template>
          <span v-if="plan.cached" class="dim">（已生成过，直接读）</span>
        </span>
        <button class="mini" :disabled="running" @click="load(true)">
          {{ running ? '生成中…' : plan ? '重新生成' : '生成今天的安排' }}
        </button>
      </div>
      <p class="hint">
        按你填的「每天可用时间」装箱（Python 贪心，零 token）。
        一天不重复练同一个技能细项 —— 一天三条大运动，孩子会腻。
        <b>生成一次就好</b>，之后进这一页直接读；换了日期或重新建档会自动重排。
      </p>
      <JobProgress :steps="steps" :done="!running && steps.length > 0" />
      <p v-if="err" class="err">{{ err }}</p>
      <p v-if="plan?.error" class="err">{{ plan.error }}</p>
    </section>

    <!-- 诊断：一天一次，不属于某一条活动，所以放在所有卡片之前 -->
    <section v-if="plan?.analysis?.signals?.length" class="card diag">
      <h3>教练诊断（推理链可校验）<span class="dim">— 读的是今天之前的观察</span></h3>
      <div v-for="(s, i) in plan.analysis.signals" :key="i" class="signal" :class="{ void: s.kind === 'none' }">
        <span class="tag">{{ s.kind }}</span>{{ s.interpretation }}
        <span class="imp">→ {{ s.impact }}</span>
        <span class="fn">功能 {{ s.function }}</span>
        <div class="quote">引文：「{{ s.evidence_quote }}」</div>
      </div>
      <p v-if="plan.unverified_quotes?.length" class="warn">
        ★ {{ plan.unverified_quotes.length }} 条引文未通过原文核对，已作废（仍显示，
        让人看得见模型"想说什么"，而不是假装它没说过）
      </p>
      <p v-if="plan.adjustment" class="adjust">★ 调整：{{ plan.adjustment }}</p>
      <AgentTrace :traces="plan.traces" />
    </section>

    <!-- 一条活动 = 一张卡片 + 它自己的一行反馈 -->
    <section v-for="(a, i) in activities" :key="a.activity.activity_id" class="card act">
      <div class="head">
        <h3><span class="id">{{ a.activity.activity_id }}</span> {{ a.activity.title }}</h3>
        <span class="sub">{{ a.activity.duration_min }} 分钟 · 难度{{ a.activity.difficulty }}</span>
        <span class="sub" :class="{ llm: a.from_llm }">
          {{ a.from_llm ? '卡片含今天的诊断与退路' : '卡片来自活动定义（完整，只是个性化程度低）' }}
        </span>
      </div>

      <p class="why"><b>为什么推它：</b>{{ a.why }}</p>
      <p class="why"><b>为什么今天给他做这个：</b>{{ a.card.why_today }}</p>

      <ol class="steps">
        <li v-for="s in a.card.steps" :key="s.step_id">
          <b>{{ s.step_id }}.</b> {{ s.do }} → <i>{{ s.say ? `说“${s.say}”` : '不说话' }}</i>，等 {{ s.wait_sec }} 秒
          <div class="ok">做到了：{{ s.if_ok }}</div>
          <div class="no">没做到：{{ s.if_not }}</div>
        </li>
      </ol>

      <div v-if="a.card.prompt_ladder?.length" class="ladder">
        <b>提示阶梯</b>（从轻到重 —— 下面那行反馈里点"用到第几级"）
        <ol><li v-for="(p, j) in a.card.prompt_ladder" :key="j">{{ p }}</li></ol>
      </div>

      <div class="refuse">
        <b>★ 如果他不配合：</b>
        <div>先认：{{ a.card.if_child_refuses.recognize }}</div>
        <div>降要求：{{ a.card.if_child_refuses.reduce_demand }}</div>
        <div>给他选：{{ (a.card.if_child_refuses.choices || []).join(' / ') }}</div>
        <div class="abandon">什么时候放弃：{{ a.card.if_child_refuses.abandon_signal }}</div>
      </div>

      <div class="check"><b>怎么算成功：</b>{{ (a.card.success_check || []).join('、') }}</div>

      <FeedbackItem :activity="a.activity" :n-steps="a.n_steps"
                    :form-text="a.feedback_form"
                    :value="rowOf(a.activity.activity_id)"
                    @update="setRow(a.activity.activity_id, $event)" />
      <button class="rm" @click="removeRow(idxOf(a.activity.activity_id))">不记这条</button>
    </section>

    <h3 class="sec">
      提交
      <span v-if="pending" class="badge">{{ pending }} 条还没点</span>
    </h3>
    <div class="card">
      <p class="hint">
        outcome 和提示层级由你点，<b>quality 不在这里算</b> —— 后端的
        <code>quality_for()</code> 是唯一的 quality 来源。
        没点的行<b>不会提交</b> —— "没有信息"不是一个观察结果，是没有观察。
      </p>
      <OtherNotes v-model="otherNotes" />

      <p class="hint">
        <span v-if="draft.savedAt.value">草稿已存 {{ draft.savedAt.value }}（刷新不会丢）</span>
        <span v-else-if="draft.restored.value">已恢复上次填到一半的内容</span>
        <span v-else>填一半也没关系 —— 草稿会自动存服务端。</span>
      </p>

      <button :disabled="!canSubmit" @click="submitObservation">
        {{ obsRunning ? '处理中…' : submitting ? '提交中…' : '提交今天的反馈' }}
      </button>
      <p v-if="obsErr" class="err">{{ obsErr }}</p>
      <p v-if="fbErr" class="err">{{ fbErr }}</p>

      <JobProgress :steps="obsSteps" :done="!obsRunning && obsSteps.length > 0" />

      <!-- ⚡ 澄清 interrupt：图停在等人上，这是 Phase 1 的核心展示点 -->
      <div v-if="obsResult?.phase === 'clarify'" class="clarify">
        <h4>⚡ 有一处想跟你确认一下</h4>
        <p class="q">{{ obsResult.interrupt?.question }}</p>
        <p class="hint">
          我没把握的地方写下来是因为<strong>记错比记少更糟</strong> ——
          记错会误导明天的干预安排。你答完我会把两段话合起来重新理解一遍。
        </p>
        <textarea v-model="clarifyAnswer" rows="3"
          placeholder="补充一句就行；想不起来可以跳过"></textarea>
        <div class="row">
          <button class="mini-btn" :disabled="obsRunning" @click="answerClarify">
            {{ obsRunning ? '处理中…' : '答完，重新理解一遍' }}
          </button>
          <button class="ghost" :disabled="obsRunning" @click="clarifyAnswer = ''; answerClarify()">
            跳过（按「确实不知道」继续）
          </button>
        </div>
      </div>

      <!-- 四层：事实 / 家长的解读 / 待验证的假设 / 还缺的信息 -->
      <div v-if="obsResult?.phase === 'done'" class="layers">
        <h4>这段话我读到了什么</h4>
        <p v-if="obsResult.degraded" class="warn">
          离线模式下不抽取事件 —— 一条都不编。
        </p>
        <template v-else>
          <div v-if="obsResult.n_events">
            抽出 <b>{{ obsResult.n_events }}</b> 条事件。
          </div>
          <div v-if="obsResult.parent_interpretation" class="layer">
            <span class="tag">你自己的解读</span>{{ obsResult.parent_interpretation }}
          </div>
          <div v-if="obsResult.hypotheses?.length" class="layer">
            <span class="tag hyp">待验证的假设</span>
            <span class="dim">（不是结论）</span>
            <ul><li v-for="(h, i) in obsResult.hypotheses" :key="i">{{ h }}</li></ul>
          </div>
          <div v-if="obsResult.missing_information?.length" class="layer">
            <span class="tag miss">还缺</span>{{ obsResult.missing_information.join('；') }}
          </div>
          <div v-if="obsResult.escalation" class="esc">
            <b>⚠ 建议当天联系医生：</b>{{ obsResult.escalation.guidance }}
          </div>
        </template>
      </div>

      <div v-if="fbResult" class="result">
        <h4>反馈处理结果</h4>
        <pre class="mono">{{ fbResult.render }}</pre>
        <div class="checks">
          <div>
            ★ <b>refusal_counts = {{ JSON.stringify(fbResult.refusal_counts) }}</b>
            <span class="dim">（只有家长亲手填了的活动才可能有值）</span>
          </div>
          <div>
            落库 observations <b>{{ fbResult.n_observations }}</b> 条
            · 本次调模型 <b>{{ fbResult.llm_calls }}</b> 次
            · 耗时 {{ fbResult.elapsed_ms }}ms
          </div>
          <div v-if="fbResult.degraded" class="warn">
            补充描述未抽取事件 —— 一条都不编（降级模式不假装"今天成功了"）
          </div>
        </div>
      </div>

      <div v-if="fbResult?.close" class="card close">
        <h4>状态转移（纯 Python，零 token）</h4>
        <p class="mono">{{ fbResult.close.transition_text }}</p>
        <h4>日结（LLM 只写 flags 和给家长的一句话）</h4>
        <pre class="mono">{{ fbResult.close.summary_text }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 18px; margin-bottom: 18px; }
.head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
h2 { margin: 0; font-size: 17px; }
h3 { margin: 0; font-size: 15px; }
.sub { font-size: 12px; color: #6b7280; }
.sub.llm { color: #2563eb; }
.id { font-family: ui-monospace, monospace; font-size: 12px; color: #6b7280; margin-right: 4px; }
.hint { font-size: 12px; color: #888; margin: 8px 0 0; }
.why { font-size: 13px; margin: 6px 0; }
.act .why b { color: #374151; }
.diag { background: #f9fafb; }
.diag h3 { margin-bottom: 8px; }
.signal { padding: 4px 0; font-size: 13px; }
.signal.void { opacity: .5; text-decoration: line-through; }
.tag { display: inline-block; font-size: 11px; background: #e5e7eb; border-radius: 10px;
  padding: 1px 8px; margin-right: 7px; }
.imp { color: #2563eb; }
.fn { color: #9ca3af; font-size: 12px; margin-left: 8px; }
.quote { color: #9ca3af; font-size: 12px; }
.steps { padding-left: 22px; font-size: 14px; }
.steps li { margin-bottom: 10px; }
.ok { color: #15803d; font-size: 13px; }
.no { color: #b45309; font-size: 13px; }
.ladder { font-size: 13px; background: #f9fafb; padding: 8px 12px; border-radius: 6px; margin: 10px 0; }
.ladder ol { margin: 4px 0 0; padding-left: 20px; }
.refuse { background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px;
  padding: 10px 14px; font-size: 13px; margin: 12px 0; }
.abandon { color: #b45309; }
.check { font-size: 14px; }
.sec { font-size: 15px; margin: 22px 0 8px; color: #374151; }
.badge { font-size: 11px; background: #fef3c7; color: #b45309;
  border-radius: 4px; padding: 1px 6px; margin-left: 8px; }
.result { margin-top: 14px; border-top: 1px solid #f3f4f6; padding-top: 8px; }
.close { background: #f9fafb; margin-top: 14px; }
.checks { font-size: 13px; }
.checks > div { padding: 3px 0; }
.warn { color: #b45309; }
.dim { color: #888; font-size: 12px; }
.err { color: #dc2626; font-size: 13px; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; white-space: pre-wrap;
  background: #f9fafb; padding: 10px 12px; border-radius: 6px; margin: 4px 0; }

/* ⚡ 澄清 interrupt —— 图停下来等人的那一下，是 Phase 1 的核心展示点 */
.clarify { border: 1px solid #bfdbfe; background: #eff6ff;
  border-radius: 8px; padding: 12px 14px; margin-top: 12px; }
.clarify h4 { margin: 0 0 6px; color: #1d4ed8; }
.clarify .q { font-size: 15px; margin: 6px 0; font-weight: 600; }
.clarify textarea { width: 100%; box-sizing: border-box; font-family: inherit;
  font-size: 13px; padding: 8px 10px; border: 1px solid #d1d5db;
  border-radius: 6px; margin-top: 6px; }
.clarify .row { display: flex; gap: 10px; align-items: center; margin-top: 8px; }
.mini-btn { background: #2563eb; color: #fff; border: 0; border-radius: 6px;
  padding: 8px 16px; font-size: 13px; cursor: pointer; }
.mini-btn:disabled { background: #9ca3af; }
.ghost { background: none; border: none; color: #6b7280;
  font-size: 12px; cursor: pointer; text-decoration: underline; }

/* 四层 */
.layers { margin-top: 14px; border-top: 1px solid #f3f4f6; padding-top: 10px; font-size: 13px; }
.layers h4 { margin: 0 0 8px; }
.layer { margin: 6px 0; }
.layer ul { margin: 3px 0 0 18px; padding: 0; }
.tag { display: inline-block; font-size: 11px; border-radius: 10px;
  padding: 1px 8px; margin-right: 6px; background: #e5e7eb; color: #374151; }
.tag.hyp { background: #ede9fe; color: #6d28d9; }
.tag.miss { background: #fef3c7; color: #b45309; }
.esc { background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c;
  padding: 8px 12px; border-radius: 6px; margin-top: 8px; }
h4 { font-size: 13px; margin: 12px 0 4px; color: #374151; }
button.mini { background: #2563eb; color: #fff; border: 0; border-radius: 6px;
  padding: 6px 14px; font-size: 13px; cursor: pointer; margin-left: auto; }
button.mini:disabled { background: #9ca3af; }
.card > button:not(.mini) { background: #2563eb; color: #fff; border: 0;
  border-radius: 6px; padding: 9px 20px; font-size: 14px; cursor: pointer; margin-top: 12px; }
.card > button:disabled { background: #9ca3af; cursor: not-allowed; }
.rm { background: #fff; border: 1px solid #d1d5db; color: #6b7280; border-radius: 5px;
  padding: 2px 9px; font-size: 12px; cursor: pointer; margin-top: 6px; }
</style>
