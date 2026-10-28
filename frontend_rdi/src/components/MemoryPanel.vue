<script setup>
// 长期记忆面板（两层设计）。
//
// **这个面板存在的意义**：计划 §10.7 要求"家长可以查看、纠正、删除记忆"。
// 光有接口不算 —— 家长得能看见"系统到底记住了我孩子什么"。
//
// **记忆现在只有两层**：
//
// 1. **拆句** —— 家长的一段话拆成几句短句，每句只讲一件事。
// 2. **分类** —— 每句归一个类目（分离与过渡、沟通与表达、社交与游戏、
//    情绪与调节、感觉与动作、生活与自理、家庭与目标）。
//
// 同类合并之后，每个类目一条**小结**，那就是家长平时看到的东西。
// 小结里点「由 N 句归纳而来」能展开看到归纳自的哪几句原话 —— 归纳不是黑盒。
//
// **2026-10-27 调整面板**：
// - **去掉「原话」那一栏** —— 短句不再单独列一遍，
//   要看原话从小结里展开就行。
// - **「家长的要求」和「系统的推测」暂时不显示**（模板里注释掉了）——
//   这两类不进类目合并，是单独进 prompt 的通道，
//   放在一起看着乱。先收起来，等确认要不要长期保留。
// **只有"删除"按钮**：记忆是自动维护的 —— 系统自己记、自己合并、自己纠正。
// 家长想改什么，**直接在对话里说"不是这样"就行**。
import { computed, ref } from 'vue'

const props = defineProps({
  memories: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['delete'])

// 类目顺序。**和 memory/categories.py 里的 CATEGORIES 一致** ——
// 顺序就是展示顺序，从"最影响建议怎么写"排到"最不影响"。
const CATEGORY_ORDER = [
  'separation',
  'communication',
  'social',
  'emotion',
  'sensory_motor',
  'daily',
  'family',
  'other',
]

const CATEGORY_LABEL = {
  separation: '分离与过渡',
  communication: '沟通与表达',
  social: '社交与游戏',
  emotion: '情绪与调节',
  sensory_motor: '感觉与动作',
  daily: '生活与自理',
  family: '家庭与目标',
  other: '其他',
}

const KIND_HINT = {
  summary: '系统归纳出来的现状（由下面的短句汇总而来）',
  note: '家长说过的一句话，一句只讲一件事',
  attempt: '系统给过、家长选过的方案（只读）',
}

const summaries = computed(() => props.memories.filter((m) => m.kind === 'summary'))
const notes = computed(() => props.memories.filter((m) => m.kind === 'note'))
const attempts = computed(() => props.memories.filter((m) => m.kind === 'attempt'))

// 家长要求的、系统的推测。**暂时不显示**（模板里注释掉了），
// 但 computed 留着 —— 要恢复显示只差模板那一段。
const preferences = computed(() => notes.value.filter((m) => m.is_preference))
const inferences = computed(() => notes.value.filter((m) => m.inference))

// 按类目分组。没有类目的塞到 other。
function groupByCategory(items) {
  const byCategory = {}
  for (const s of items) {
    const key = s.category || 'other'
    ;(byCategory[key] ||= []).push(s)
  }
  const order = CATEGORY_ORDER.filter((c) => byCategory[c]?.length)
  const extra = Object.keys(byCategory).filter((c) => !CATEGORY_ORDER.includes(c))
  return [...order, ...extra].map((category) => ({
    category,
    label: CATEGORY_LABEL[category] || category,
    items: byCategory[category],
  }))
}

const summaryGroups = computed(() => groupByCategory(summaries.value))

// 展开状态：哪些小结要看它的原始短句。
const expanded = ref({})

function toggleExpanded(id) {
  expanded.value = { ...expanded.value, [id]: !expanded.value[id] }
}

// 找一条小结归纳自的短句。
function sourcesOf(summary) {
  const ids = new Set(summary.derived_from || [])
  if (!ids.size) return []
  return notes.value.filter((m) => ids.has(m.memory_id))
}

const STATUS_LABEL = {
  confirmed: '已确认',
  active: '已确认',
  superseded: '已作废',
  deleted: '已删除',
}

function statusClass(item) {
  if (['confirmed', 'active'].includes(item.status)) return 'ok'
  return ''
}

function askDelete(item) {
  // 删除前问一句 —— 这是不可逆操作（软删，但页面上就看不见了）。
  if (window.confirm(`确定删掉这条记忆吗？\n\n${item.content}`)) {
    emit('delete', item)
  }
}
</script>

<template>
  <div class="card">
    <header>
      <h3>长期记忆</h3>
    </header>

    <div class="small muted intro">
      系统自动记、自动整理。有不对的，直接在对话里说"不是这样"就行。
    </div>

    <div v-if="!memories.length" class="small muted">
      这个孩子还没有记录。聊过几轮之后，稳定的信息会慢慢留在这里。
    </div>

    <!-- ---- 小结：家长平时看的就这些 ---- -->
    <section v-for="g in summaryGroups" :key="`sum-${g.category}`">
      <h4>{{ g.label }}</h4>

      <div v-for="m in g.items" :key="m.memory_id" class="mem">
        <div class="row">
          <span class="badge" :class="statusClass(m)">{{ STATUS_LABEL[m.status] || m.status }}</span>
          <span v-if="m.source_kind === 'direct'" class="badge">仅一次记录</span>
          <span v-if="m.confidence_level" class="badge">
            把握 {{ { high: '高', medium: '中', low: '低' }[m.confidence_level] || m.confidence_level }}
          </span>
        </div>

        <div class="content">{{ m.content }}</div>

        <div v-if="m.scope_conditions?.length" class="small muted">
          成立条件：{{ m.scope_conditions.join('、') }}
        </div>

        <!-- 归纳来源：点开能看到它是从哪几句来的 -->
        <div v-if="m.derived_from?.length" class="sources">
          <button class="link small" @click="toggleExpanded(m.memory_id)">
            {{ expanded[m.memory_id] ? '收起原话' : `由 ${m.derived_from.length} 句归纳而来` }}
          </button>
          <div v-if="expanded[m.memory_id]" class="source-list">
            <div v-for="s in sourcesOf(m)" :key="s.memory_id" class="small muted src">
              · {{ s.content }}
            </div>
            <div v-if="!sourcesOf(m).length" class="small muted src">
              （原话已被删除）
            </div>
          </div>
        </div>

        <div class="row actions">
          <button class="small" :disabled="busy" @click="askDelete(m)">删除</button>
        </div>
      </div>
    </section>

    <!-- ---- 家长的要求：硬约束，不参与合并 ----
         2026-10-27：暂时不显示，等确认要不要保留这一栏。
         它不进类目合并，是单独进 prompt 的硬约束。
         要恢复显示，把这段注释去掉即可（preferences / inferences
         两个 computed 还留着）。
    <section v-if="preferences.length">
      <h4>家长的要求 <span class="small muted">· 不参与合并，直接影响建议</span></h4>
      <div v-for="m in preferences" :key="m.memory_id" class="mem">
        <div class="row">
          <span class="badge" :class="statusClass(m)">{{ STATUS_LABEL[m.status] || m.status }}</span>
          <span v-if="m.category" class="badge">{{ CATEGORY_LABEL[m.category] || m.category }}</span>
        </div>
        <div class="content">{{ m.content }}</div>
        <div v-if="m.source?.quote" class="quote">原话：{{ m.source.quote }}</div>
        <div class="row actions">
          <button class="small" :disabled="busy" @click="askDelete(m)">删除</button>
        </div>
      </div>
    </section>
    -->

    <!-- ---- 系统的推测：家长没这么说 ----
         2026-10-27：同上，暂时不显示。
    <section v-if="inferences.length">
      <h4>系统的推测 <span class="small muted">· 还没验证，不能当结论</span></h4>
      <div v-for="m in inferences" :key="m.memory_id" class="mem">
        <div class="row">
          <span class="badge" :class="statusClass(m)">{{ STATUS_LABEL[m.status] || m.status }}</span>
          <span v-if="m.category" class="badge">{{ CATEGORY_LABEL[m.category] || m.category }}</span>
          <span class="badge">未验证</span>
        </div>
        <div class="content">{{ m.content }}</div>
        <div v-if="m.scope_conditions?.length" class="small muted">
          适用条件：{{ m.scope_conditions.join('、') }}
        </div>
        <div class="row actions">
          <button class="small" :disabled="busy" @click="askDelete(m)">删除</button>
        </div>
      </div>
    </section>
    -->

    <!-- ---- 尝试：只读投影 ---- -->
    <section v-if="attempts.length">
      <h4>尝试 <span class="small muted">· {{ KIND_HINT.attempt }}</span></h4>
      <div v-for="m in attempts" :key="m.memory_id" class="mem">
        <div class="content">{{ m.content }}</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
section { margin-top: 12px; }
h4 { margin: 12px 0 6px; font-size: 13.5px; color: var(--color-text-secondary); font-weight: 600; }
.intro { margin-bottom: 8px; }
.mem { border-top: 1px solid var(--color-border); padding: 12px 0; }
.mem:first-of-type { border-top: none; }
.content { margin: 6px 0; }
.quote {
  margin: 6px 0; padding: 6px 10px;
  border-left: 2px solid var(--color-border-strong);
  color: var(--color-text-secondary); font-size: 13px;
}
.sources { margin: 6px 0; }
.source-list { margin-top: 6px; padding-left: 6px; border-left: 2px solid var(--color-border-strong); }
.src { margin: 3px 0; }
button.link {
  background: none; border: none; padding: 0;
  color: var(--color-brand-text); text-decoration: underline; cursor: pointer;
}
button.link:hover:not(:disabled) { background: none; color: var(--color-brand-deep); }
.actions { margin-top: 6px; }
</style>
