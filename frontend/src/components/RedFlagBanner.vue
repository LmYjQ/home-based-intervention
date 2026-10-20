<script setup>
// 红旗横幅（Phase 2）。**置顶常驻**，不是"提交完飘一次"。
//
// ★ 为什么不复用 RedFlagList.vue：它吃的是一次扫描结果（report.hits[]），
//   渲染完随组件卸载就没了 —— 那正是 Phase 2 要修的毛病。
//   这里吃的是**已经落库的行**（有 id / 日期 / 状态），还要按状态给按钮。
//   两个形状硬塞进一个组件，就会得到一个 props 没法校验的组件。
//
// ★ 父组件不传 paused 就算法，只传后端返回的那个 ——
//   前端自己从 flags 推会出现第二套口径，而打架的后果是
//   "横幅说暂停着、计划照生成"。见 store.plan_paused。
import { computed } from 'vue'

const props = defineProps({
  flags: { type: Array, default: () => [] },
  paused: { type: Boolean, default: false },
  busy: { type: Number, default: 0 },   // 正在改状态的行 id
})
const emit = defineEmits(['set-status'])

// 未关闭的排前面 —— 与后端 list_red_flags 的 ORDER BY 同一个口径，
// 但这里再排一次是为了让"刚提交完的那条"一定出现在最上面。
const open = computed(() => props.flags.filter(f => f.status !== 'closed'))
const done = computed(() => props.flags.filter(f => f.status === 'closed'))
const anyEmergency = computed(() => open.value.some(f => f.referral_emergency))
</script>

<template>
  <section v-if="flags.length" class="banner" :class="{ emergency: anyEmergency }">
    <div class="head">
      <b>⚠ 需要你处理的事</b>
      <span v-if="paused" class="paused">
        计划生成已暂停 —— 直到下面每一条都被标为「已处理」
      </span>
      <span v-else class="ok">当前没有暂停中的红旗</span>
    </div>

    <article v-for="f in open" :key="f.id" class="flag" :class="f.severity">
      <div class="line1">
        <span class="tag">{{ f.severity }}</span>
        <b>{{ f.flags_group }}</b>（{{ f.code }}）· {{ f.observed_at }}
        <span class="dim">· 来自{{ f.source_kind === 'report' ? '评估报告' : '当天观察' }}</span>
        <span v-if="f.status === 'acknowledged'" class="seen">已看到</span>
      </div>
      <div class="quote">你说的：{{ f.quote }}</div>
      <div class="ask">要不要补充一下：{{ f.ask }}</div>

      <!-- 转诊指引逐条给，不揉成一段（后端 guidance_for 的形状）。
           三个字段对应家长真正会问的三个问题，少一个就答不上来；
           「不要做什么」是防害的那一个，不是可选项。 -->
      <dl class="guide">
        <dt>找谁</dt><dd>{{ f.referral_who }}</dd>
        <dt>做什么</dt><dd>{{ f.referral_do }}</dd>
        <dt v-if="f.referral_avoid" class="avoid">不要做</dt>
        <dd v-if="f.referral_avoid" class="avoid">{{ f.referral_avoid }}</dd>
        <template v-if="f.referral_emergency">
          <dt class="emg">急诊红线</dt><dd class="emg">{{ f.referral_emergency }}</dd>
        </template>
      </dl>

      <div class="acts">
        <button class="mini" :disabled="busy === f.id || f.status === 'acknowledged'"
                @click="emit('set-status', { id: f.id, status: 'acknowledged' })">
          {{ f.status === 'acknowledged' ? '已看到' : '标为已看到' }}
        </button>
        <button class="mini danger" :disabled="busy === f.id"
                @click="emit('set-status', { id: f.id, status: 'closed' })">
          标为已处理
        </button>
      </div>
    </article>

    <details v-if="done.length" class="history">
      <summary>已处理 {{ done.length }} 条（点开看看 —— 复诊时这些才是有用的）</summary>
      <article v-for="f in done" :key="f.id" class="flag closed">
        <div class="line1">
          <span class="tag">{{ f.severity }}</span>
          <b>{{ f.flags_group }}</b>（{{ f.code }}）· {{ f.observed_at }}
        </div>
        <div class="quote">你说的：{{ f.quote }}</div>
        <button class="mini" :disabled="busy === f.id"
                @click="emit('set-status', { id: f.id, status: 'open' })">
          重新打开
        </button>
      </article>
    </details>
  </section>
</template>

<style scoped>
.banner { background: #fff7ed; border: 2px solid #fdba74; border-radius: 10px;
  padding: 12px 16px; margin: 0 0 16px; }
.banner.emergency { background: #fef2f2; border-color: #dc2626; }
.head { font-size: 14px; margin-bottom: 8px; }
.paused { color: #b91c1c; font-weight: 600; margin-left: 8px; }
.ok { color: #16a34a; margin-left: 8px; }
.flag { font-size: 13px; padding: 8px 0; border-top: 1px solid #fde68a; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px;
  margin-right: 6px; color: #fff; background: #9ca3af; }
.urgent .tag { background: #dc2626; }
.high .tag { background: #ea580c; }
.watch .tag { background: #ca8a04; }
.urgent { font-weight: 600; }
.dim { color: #6b7280; font-weight: 400; }
.seen { color: #b45309; margin-left: 6px; }
.quote { color: #374151; margin: 4px 0 2px; }
.ask { color: #92400e; margin-bottom: 4px; }
.guide { display: grid; grid-template-columns: 4.5em 1fr; gap: 2px 8px;
  margin: 6px 0; color: #374151; }
.guide dt { color: #6b7280; }
.guide dd { margin: 0; }
.guide .avoid, .guide dd.avoid { color: #b91c1c; }
.guide .emg, .guide dd.emg { color: #b91c1c; font-weight: 600; }
.acts { margin-top: 6px; }
.acts button { margin-right: 8px; }
.history { margin-top: 8px; font-size: 13px; }
.history summary { cursor: pointer; color: #6b7280; }
.flag.closed { opacity: .65; }
button.danger { color: #b91c1c; }
</style>
