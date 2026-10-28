<script setup>
// 选年月的日历控件。
//
// **为什么不用原生 `<input type="month">`**：Chrome 会弹月历，
// Firefox 会退化成两个下拉（甚至纯文本框），行为不统一；
// 原生 `<input type="date">` 又是按天的 —— 出生日期只精确到月，
// 让家长去挑一个"具体哪天"是假精度。
//
// 所以自己画一个：点开是年份 + 12 个月的格子，点哪个月就是哪个月。
// 没有引入日期库（这个项目只装了 vue）。
//
// 交互约定：
//   - 未来月份是灰的，点不了（出生日期不能晚于今天）
//   - 点外面 / 按 Esc 关闭
//   - 年份可以往回翻（往前翻到出生那一年），不能往前翻过今天
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  // 'YYYY-MM'，空串 = 还没选
  modelValue: { type: String, default: '' },
  // 上限，'YYYY-MM' 或 'YYYY-MM-DD' 都行
  max: { type: String, default: '' },
  placeholder: { type: String, default: '选择年月' },
})
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const root = ref(null)
// 日历当前显示哪一年
const viewYear = ref(new Date().getFullYear())

const MONTH_LABELS = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']

const now = new Date()
const currentYear = now.getFullYear()
const currentMonth = now.getMonth() + 1

// 上限的年 + 月
const maxYM = computed(() => {
  const text = (props.max || '').trim()
  if (!text) return null
  const [y, m] = text.split('-')
  const year = Number(y)
  const month = Number(m)
  if (!year || !month) return null
  return { year, month }
})

// 日历上能选的月份
const months = computed(() =>
  MONTH_LABELS.map((label, i) => ({
    label,
    month: i + 1,
    disabled: maxYM.value
      ? viewYear.value > maxYM.value.year ||
        (viewYear.value === maxYM.value.year && i + 1 > maxYM.value.month)
      : false,
  })),
)

const canPrevYear = computed(() => viewYear.value > 1900)
const canNextYear = computed(() =>
  maxYM.value ? viewYear.value < maxYM.value.year : viewYear.value < currentYear,
)

const displayText = computed(() => {
  if (!props.modelValue) return props.placeholder
  const [y, m] = props.modelValue.split('-')
  return `${y} 年 ${Number(m)} 月`
})

function toggle() {
  open.value = !open.value
  if (open.value) {
    // 打开时回到"当前值所在年"；没值就停在今年
    const y = props.modelValue ? Number(props.modelValue.split('-')[0]) : currentYear
    viewYear.value = y
  }
}

function prevYear() {
  if (canPrevYear.value) viewYear.value -= 1
}

function nextYear() {
  if (canNextYear.value) viewYear.value += 1
}

function pick(month) {
  if (maxYM.value) {
    const limit = maxYM.value
    if (viewYear.value > limit.year) return
    if (viewYear.value === limit.year && month > limit.month) return
  }
  emit('update:modelValue', `${viewYear.value}-${String(month).padStart(2, '0')}`)
  open.value = false
}

function onDocClick(e) {
  if (!root.value) return
  if (!root.value.contains(e.target)) open.value = false
}

function onKey(e) {
  if (e.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})

// 外部把值改了，日历显示的年份跟着走
watch(
  () => props.modelValue,
  (v) => {
    if (v) viewYear.value = Number(v.split('-')[0])
  },
)
</script>

<template>
  <div class="mp" ref="root">
    <button
      type="button"
      class="trigger"
      :class="{ empty: !modelValue }"
      @click="toggle"
      :aria-expanded="open"
    >
      <span>{{ displayText }}</span>
      <span class="caret">▾</span>
    </button>

    <div v-if="open" class="pop" role="dialog" aria-label="选择出生年月">
      <div class="yrow">
        <button type="button" class="nav" :disabled="!canPrevYear" @click="prevYear">◀</button>
        <span class="year">{{ viewYear }} 年</span>
        <button type="button" class="nav" :disabled="!canNextYear" @click="nextYear">▶</button>
      </div>

      <div class="grid">
        <button
          v-for="m in months"
          :key="m.month"
          type="button"
          class="cell"
          :class="{
            on: modelValue === `${viewYear}-${String(m.month).padStart(2, '0')}`,
            off: m.disabled,
          }"
          :disabled="m.disabled"
          @click="pick(m.month)"
        >
          {{ m.month }}月
        </button>
      </div>

      <div class="small muted" style="margin-top:6px">
        只精确到月。灰掉的月份是未来，不能选。
      </div>
    </div>
  </div>
</template>

<style scoped>
.mp { position: relative; }

/* 触发按钮长得像输入框 —— 免得一行里 input 和 button 看着不像一套 */
.trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 10px 12px;
  border-radius: var(--radius-button);
  border: 1px solid var(--color-border-strong);
  background: var(--color-surface);
  color: var(--color-text);
  font-size: 15px;
  text-align: left;
}
.trigger:hover { border-color: var(--color-brand); }
.trigger.empty { color: var(--color-text-muted); }
.caret { color: var(--color-text-secondary); font-size: 11px; }

.pop {
  position: absolute;
  z-index: 20;
  top: calc(100% + 4px);
  left: 0;
  width: 268px;
  padding: 14px;
  border-radius: var(--radius-panel);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  box-shadow: 0 10px 28px rgba(69, 73, 67, 0.12);
}

.yrow {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.nav { padding: 2px 10px; line-height: 1.2; }
.year { font-weight: 600; }

.grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
}
.cell {
  padding: 7px 0;
  font-size: 13px;
  text-align: center;
}
.cell.on {
  background: var(--color-brand-hover);
  border-color: var(--color-brand-hover);
  color: #ffffff;
  font-weight: 600;
}
</style>
