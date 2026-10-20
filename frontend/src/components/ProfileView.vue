<script setup>
// 建档之后的画像本体。
//
// ★ 为什么它必须存在：`/api/state` 原先只给 levels（技能名→等级），
//   前端顶栏就只能显示一个可信度数字。而每条结论的**证据引文**、
//   来源是报告明说的还是模型推断的、置信度多少 —— 这些才是建档最该看的东西，
//   它们全在 assessments 里，却没有出口。
//
//   （`ProfileForm` 里本来有一份画像明细，但它在建档完成后被 `v-if` 卸载了，
//   于是家长建完档反而看不到建了什么。）
//
// ★ 展示口径：
//   - `source=report_stated` 是报告里明说的；`inferred` 是模型推断 ——
//     两者用不同颜色标出，因为**推断不能当升级依据**（见 profile.py 的钳制规则）。
//   - 置信度 <0.4 的一律降级成 0.35，所以看到 0.10/0.35 是正常的，
//     不代表模型"很确定地认为他什么都不会"。
// ★ 必须显式 import：`<script setup>` **不会**自动导入 vue 的 API。
//   漏了这行编译期不报错、vite build 也照样通过，只在浏览器里渲染时
//   才抛 "computed is not defined" —— 表现为整页白屏。
//   同项目 App.vue / ProgressView.vue 都有这一行。
import { computed } from 'vue'

const props = defineProps({ state: { type: Object, default: null } })

const rows = computed(() => {
  const list = props.state?.assessments || []
  const cn = props.state?.domain_cn || {}
  const byDomain = {}
  for (const a of list) {
    const d = a.domain || ''
    ;(byDomain[d] = byDomain[d] || []).push(a)
  }
  // 优先能区排前面，其余按能区固定顺序，跟家长看报告的习惯一致。
  const pri = props.state?.priority_domains || []
  const rest = Object.keys(byDomain).filter(d => !pri.includes(d))
  return [...pri, ...rest].filter(d => byDomain[d])
    .map(d => ({ domain: d, name: cn[d] || d, items: byDomain[d] }))
})

const pc = computed(() => props.state?.constraints || {})
const nStated = computed(() =>
  (props.state?.assessments || []).filter(a => a.source === 'report_stated').length)
</script>

<template>
  <section v-if="state?.has_profile" class="card">
    <div class="head">
      <h2>能力画像</h2>
      <span class="dim">
        共 {{ state.assessments?.length || 0 }} 条 ·
        报告明说 {{ nStated }} 条 · 模型推断 {{ (state.assessments?.length || 0) - nStated }} 条
        <template v-if="state.profile_report_date"> · 报告日期 {{ state.profile_report_date }}</template>
      </span>
    </div>

    <p class="hint">
      「报告明说」= 报告原文里逐字引用的证据；「推断」= 报告没针对这个细项明确描述，
      由模型推的。<b>推断项不能充当能力升级的依据</b>，所以它置信度低、也不推动状态机。
    </p>

    <div v-for="g in rows" :key="g.domain" class="group">
      <h3>
        {{ g.name }}
        <span v-if="state.priority_domains?.includes(g.domain)" class="pri">优先</span>
      </h3>
      <table>
        <tr v-for="a in g.items" :key="a.skill_item">
          <td class="item">{{ a.skill_item }}</td>
          <td class="num">L{{ a.level }}</td>
          <td class="num conf" :class="{ low: a.confidence < 0.4 }">
            {{ a.confidence?.toFixed(2) ?? '—' }}
          </td>
          <td class="src" :class="a.source">
            {{ a.source === 'report_stated' ? '报告明说' : '推断' }}
          </td>
          <td class="quote">{{ a.evidence_quote || '（报告未明确描述）' }}</td>
        </tr>
      </table>
    </div>

    <details>
      <summary>家长约束（检索器硬过滤用的就是这三条）</summary>
      <ul class="pc">
        <li>每天可投入 <b>{{ pc.daily_minutes_available }}</b> 分钟</li>
        <li>家里现成的材料：{{ (pc.materials_at_home || []).join('、') || '（没填）' }}</li>
        <li>独自进行：{{ pc.alone_only ? '是' : '否' }}</li>
        <li>近期感官负荷高：{{ pc.avoid_load_sensory ? '是（避开高感官活动）' : '否' }}</li>
      </ul>
    </details>
  </section>
</template>

<style scoped>
.card { background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:18px; margin-bottom:18px; }
.head { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:6px; }
h2 { font-size:17px; margin:0; }
h3 { font-size:13px; color:#374151; margin:14px 0 4px; }
.dim { color:#888; font-size:12px; }
.hint { font-size:12px; color:#666; background:#f9fafb; border:1px solid #eef2f7;
  border-radius:6px; padding:8px 10px; margin:8px 0; }
table { border-collapse:collapse; width:100%; font-size:13px; }
td { padding:4px 8px; border-bottom:1px solid #f3f4f6; vertical-align:top; }
.item { font-weight:600; white-space:nowrap; }
.num { text-align:right; color:#6b7280; font-variant-numeric:tabular-nums; white-space:nowrap; }
.conf.low { color:#b45309; }
.src { white-space:nowrap; font-size:12px; }
.src.report_stated { color:#15803d; }
.src.inferred { color:#b45309; }
.quote { color:#6b7280; font-size:12px; }
.pri { font-size:11px; background:#dbeafe; color:#1d4ed8;
  border-radius:4px; padding:1px 6px; margin-left:6px; }
.pc { font-size:13px; color:#555; margin:6px 0; padding-left:18px; }
summary { cursor:pointer; font-size:13px; color:#555; margin-top:10px; }
</style>
