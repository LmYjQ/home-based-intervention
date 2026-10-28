<script setup>
// 一张建议卡。
//
// 这一版把**引用下沉到片段**：
// 说明被拆成 explanation_parts，每段带自己的 evidence_ids，
// 段后面挂一个编号（[1]、[2]…），编号对应卡下方"依据"里的那一条。
// 家长点编号就能看到"这一句的依据是指南第几页的那一段"。
//
// **依据默认折叠**：家长先要读的是建议本身，依据是"想核对时再看"的东西。
// 折叠起来卡片才不会被一屏引用淹掉。
//
// **为什么是点开而不是 hover**：hover 隐藏内容在触屏上根本点不到，
// 而且"鼠标移到哪"是不可发现的 —— 用户不会知道那里有东西。
// 显式的开关（带条数）才既可发现又可控。
import { computed, ref } from 'vue'

const props = defineProps({
  plan: { type: Object, required: true },
  selectable: { type: Boolean, default: false },
  selected: { type: Boolean, default: false },
  // 本轮的证据清单。用来把片段上的 evidence_ids 和指南段落对上 ——
  // 片段里存的是 id，章节和页码在证据那边。
  evidence: { type: Array, default: () => [] },
})
defineEmits(['select'])

const LABEL_TEXT = {
  source_summary: '指南依据',
  ai_adaptation: '应用转化',
  expert_reviewed: '专业审核',
}

const labelText = computed(
  () => LABEL_TEXT[props.plan.application_label] || props.plan.application_label,
)

// 证据按 evidence_id 建索引。
const evidenceIndex = computed(() => {
  const map = new Map()
  for (const e of props.evidence || []) {
    if (e?.evidence_id) map.set(e.evidence_id, e)
  }
  return map
})

// 说明的片段。老卡（数据库里已经落库的那些）只有一整段 explanation，
// 这里兜住，别让老数据显示成空白。
const parts = computed(() => {
  const list = props.plan.explanation_parts || []
  if (list.length) return list
  if (props.plan.explanation) return [{ text: props.plan.explanation, evidence_ids: [] }]
  return []
})

// 编号表：按"说明段 → 动作 → 卡级引用"的顺序给证据编号。
//
// **顺序必须和后端一致**（graph/nodes/retrieval_nodes.py::_render_cards）：
// 教练回复那段文字里的 [1] 就是这里的 [1]。两边顺序不一样，
// 家长对着文字和卡片会看到两个不同的编号。
const numbered = computed(() => {
  const order = []
  const push = (id) => {
    if (id && !order.includes(id)) order.push(id)
  }
  for (const p of parts.value) for (const id of p.evidence_ids || []) push(id)
  for (const a of props.plan.parent_actions || []) for (const id of a.evidence_ids || []) push(id)
  for (const c of props.plan.source_citations || []) push(c.source_id)
  return order.map((id, i) => ({
    no: i + 1,
    id,
    item: evidenceIndex.value.get(id) || null,
  }))
})

const numberOf = (id) => numbered.value.find((n) => n.id === id)?.no || null

// ---- 依据区块的展开/收起 ----
const showEvidence = ref(false)
// 点某个编号时展开并滚到那一条
const activeNo = ref(null)
const evidenceRefs = ref({})

function toggleEvidence() {
  showEvidence.value = !showEvidence.value
  if (!showEvidence.value) activeNo.value = null
}

function jumpTo(no) {
  showEvidence.value = true
  activeNo.value = no
  // 等 DOM 出来再滚
  setTimeout(() => {
    evidenceRefs.value[no]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, 0)
}

function setEvidenceRef(no, el) {
  if (el) evidenceRefs.value[no] = el
  else delete evidenceRefs.value[no]
}

// 这张卡引用到的证据 id（调试用）
const citedIds = computed(() => numbered.value.map((n) => n.id))

defineExpose({ citedIds })
</script>

<template>
  <div class="plan" :class="{ rec: plan.recommended }">
    <div class="row">
      <h4>{{ plan.title }}</h4>
      <span v-if="plan.recommended" class="badge accent">★ 推荐</span>
      <span class="spacer"></span>
      <span class="badge" :class="{ warn: plan.application_label === 'source_summary' }">
        {{ labelText }}
      </span>
    </div>

    <div class="small muted">plan_id: {{ plan.plan_id }}</div>

    <section v-if="plan.observed_facts?.length">
      <h4>依据事实</h4>
      <ul><li v-for="(f, i) in plan.observed_facts" :key="i">{{ f }}</li></ul>
    </section>

    <section v-if="plan.tentative_interpretations?.length">
      <h4>可能解释（未证实）</h4>
      <ul><li v-for="(f, i) in plan.tentative_interpretations" :key="i">{{ f }}</li></ul>
    </section>

    <section v-if="parts.length">
      <h4>说明</h4>
      <!--
        每段自己带依据。编号是可点的 —— 点一下就展开下方"依据"里对应的那一条。
        没有依据的段落（应用转化）不显示编号，这样家长一眼能看出
        哪句话是指南说的、哪句话是我们建议的。
      -->
      <p v-for="(p, i) in parts" :key="i" class="part">
        {{ p.text
        }}<button
          v-for="(id, j) in (p.evidence_ids || []).filter((x) => numberOf(x) !== null)"
          :key="j"
          class="marker"
          :title="`看这条的依据（${numberOf(id)}）`"
          @click="jumpTo(numberOf(id))"
        >{{ numberOf(id) }}</button>
      </p>
    </section>

    <section v-if="plan.parent_actions?.length">
      <h4>家长可以做的事</h4>
      <div v-for="(a, i) in plan.parent_actions" :key="i" class="action">
        <div>
          {{ a.action
          }}<button
            v-for="(id, j) in (a.evidence_ids || []).filter((x) => numberOf(x) !== null)"
            :key="j"
            class="marker"
            :title="`看这条的依据（${numberOf(id)}）`"
            @click="jumpTo(numberOf(id))"
          >{{ numberOf(id) }}</button>
          <span v-if="a.context" class="small muted">情境：{{ a.context }}</span>
        </div>
        <ul v-if="a.steps?.length"><li v-for="(s, j) in a.steps" :key="j">{{ s }}</li></ul>
        <ul v-if="a.observe?.length">
          <li v-for="(s, j) in a.observe" :key="j">观察：{{ s }}</li>
        </ul>
        <ul v-if="a.reduce_or_stop_if?.length">
          <li v-for="(s, j) in a.reduce_or_stop_if" :key="j">减量/停止：{{ s }}</li>
        </ul>
      </div>
    </section>

    <section v-if="plan.success_indicators?.length">
      <h4>怎么算有效果</h4>
      <ul><li v-for="(f, i) in plan.success_indicators" :key="i">{{ f }}</li></ul>
    </section>

    <section v-if="plan.stop_conditions?.length">
      <h4>什么时候该停</h4>
      <ul><li v-for="(f, i) in plan.stop_conditions" :key="i">{{ f }}</li></ul>
    </section>

    <!-- 依据：默认折叠，点开才展开 -->
    <section>
      <button class="toggle" @click="toggleEvidence">
        <span class="caret">{{ showEvidence ? '▾' : '▸' }}</span>
        这次的依据（{{ numbered.length }} 条{{ showEvidence ? '' : '，点开看章节和页码' }}）
      </button>

      <div v-if="showEvidence" class="evidence-box">
        <div v-if="!numbered.length" class="small muted">
          （空）—— 这张卡没有直接引用指南原文，是根据你描述的情况给出的。
        </div>
        <div
          v-for="n in numbered"
          :key="n.no"
          :ref="(el) => setEvidenceRef(n.no, el)"
          class="small cite"
          :class="{ active: activeNo === n.no }"
        >
          <div>
            <span class="cite-no">{{ n.no }}</span>
            <template v-if="n.item">
              《{{ n.item.section_title || n.item.title }}》
              <span v-if="n.item.pdf_page_start != null">
                · PDF 第 {{ n.item.pdf_page_start }}–{{ n.item.pdf_page_end }} 页
              </span>
            </template>
            <template v-else>
              <span class="warn-text">⚠ 没在本次证据里找到这一条</span>
              <code>{{ n.id }}</code>
            </template>
          </div>
          <div v-if="n.item" class="muted">这段的大意：{{ n.item.summary }}</div>
          <!-- 卡级引用里的"支撑了哪句话"也要能看到 -->
          <div
            v-for="(c, i) in (plan.source_citations || []).filter((x) => x.source_id === n.id)"
            :key="i"
            class="muted"
          >
            支撑：{{ c.claim_supported }}
          </div>
        </div>
      </div>
    </section>

    <section v-if="plan.uncertainty_notes?.length">
      <h4>不确定性</h4>
      <ul><li v-for="(f, i) in plan.uncertainty_notes" :key="i">{{ f }}</li></ul>
    </section>

    <div v-if="selectable" class="row" style="margin-top:12px">
      <button class="primary" @click="$emit('select', plan.plan_id)">
        {{ selected ? '已选择（可改选）' : '选这张' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.action { margin-bottom: 10px; }
.part { margin: 6px 0; }
.cite { border-left: 2px solid var(--color-border-strong); padding-left: 12px; margin-bottom: 10px; }
code { font-size: 12px; color: var(--color-text-secondary); }
/* 引用没对上证据 —— 这是要立刻看见的问题，不该灰着过去 */
.warn-text { color: var(--color-warn); }

/* 片段里的编号角标：像脚注一样，但可点 */
.marker {
  display: inline-block;
  min-width: 18px;
  height: 18px;
  margin: 0 1px 0 4px;
  padding: 0 5px;
  border: 1px solid var(--color-info);
  border-radius: 9px;
  background: transparent;
  color: var(--color-info);
  font-size: 12px;
  line-height: 16px;
  vertical-align: super;
  cursor: pointer;
}
.marker:hover { background: var(--color-info-soft); }

/* 依据区块的开关 */
.toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 0;
  border: 0;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.toggle:hover { color: var(--color-brand-text); }
.caret { font-size: 10px; }
.evidence-box {
  border-left: 2px solid var(--color-border-strong);
  padding-left: 12px;
  margin-bottom: 10px;
}
.cite.active {
  border-left-color: var(--color-info);
  background: var(--color-info-soft);
  border-radius: 0 8px 8px 0;
}
.cite-no {
  display: inline-block;
  min-width: 18px;
  margin-right: 6px;
  color: var(--color-info);
  font-weight: 600;
}
</style>
