<script setup>
// 一张建议卡。
//
// 刻意把 application_label 显示出来：这一版的验收重点之一就是
// "引用校验有没有真的生效" —— 标了 source_summary 却拿不出对应证据的卡
// 应该被丢掉，这里能一眼看出来。
//
// **标签显示中文**：`source_summary` / `ai_adaptation` 是内部契约值，
// 直接显示给家长看是英文的机器名。家长需要看懂的是
// "这条是指南里说的" 还是 "这是我们根据指南做的应用转化" ——
// 这两件事必须让家长分得清，所以显示中文。
import { computed } from 'vue'

const props = defineProps({
  plan: { type: Object, required: true },
  selectable: { type: Boolean, default: false },
  selected: { type: Boolean, default: false },
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

    <section v-if="plan.explanation">
      <h4>说明</h4>
      <div>{{ plan.explanation }}</div>
    </section>

    <section v-if="plan.parent_actions?.length">
      <h4>家长可以做的事</h4>
      <div v-for="(a, i) in plan.parent_actions" :key="i" class="action">
        <div>{{ a.action }}</div>
        <div v-if="a.context" class="small muted">情境：{{ a.context }}</div>
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

    <section>
      <h4>引用（source_citations）</h4>
      <div v-if="!plan.source_citations?.length" class="small muted">（空）</div>
      <div v-for="(c, i) in plan.source_citations || []" :key="i" class="small cite">
        <div>{{ c.title }}<span v-if="c.chapter"> · {{ c.chapter }}</span></div>
        <div class="muted">
          p.{{ c.pdf_page_start }}–{{ c.pdf_page_end }} · {{
            c.source_type === 'guideline' ? '指南' : c.source_type
          }}
        </div>
        <div class="muted">source_id: <code>{{ c.source_id }}</code></div>
        <div>支撑：{{ c.claim_supported }}</div>
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
.action { margin-bottom: 8px; }
.cite { border-left: 2px solid var(--line); padding-left: 9px; margin-bottom: 7px; }
code { font-size: 11px; color: var(--fg-dim); }
</style>