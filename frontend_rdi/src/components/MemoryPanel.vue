<script setup>
// 长期记忆面板（阶段 3）。
//
// **这个面板存在的意义**：计划 §10.7 要求"家长可以查看、纠正、删除记忆"。
// 光有接口不算 —— 家长得能看见"系统到底记住了我孩子什么"，
// 而且要能看出**哪条是家长说的、哪条是系统猜的**。
//
// 所以每条都显示：
//   - 原话（source.quote）—— 家长能对着看"我是不是这么说过"
//   - 状态（已确认 / 待确认 / 被削弱 / 已作废）
//   - 来源（哪一轮说的）
//
// 推断出来的假设单独标一个"系统推测"，不跟事实混在一起 ——
// 混在一起的话，家长会以为系统已经认定了他孩子是什么样。
import { computed, ref } from 'vue'

const props = defineProps({
  memories: { type: Array, default: () => [] },
  pending: { type: Number, default: 0 },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'correct', 'delete'])

const KIND_LABEL = {
  fact: '事实',
  preference: '偏好',
  observation: '观察',
  hypothesis: '假设',
  attempt: '尝试',
}

const KIND_HINT = {
  fact: '家长说过、确认过的稳定信息',
  preference: '家长希望的 / 不希望的做法',
  observation: '某一次具体发生的事，不是孩子的特点',
  hypothesis: '系统根据几次观察猜的，还没验证',
  attempt: '系统给过、家长选过的方案（只读）',
}

const ORDER = ['fact', 'preference', 'observation', 'hypothesis', 'attempt']

const groups = computed(() =>
  ORDER.map((kind) => ({
    kind,
    label: KIND_LABEL[kind],
    hint: KIND_HINT[kind],
    items: props.memories.filter((m) => m.kind === kind),
  })).filter((g) => g.items.length),
)

// 行内编辑。一次只改一条 —— 同时开好几个输入框，家长自己也分不清在改哪条。
const editingId = ref('')
const draft = ref('')

const STATUS_LABEL = {
  confirmed: '已确认',
  active: '已确认',
  pending_confirmation: '待确认',
  candidate: '待验证',
  weakened: '被削弱',
  superseded: '已作废',
  deleted: '已删除',
}

function statusClass(item) {
  if (['confirmed', 'active'].includes(item.status)) return 'ok'
  if (['pending_confirmation', 'candidate', 'weakened'].includes(item.status)) return 'warn'
  return ''
}

function canConfirm(item) {
  return ['pending_confirmation', 'candidate'].includes(item.status)
}

function startEdit(item) {
  editingId.value = item.memory_id
  draft.value = item.content
}

function cancelEdit() {
  editingId.value = ''
  draft.value = ''
}

function saveEdit(item) {
  const text = draft.value.trim()
  if (!text) return
  emit('correct', { memory: item, content: text })
  cancelEdit()
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
      <span v-if="pending" class="badge warn">{{ pending }} 条待确认</span>
    </header>

    <div v-if="!memories.length" class="small muted">
      这个孩子还没有任何长期记忆。说几轮之后，系统会把稳定信息记下来。
    </div>

    <section v-for="g in groups" :key="g.kind">
      <h4>{{ g.label }} <span class="small muted">· {{ g.hint }}</span></h4>

      <div v-for="m in g.items" :key="m.memory_id" class="mem">
        <div class="row">
          <span class="badge" :class="statusClass(m)">{{ STATUS_LABEL[m.status] || m.status }}</span>
          <span v-if="m.kind === 'hypothesis'" class="badge">系统推测</span>
          <span v-if="m.is_attempt" class="badge">家长试过</span>
          <span v-if="m.contradicting_observation_ids?.length" class="badge warn">
            有 {{ m.contradicting_observation_ids.length }} 条反例
          </span>
        </div>

        <div v-if="editingId === m.memory_id" class="edit">
          <textarea v-model="draft" rows="3"></textarea>
          <div class="row">
            <button class="small primary" :disabled="busy || !draft.trim()" @click="saveEdit(m)">
              保存
            </button>
            <button class="small" @click="cancelEdit">取消</button>
          </div>
        </div>
        <div v-else class="content">{{ m.content }}</div>

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

        <!-- 尝试是 trials 的只读投影，不让改 —— 改了就和审计对不上 -->
        <div v-if="m.kind !== 'attempt' && editingId !== m.memory_id" class="row actions">
          <button v-if="canConfirm(m)" class="small" :disabled="busy" @click="emit('confirm', m)">
            确认
          </button>
          <button class="small" :disabled="busy" @click="startEdit(m)">纠正</button>
          <button class="small" :disabled="busy" @click="askDelete(m)">删除</button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
section { margin-top: 10px; }
h4 { margin: 10px 0 4px; font-size: 12px; color: var(--fg-dim); font-weight: 600; }
.mem { border-top: 1px solid var(--line); padding: 8px 0; }
.mem:first-of-type { border-top: none; }
.content { margin: 4px 0; }
.quote {
  margin: 4px 0; padding: 4px 8px;
  border-left: 2px solid var(--line);
  color: var(--fg-dim); font-size: 12px;
}
.edit textarea { width: 100%; margin: 4px 0; }
.actions { margin-top: 4px; }
</style>
