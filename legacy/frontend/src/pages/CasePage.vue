<script setup>
// 案例页：一个孩子的一段持续跟进
//
// ★ 这一页只做四件事
// ------------------
// 1. 建案例 / 选案例
// 2. 说一段话（建案例时说的第一段，或后来的补充）
// 3. 回答系统问的那一句
// 4. 看四栏：我们已经知道的 / 我们的猜测 / 还不清楚的 / 两处说法对不上
//
// ★ 措辞全部来自后端
// --------------------
// 栏目标题、状态徽标、每条证据的「谁说的」角标 —— 都是 API 返回的
// `label` / `hint`。**前端不硬编码任何家长看得到的字**。
// 理由：措辞改一次要改一整页的前端，而前端一多就必然漏一处。
//
// ★ 两阶段协议
// ------------
// 建案例 / 补充描述 / 回答追问，三个动作**都可能**返回 phase="clarify"
// （图停在 interrupt 上等家长回答）。那不是错误，是一个正常结果。
import { ref, onMounted } from 'vue'
import { api, streamJob } from '../api.js'

const props = defineProps({ CHILD: { type: String, required: true } })

const cases = ref([])
const cur = ref(null)          // 当前案例的完整状态（后端给的形状）
const busy = ref(false)
const steps = ref('')
const err = ref('')

const title = ref('')
const text = ref('')

// ★ 追问的回答：按问题下标存
//   { [i]: { chosen: '选中的选项文字' | '', text: '自己打的字' } }
const answers = ref({})
const active = ref(0)          // 当前显示第几个问题

const COLS = ['known_facts', 'hypotheses', 'unknowns', 'contradictions']

async function loadList() {
  const r = await api.caseList(props.CHILD)
  cases.value = r.cases || []
}

async function loadCase(cid) {
  cur.value = await api.caseGet(cid)
  resetAnswers()
}

// 换一批问题时，之前答的作废（问题变了，答案对不上号了）。
function resetAnswers() {
  answers.value = {}
  active.value = 0
}

const questions = () => cur.value?.clarify?.questions || []

// 这一题答了没有？——选了选项，或者自己打了字。
function answered(i) {
  const a = answers.value[i]
  if (!a) return false
  return !!(a.chosen || (a.text || '').trim())
}

const allAnswered = () => questions().every((_, i) => answered(i))

function choose(i, label) {
  const a = answers.value[i] || { chosen: '', text: '' }
  answers.value[i] = { chosen: label, text: '' }   // 选了选项就清掉自己打的字
}

function typeText(i, v) {
  const a = answers.value[i] || { chosen: '', text: '' }
  // ★ 自己打字 = 不选任何选项。两者只能有一个。
  answers.value[i] = { chosen: '', text: v }
}

async function createCase() {
  if (!text.value.trim()) { err.value = '先写一句要说的'; return }
  err.value = ''
  busy.value = true
  steps.value = '在整理你说的话…'
  try {
    const { case_id, job_id } = await api.caseCreate({
      child_id: props.CHILD, title: title.value, text: text.value,
    })
    const done = await streamJob(job_id, (d) => { steps.value = d.text || steps.value })
    cur.value = done.result
    text.value = ''
    title.value = ''
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function send() {
  if (!text.value.trim() || !cur.value) return
  err.value = ''
  busy.value = true
  steps.value = '在整理你说的话…'
  try {
    const { job_id } = await api.caseMessage(cur.value.case_id, { text: text.value })
    const done = await streamJob(job_id, (d) => { steps.value = d.text || steps.value })
    cur.value = done.result
    text.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function reply() {
  if (!cur.value) return
  err.value = ''
  busy.value = true
  steps.value = '在想你说的这件事…'
  try {
    // ★ 一次提交**全部**问题的回答。
    //   每条都带 unknown 回指 —— 后端靠它把「还不清楚的」里那一项销掉，
    //   靠顺序对齐是不行的（问题顺序变了就对不上号）。
    const payload = questions().map((q, i) => {
      const a = answers.value[i] || {}
      return {
        unknown: q.unknown || '',
        answer: a.chosen || (a.text || '').trim(),
        skipped: false,
      }
    })
    const { job_id } = await api.caseClarify(cur.value.case_id, { answers: payload })
    const done = await streamJob(job_id, (d) => { steps.value = d.text || steps.value })
    cur.value = done.result
    resetAnswers()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

// 刷新页面后要能发现「还有个问题等你答」—— 靠重新读一次当前案例。
async function refresh() {
  if (cur.value) await loadCase(cur.value.case_id)
  await loadList()
}

onMounted(async () => {
  await loadList()
  if (cases.value.length) await loadCase(cases.value[0].case_id)
})
</script>

<template>
  <div>
    <!-- 案例列表 -->
    <div class="card">
      <div class="sec">案例</div>
      <p class="hint">
        一个案例 = 一段持续跟进。家长前后多次说同一件事，系统把它攒成一份可追溯的评估。
      </p>
      <div v-if="cases.length" class="caselist">
        <button v-for="c in cases" :key="c.case_id" class="mini"
                :class="{ on: cur && cur.case_id === c.case_id }"
                @click="loadCase(c.case_id)">
          {{ c.title || c.case_id }} · {{ c.status_label?.label }}
        </button>
      </div>
      <p v-else class="dim">还没有案例。下面写一段话就能建一个。</p>
    </div>

    <!-- 新建案例 -->
    <div class="card" v-if="!cur">
      <div class="sec">新建案例</div>
      <input v-model="title" placeholder="一句话标题（可不填）" style="width:100%;padding:6px 8px;border:1px solid #d1d5db;border-radius:6px;margin-bottom:8px;" />
      <textarea v-model="text" rows="5" style="width:100%;padding:8px;border:1px solid #d1d5db;border-radius:6px;font:inherit;"
                placeholder="说说这个孩子的情况，越具体越好。例如：他在幼儿园不太愿意靠近别的小朋友，但是老师一个人抱他讲故事他就能回答问题。"></textarea>
      <p class="hint">写完点下面的按钮，系统会先把这段话里的事实一条条记下来，缺什么会反过来问你一句。</p>
      <button class="mini" :disabled="busy" @click="createCase">
        {{ busy ? '在整理…' : '记下来' }}
      </button>
      <span v-if="steps" class="dim"> {{ steps }}</span>
    </div>

    <!-- 当前案例 -->
    <template v-if="cur">
      <div class="card">
        <div class="sec">
          {{ cur.title || cur.case_id }}
          <span class="badge">{{ cur.status_label?.label }}</span>
        </div>
        <p v-if="cur.status_label?.hint" class="hint">{{ cur.status_label.hint }}</p>

        <!-- 追问卡：一次给全，逐题选，最后一次性提交 -->
        <div v-if="cur.clarify?.pending && questions().length" class="ask">
          <h4>{{ cur.clarify.labels?.title || '想问你一句' }}</h4>
          <p class="hint">{{ cur.clarify.labels?.hint }}（{{ questions().length }} 个问题，答完一起交）</p>

          <!-- 题目切换条：一眼看出哪几题还没答 -->
          <div class="qtabs">
            <button v-for="(q, i) in questions()" :key="i" class="qtab"
                    :class="{ on: active === i, done: answered(i) }"
                    @click="active = i">
              {{ i + 1 }}<span v-if="answered(i)" class="ok">✓</span>
            </button>
          </div>

          <!-- 当前这一题 -->
          <div class="qbody" v-if="questions()[active]">
            <p class="q">{{ questions()[active].question }}</p>
            <p class="hint" v-if="questions()[active].unknown">还不清楚的是：{{ questions()[active].unknown }}</p>

            <div class="opts">
              <button v-for="(o, j) in questions()[active].options" :key="j"
                      class="opt" :class="{ on: (answers[active] || {}).chosen === o.label }"
                      @click="choose(active, o.label)">
                <span class="olabel">{{ o.label }}</span>
                <span v-if="o.hint" class="ohint">{{ o.hint }}</span>
              </button>
            </div>

            <input :value="(answers[active] || {}).text || ''"
                   @input="typeText(active, $event.target.value)"
                   placeholder="都不对？我自己说…"
                   style="width:100%;padding:7px 9px;border:1px solid #d1d5db;border-radius:6px;font:inherit;margin-top:8px;" />

            <div class="qnav">
              <button class="mini" :disabled="active === 0" @click="active = active - 1">上一题</button>
              <button class="mini" :disabled="active >= questions().length - 1" @click="active = active + 1">下一题</button>
            </div>
          </div>

          <div class="qsubmit">
            <button class="mini strong" :disabled="busy || !allAnswered()" @click="reply">
              {{ busy ? '在想…' : `交这 ${questions().length} 个答案` }}
            </button>
            <span v-if="!allAnswered()" class="dim">还有 {{ questions().filter((_, i) => !answered(i)).length }} 题没答</span>
          </div>
        </div>

        <p v-if="busy" class="dim">{{ steps }}</p>
        <p v-if="err" class="err">{{ err }}</p>
      </div>

      <!-- 四栏 -->
      <div class="cols">
        <div v-for="k in COLS" :key="k" class="card col">
          <div class="sec">{{ cur.columns?.[k]?.label || k }}</div>
          <p class="hint">{{ cur.columns?.[k]?.hint }}</p>
          <ul v-if="(cur[k] || []).length">
            <li v-for="(v, i) in cur[k]" :key="i">
              <template v-if="typeof v === 'string'">{{ v }}</template>
              <template v-else>「{{ v.a }}」 ↔ 「{{ v.b }}」</template>
            </li>
          </ul>
          <p v-else class="dim">（还没有）</p>
        </div>
      </div>

      <!-- 证据 -->
      <div class="card" v-if="(cur.evidence || []).length">
        <div class="sec">你说过的话</div>
        <div v-for="(e, i) in cur.evidence" :key="i" class="ev">
          <div class="evclaim">{{ e.claim }}</div>
          <div v-if="e.quote" class="evquote">“{{ e.quote }}”</div>
          <div class="evmeta">
            <span class="tag">{{ e.origin?.label || '我们记录的' }}</span>
            <span v-if="e.origin?.hint" class="dim"> {{ e.origin.hint }}</span>
            <span v-if="e.issue" class="warn"> {{ e.issue }}</span>
          </div>
        </div>
      </div>

      <!-- 继续说 -->
      <div class="card">
        <div class="sec">再补充一点</div>
        <textarea v-model="text" rows="4" style="width:100%;padding:8px;border:1px solid #d1d5db;border-radius:6px;font:inherit;"
                  placeholder="想到什么就写什么，不用组织语言"></textarea>
        <button class="mini" :disabled="busy" @click="send">
          {{ busy ? '在整理…' : '记下来' }}
        </button>
        <button class="mini" style="margin-left:8px" @click="refresh">刷新</button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.caselist { display: flex; flex-wrap: wrap; gap: 6px; }
.caselist .mini.on { background: #2563eb; color: #fff; border-color: #2563eb; }
.badge { display: inline-block; margin-left: 8px; padding: 1px 8px; border-radius: 10px;
  background: #eff6ff; color: #1d4ed8; font-size: 12px; font-weight: 400; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.cols .card { margin-bottom: 0; }
.cols ul { margin: 0; padding-left: 18px; font-size: 14px; line-height: 1.7; }
.ask { background: #fffbeb; border: 1px solid #fcd34d; border-radius: 8px; padding: 12px 14px; margin: 8px 0; }
.ask .q { font-size: 15px; margin: 4px 0 8px; }
.qtabs { display: flex; gap: 6px; margin: 8px 0 4px; flex-wrap: wrap; }
.qtab { width: 30px; height: 30px; border-radius: 50%; border: 1px solid #d1d5db;
  background: #fff; color: #6b7280; font-size: 13px; cursor: pointer; position: relative; }
.qtab.on { border-color: #d97706; background: #fff; color: #b45309; font-weight: 700;
  box-shadow: 0 0 0 2px #fde68a; }
.qtab.done { border-color: #16a34a; color: #16a34a; }
.qtab .ok { position: absolute; right: -2px; top: -4px; font-size: 10px; color: #16a34a; }
.opts { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.opt { text-align: left; border: 1px solid #d1d5db; background: #fff; border-radius: 7px;
  padding: 8px 11px; cursor: pointer; display: flex; flex-direction: column; gap: 2px; }
.opt:hover { border-color: #d97706; background: #fffdf5; }
.opt.on { border-color: #d97706; background: #fef3c7; font-weight: 600; }
.olabel { font-size: 14px; }
.ohint { font-size: 12px; color: #888; font-weight: 400; }
.qnav { display: flex; gap: 6px; margin-top: 10px; }
.qsubmit { display: flex; align-items: center; gap: 10px; margin-top: 12px;
  border-top: 1px solid #fde68a; padding-top: 10px; }
button.mini.strong { background: #d97706; border-color: #d97706; color: #fff; font-weight: 600; }
button.mini.strong:disabled { background: #f3f4f6; border-color: #d1d5db; color: #9ca3af; }
.ev { border-top: 1px solid #f1f5f9; padding: 10px 0; }
.ev:first-of-type { border-top: none; }
.evclaim { font-size: 14px; }
.evquote { font-size: 13px; color: #6b7280; margin: 2px 0; }
.evmeta { font-size: 12px; }
.tag { background: #f3f4f6; border-radius: 8px; padding: 0 7px; color: #4b5563; }
@media (max-width: 640px) { .cols { grid-template-columns: 1fr; } }
</style>
