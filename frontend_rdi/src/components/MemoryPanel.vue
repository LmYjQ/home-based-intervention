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
// 短句在下面按类目列出来，可以展开看归纳来源 —— 归纳不是黑盒。
//
// **三种短句分开显示**（它们进不进 prompt 的方式不一样）：
// - 普通短句 —— 参与合并。
// - 家长的要求（偏好）—— 硬约束，不参与合并。
// - 系统的推测 —— 家长没这么说，不参与合并。
//
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

// 三种短句分开：参与合并的、家长要求的、系统的推测。
const plainNotes = computed(() =>
  notes.value.filter((m) => !m.is_preference && !m.inference),
)
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
const noteGroups = computed(() => groupByCategory(plainNotes.value))

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
      <span class="badge">{{ memories.length }} 条</span>
    </header>

    <div class="small muted intro">
      系统自动记、自动整理。有不对的，直接在对话里说"不是这样"就行。
    </div>

    <div v-if="!memories.length" class="small muted">
      这个孩子还没有任何长期记忆。说几轮之后，系统会把稳定信息记下来。
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

    <!-- ---- 家长的要求：硬约束，不参与合并 ---- -->
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

    <!-- ---- 系统的推测：家长没这么说 ---- -->
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

    <!-- ---- 原话：作依据，同类合并成上面的小结 ---- -->
    <section v-for="g in noteGroups" :key="`note-${g.category}`">
      <h4>{{ g.label }} <span class="small muted">· 原话</span></h4>
      <div v-for="m in g.items" :key="m.memory_id" class="mem">
        <div class="row">
          <span class="badge" :class="statusClass(m)">{{ STATUS_LABEL[m.status] || m.status }}</span>
        </div>

        <div class="content">{{ m.content }}</div>

        <div v-if="m.scope_conditions?.length" class="small muted">
          适用条件：{{ m.scope_conditions.join('、') }}
        </div>
        <div v-if="m.source?.quote" class="quote">原话：{{ m.source.quote }}</div>
        <div class="small muted">
          {{ m.source?.kind === 'feedback' ? '来自执行反馈' : '来自对话' }}
          <span v-if="m.source?.turn_id"> · {{ m.source.turn_id }}</span>
          <span v-if="m.supersedes_memory_id"> · 取代了 {{ m.supersedes_memory_id }}</span>
          <span v-if="m.superseded_by_memory_id"> · 已被 {{ m.superseded_by_memory_id }} 取代</span>
        </div>

        <div class="row actions">
          <button class="small" :disabled="busy" @click="askDelete(m)">删除</button>
        </div>
      </div>
    </section>

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
section { margin-top: 10px; }
h4 { margin: 10px 0 4px; font-size: 12px; color: var(--fg-dim); font-weight: 600; }
.intro { margin-bottom: 6px; }
.mem { border-top: 1px solid var(--line); padding: 8px 0; }
.mem:first-of-type { border-top: none; }
.content { margin: 4px 0; }
.quote {
  margin: 4px 0; padding: 4px 8px;
  border-left: 2px solid var(--line);
  color: var(--fg-dim); font-size: 12px;
}
.sources { margin: 4px 0; }
.source-list { margin-top: 4px; padding-left: 4px; border-left: 2px solid var(--line); }
.src { margin: 2px 0; }
button.link {
  background: none; border: none; padding: 0;
  color: var(--fg-dim); text-decoration: underline; cursor: pointer;
}
.actions { margin-top: 4px; }
</style>
