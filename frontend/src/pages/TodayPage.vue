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
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api.js'
import { runJob } from '../api.js'
import JobProgress from '../components/JobProgress.vue'
import AgentTrace from '../components/AgentTrace.vue'
import FeedbackItem from '../components/FeedbackItem.vue'
import OtherNotes from '../components/OtherNotes.vue'

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

const canSubmit = computed(() =>
  feedbacks.value.some(f => f.outcome) && !submitting.value)
const pending = computed(() => feedbacks.value.filter(f => !f.outcome).length)

async function submitFeedback() {
  submitting.value = true; fbErr.value = ''
  try {
    const filled = feedbacks.value
      .filter(f => f.outcome)      // ★ 没点的不发 —— "没有信息"不是一种观察结果
      .map(f => ({
        activity_id: f.activity_id, outcome: f.outcome,
        prompt_level: f.prompt_level, steps_done: f.steps_done, note: f.note,
      }))
    fbResult.value = await api.feedbacks({
      child_id: props.CHILD, date: props.date, feedbacks: filled,
      other_notes: otherNotes.value,
    })
    emit('refresh-state')
  } catch (e) { fbErr.value = e.message } finally { submitting.value = false }
}

defineExpose({ load })
</script>

<template>
  <div>
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
      <button :disabled="!canSubmit" @click="submitFeedback">
        {{ submitting ? '提交中…' : '提交今天的反馈' }}
      </button>
      <p v-if="fbErr" class="err">{{ fbErr }}</p>

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
