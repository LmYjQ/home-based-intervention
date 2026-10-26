<script setup>
// 建档：贴评估报告 → 18-47s → 出画像 + 红旗。
// ★ 必须慢：这是阶段 2 定的方案（报告抽取 90s 上限，不是每日链路的 20s），
//   所以走 job + SSE，不在前端同步等。
import { ref } from 'vue'
import { api, runJob } from '../api.js'
import JobProgress from './JobProgress.vue'
import RedFlagList from './RedFlagList.vue'
import BirthDatePicker from './BirthDatePicker.vue'

const props = defineProps({ childId: String })
const emit = defineEmits(['done'])

const SAMPLE = `小宇，34个月，2026-09 由康复科评估。
大运动：能独立走平路，但上下楼梯需要扶，平衡能力接近同龄水平。
精细动作：能捏起小物件，但不会用勺子。
语言：能说"妈妈""要"，但不会主动表达需求，词汇量约 15 个。
认知：能指认身体部位，但不理解两步指令。
社会适应：见到人不会主动打招呼，共同注意建立困难。
行为方面：他去咬自己的袖子，需要提醒，情绪激动时会用手推人。`

const report = ref(SAMPLE)
// ★ 年龄由出生日期算，**不再让家长手填月龄**。
//   月龄是要人算的数，填错 6 个月就整体偏一级难度 —— 而偏一级在干预里
//   不是"稍微不合适"，是练不到或练过头。出生日期只有一个正确值。
const birthDate = ref('')
const minutes = ref(30)
const materials = ref('杯子,绘本,小汽车,积木')

const steps = ref([])
const running = ref(false)
const result = ref(null)
const err = ref('')

async function submit() {
  // ★ 没填出生日期就不发 —— 后端会 422，但让它走到那里就是让家长
  //   等一个必然失败的请求。组件已经把格式守住了，这里守"有没有填"。
  if (!birthDate.value) { err.value = '还没填出生日期'; return }
  running.value = true; steps.value = []; result.value = null; err.value = ''
  try {
    const r = await runJob('/api/profile', {
      child_id: props.childId,
      report_text: report.value,
      birth_date: birthDate.value,
      daily_minutes: Number(minutes.value),
      materials: materials.value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    }, (d) => steps.value.push(d))
    result.value = r.result
    emit('done')
  } catch (e) {
    err.value = e.message
  } finally {
    running.value = false
  }
}
</script>

<template>
  <section class="card">
    <h2>建档：评估报告 → 能力画像</h2>
    <p class="hint">
      真实报告要 18-47s（7B 出约 1100 token）。点开后走后台 job，
      下面的进度是模型每一步的回调，不是假进度条。
    </p>

    <label>评估报告原文</label>
    <textarea v-model="report" rows="10" placeholder="把报告贴进来"></textarea>

    <div class="row">
      <label>出生日期
        <BirthDatePicker v-model="birthDate" :default-months="34" />
      </label>
      <label>每天能挤出的时间（分钟）<input type="number" v-model="minutes" /></label>
      <label class="wide">家里现成的材料<input v-model="materials" placeholder="逗号分隔" /></label>
    </div>

    <button :disabled="running || !report.trim()" @click="submit">
      {{ running ? '正在建档…' : '开始建档' }}
    </button>

    <JobProgress :steps="steps" :done="!running && steps.length > 0" />
    <p v-if="err" class="err">{{ err }}</p>

    <div v-if="result">
      <RedFlagList :report="result.redflags" />
      <div class="meta">
        整体可信度 <b>{{ result.profile.overall_confidence?.toFixed(2) }}</b>
        <span v-if="result.profile.overall_confidence < 0.5" class="warn">
          —— 低于升级闸门 0.5，状态机会冻结全部升级（这是正确行为）
        </span>
        <span v-if="result.degraded" class="warn">· 走了降级路径，不是模型给的</span>
      </div>
      <details>
        <summary>画像明细（{{ result.profile.assessments?.length || 0 }} 条技能细项）</summary>
        <table>
          <tr v-for="a in result.profile.assessments" :key="a.skill_item">
            <td>{{ a.skill_item }}</td><td class="num">L{{ a.level }}</td>
            <td class="num">置信 {{ a.confidence?.toFixed(2) }}</td>
          </tr>
        </table>
      </details>
      <details v-if="Object.keys(result.unverified || {}).length">
        <summary>引文未通过原文核对，已降级为推断（{{ Object.keys(result.unverified).length }} 条）</summary>
        <ul><li v-for="(q, k) in result.unverified" :key="k">{{ k }}：{{ q }}</li></ul>
      </details>
    </div>
  </section>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
h2 { margin: 0 0 6px; font-size: 18px; }
.hint { color: #888; font-size: 13px; margin: 0 0 14px; }
label { display: block; font-size: 13px; color: #555; margin-bottom: 4px; }
textarea { width: 100%; box-sizing: border-box; font-family: inherit; font-size: 14px;
  padding: 10px; border: 1px solid #d1d5db; border-radius: 6px; }
.row { display: flex; gap: 16px; flex-wrap: wrap; margin: 12px 0; }
.row label { flex: 1; min-width: 140px; }
.row .wide { flex: 2; }
.sub { display: block; font-size: 12px; color: #6b7280; margin-top: 3px; }
input[type=number], input[type=text], input[type=date], input:not([type]) { width: 100%; box-sizing: border-box;
  padding: 6px 8px; border: 1px solid #d1d5db; border-radius: 6px; }
button { background: #2563eb; color: #fff; border: 0; padding: 9px 20px;
  border-radius: 6px; cursor: pointer; font-size: 14px; }
button:disabled { background: #9ca3af; cursor: not-allowed; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }
td { padding: 4px 8px; border-bottom: 1px solid #f3f4f6; }
.num { color: #888; text-align: right; }
.meta { margin: 12px 0; font-size: 13px; }
.warn { color: #b45309; }
.err { color: #dc2626; font-size: 13px; }
details { font-size: 13px; margin-top: 8px; }
summary { cursor: pointer; color: #555; }
</style>
