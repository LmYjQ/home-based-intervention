<script setup>
// 出生日期输入（建档页用）
// ========================
//
// ★ 为什么单独做成组件，而不是在 ProfileForm 里写个 <input type="date">
//   "格式准确"这件事需要**校验 + 提示 + 阻断提交**三件事同时做，
//   而它们是同一个判断的三种表现。散在表单里就会各写各的：
//   表单一处判"有没有填"，提交处另一处判"对不对"，后端第三处再判一次 ——
//   三处口径迟早漂移。
//
// ★ 这个组件保证的：
//   1. 出来的值**只可能是** `YYYY-MM-DD` 的真日期（非法值不往外 emit，
//      所以上层拿到的一定是能用的）。
//   2. 不能选未来日期（那不是"填错格式"，是"填错了"）。
//   3. 顺手把换算出来的月龄给家长看 —— 填完能立刻核对，
//      而"填错 6 个月 → 活动难度整体偏一级"是这套系统最贵的错。
//
// ★ 与后端的关系：这里的月龄**只用于显示**。真正参与判定的那一份
//   由后端 `asd_agent/child_age.py` 算 —— 前端不算业务数
//   （与 `api.js` 顶上"前端不算 quality"同源）。
import { computed, onMounted, ref, watch } from 'vue'
import { ageText, checkBirthDate, monthsOld, monthsAgoDate, todayISO } from '../childAge.js'

const props = defineProps({
  modelValue: { type: String, default: '' },
  // 默认给一个"看起来合理"的日期，省得家长对着空框发呆。
  defaultMonths: { type: Number, default: 34 },
})
const emit = defineEmits(['update:modelValue'])

const max = todayISO()
const err = ref('')

// 本地值：用户正在输入的原始字符串。
// ★ 不直接用 v-model 绑到外面的 modelValue —— 输入过程中它可能是半截的
//   （"2024-0"），而外层拿到半截值就等于拿到了一个"没填"的月份。
const local = ref(props.modelValue || monthsAgoDate(props.defaultMonths))
const touched = ref(false)

// ★ 上层一开始是空的（它不知道默认日期长什么样），所以默认值由**组件**
//   往上给 —— 两边各算一遍默认日期就会漂移（一个用今天、一个用建档那天）。
onMounted(() => {
  if (!props.modelValue) emit('update:modelValue', local.value)
})

watch(() => props.modelValue, (v) => {
  if (v !== local.value) { local.value = v || monthsAgoDate(props.defaultMonths); touched.value = true }
})

const months = computed(() => monthsOld(local.value))
const ageLine = computed(() => (months.value == null ? '' : `月龄 ${ageText(months.value)}`))

function onInput(e) {
  const v = e.target.value
  local.value = v
  touched.value = true
  const c = checkBirthDate(v, max)
  if (c.ok) {
    err.value = ''
    emit('update:modelValue', v)      // ★ 只在合法时往外 emit
  } else {
    err.value = c.reason
  }
}

function onBlur() {
  touched.value = true
  const c = checkBirthDate(local.value, max)
  if (!c.ok) {
    err.value = c.reason
    return
  }
  // 补齐成规范形式（有些浏览器会把 2024-3-5 存成 2024-03-05，
  // 但也有的原样留着）—— 外层拿到的一定是 YYYY-MM-DD。
  const y = c.date.getFullYear()
  const m = String(c.date.getMonth() + 1).padStart(2, '0')
  const d = String(c.date.getDate()).padStart(2, '0')
  local.value = `${y}-${m}-${d}`
  emit('update:modelValue', local.value)
  err.value = ''
}
</script>

<template>
  <div class="bd">
    <input type="date" :value="local" :max="max" @input="onInput" @blur="onBlur" />
    <span class="sub" v-if="ageLine">{{ ageLine }}</span>
    <span class="err" v-if="touched && err">{{ err }}</span>
  </div>
</template>

<style scoped>
.bd { }
input { width: 100%; box-sizing: border-box; padding: 6px 8px;
  border: 1px solid #d1d5db; border-radius: 6px; }
input:focus { outline: 2px solid #bfdbfe; border-color: #2563eb; }
.sub { display: block; font-size: 12px; color: #6b7280; margin-top: 3px; }
.err { display: block; font-size: 12px; color: #dc2626; margin-top: 3px; }
</style>
