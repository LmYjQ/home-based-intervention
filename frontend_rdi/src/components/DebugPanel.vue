<script setup>
// 调试面板（计划 §12.5）。
//
// **这是调试工具，不是产品界面**：显示的是 checkpoint 摘要和节点走向，
// 不显示儿童原文、记忆内容或完整回答（后端已经脱敏，前端也不补）。
//
// 分叉的语义：从某个历史 checkpoint 另起一条**独立线程**，
// 改一个变量再跑一遍看输出怎么变。原会话的 checkpoint 和业务表都不动。
import { ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  conversationId: { type: String, default: '' },
})

const checkpoints = ref([])
const current = ref(null)
const selected = ref(null)
const patchText = ref('{}')
const forkResult = ref(null)
const busy = ref(false)
const error = ref('')

async function refresh() {
  if (!props.conversationId) return
  busy.value = true
  error.value = ''
  try {
    const [state, history] = await Promise.all([
      api.debugState(props.conversationId),
      api.debugCheckpoints(props.conversationId),
    ])
    current.value = state
    checkpoints.value = history.checkpoints || []
    if (!selected.value || !checkpoints.value.some((c) => c.checkpoint_id === selected.value)) {
      selected.value = checkpoints.value.find((c) => c.resumable)?.checkpoint_id || ''
    }
  } catch (e) {
    error.value = e.message
    checkpoints.value = []
    current.value = null
  } finally {
    busy.value = false
  }
}

function choose(item) {
  selected.value = item.checkpoint_id
  // 默认只改 case_summary —— 改整份 state 会把证据包也换掉，
  // 分叉出来的结果就没有对照意义了。
  patchText.value = JSON.stringify(
    { case_summary: { observed_facts: [] } },
    null,
    2,
  )
}

async function fork() {
  if (!props.conversationId || !selected.value) return
  busy.value = true
  error.value = ''
  forkResult.value = null
  try {
    let patch = {}
    if (patchText.value.trim()) patch = JSON.parse(patchText.value)
    forkResult.value = await api.debugFork(props.conversationId, {
      checkpoint_id: selected.value,
      values_patch: patch,
      run: true,
    })
  } catch (e) {
    error.value = e instanceof SyntaxError ? `values_patch 不是合法 JSON：${e.message}` : e.message
  } finally {
    busy.value = false
  }
}

watch(
  () => props.conversationId,
  () => {
    selected.value = ''
    forkResult.value = null
    current.value = null
    checkpoints.value = []
    refresh()
  },
  { immediate: true },
)
</script>

<template>
  <details class="card" v-if="conversationId">
    <summary>调试：checkpoint · 阶段 5</summary>
    <div class="row">
      <button class="small" :disabled="busy" @click="refresh">
        {{ busy ? '读取中…' : '刷新' }}
      </button>
    </div>

    <div class="err-box" v-if="error">{{ error }}</div>

    <div v-if="current" class="small" style="margin-bottom:8px">
      <span class="badge" :class="current.has_pending_interrupt ? 'warn' : 'ok'">
        {{ current.status }}
      </span>
      <span class="muted">
        next = {{ current.next?.join(' → ') || '（已结束）' }}
        · step {{ current.step }}
      </span>
    </div>

    <div class="small muted" v-if="!checkpoints.length && !busy">还没有 checkpoint。</div>

    <div
      v-for="c in checkpoints"
      :key="c.checkpoint_id"
      class="debug-row"
      :class="{ selected: c.checkpoint_id === selected }"
      @click="choose(c)"
    >
      <div>
        <strong>step {{ c.step }}</strong>
        <span class="muted"> · {{ c.next?.join(' → ') || 'END' }}</span>
        <span v-if="c.has_pending_interrupt" class="badge warn small">停在中断</span>
      </div>
      <div class="small muted">
        {{ c.entrypoint || '-' }} · {{ c.turn_id || '-' }}
        <span v-if="c.plan_ids?.length"> · 方案 {{ c.plan_ids.join('、') }}</span>
      </div>
    </div>

    <div class="field" v-if="selected">
      <label>从选中的 checkpoint 分叉（values_patch，JSON）</label>
      <textarea v-model="patchText" rows="6" spellcheck="false" />
      <div class="small muted">
        分叉落在独立 thread，原会话不动；调试分支不写业务表。
      </div>
      <button class="primary" :disabled="busy" @click="fork">
        {{ busy ? '分叉中…' : '分叉并运行' }}
      </button>
    </div>

    <div class="field" v-if="forkResult">
      <label>分叉结果</label>
      <div class="small">
        新线程 <code>{{ forkResult.thread_id }}</code>
        （as_node = {{ forkResult.as_node }}）
      </div>
      <div class="small">
        状态：{{ forkResult.state?.status }}
        · next = {{ forkResult.state?.next?.join(' → ') || '（已结束）' }}
        <span v-if="forkResult.state?.writes_skipped?.length" class="badge warn small">
          跳过写入：{{ forkResult.state.writes_skipped.join('、') }}
        </span>
      </div>
      <div class="small muted" v-if="forkResult.interrupt">
        停在中断：{{ forkResult.interrupt.kind }}
      </div>
      <pre v-if="forkResult.message">{{ forkResult.message }}</pre>
    </div>
  </details>
</template>

<style scoped>
.debug-row {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 8px 10px;
  margin-bottom: 8px;
  cursor: pointer;
}
.debug-row:hover { border-color: var(--color-brand); }
.debug-row.selected {
  border-color: var(--color-info);
  background: var(--color-info-soft);
}
</style>
