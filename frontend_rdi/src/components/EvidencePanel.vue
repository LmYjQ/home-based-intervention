<script setup>
// 证据面板（计划 §9.3）。
//
// **故意不显示 source_excerpt** —— 后端根本不返回它。
// may_quote_to_user 恒为 false（这本书授权未核实），
// 所以家长只能看到"依据的是哪几页的什么大意"，拿不到原文。
// 这个面板存在的意义就是让人核对"这个建议到底有没有依据"。
//
// **每条证据带序号**：建议卡上的引用会写"见右侧证据第 N 条"，
// 两边靠这个号对上。不带序号的话，家长只能自己在两堆文字里
// 找同一个 UUID —— 那不叫可核对，叫让人放弃核对。
import { computed } from 'vue'

const props = defineProps({
  evidence: { type: Array, default: () => [] },
  decision: { type: Object, default: () => ({}) },
  gaps: { type: Array, default: () => [] },
  // 被建议卡引用到的 evidence_id。命中的标出来。
  citedIds: { type: Array, default: () => [] },
})

const cited = computed(() => new Set(props.citedIds || []))
</script>

<template>
  <div class="card">
    <header>
      <h3>证据</h3>
      <span class="badge">{{ evidence.length }} 条</span>
      <span v-if="cited.size" class="badge accent">{{ cited.size }} 条被引用</span>
      <span v-if="decision.evidence_status" class="badge accent">
        {{ decision.evidence_status }}
      </span>
      <span
        v-if="decision.can_support_personalized_advice !== null
              && decision.can_support_personalized_advice !== undefined"
        class="badge"
        :class="decision.can_support_personalized_advice ? 'ok' : 'warn'"
      >
        {{ decision.can_support_personalized_advice ? '可个性化' : '不可个性化' }}
      </span>
    </header>

    <div v-if="!evidence.length" class="small muted">本轮没有取到证据。</div>

    <div v-for="(e, i) in evidence" :key="e.evidence_id" class="ev" :class="{ used: cited.has(e.evidence_id) }">
      <div class="row">
        <span class="idx">{{ i + 1 }}</span>
        <strong>{{ e.title }}</strong>
        <span v-if="e.section_title" class="badge">{{ e.section_title }}</span>
        <span class="badge" :class="e.review_status === 'reviewed' ? 'ok' : ''">
          {{ e.review_status || 'unreviewed' }}
        </span>
        <span v-if="cited.has(e.evidence_id)" class="badge accent">已引用</span>
      </div>
      <div class="small muted">
        p.{{ e.pdf_page_start }}–{{ e.pdf_page_end }}
        <span v-if="e.similarity != null"> · 相似度 {{ Number(e.similarity).toFixed(3) }}</span>
      </div>
      <div v-if="e.summary" style="margin:5px 0">{{ e.summary }}</div>
      <div v-if="e.relevance_reason" class="small muted">召回理由：{{ e.relevance_reason }}</div>
      <div class="small muted">evidence_id: <code>{{ e.evidence_id }}</code></div>
      <div class="small muted">
        may_quote_to_user: <span :class="e.may_quote_to_user ? 'warn' : 'ok'">{{ e.may_quote_to_user }}</span>
      </div>
    </div>

    <template v-if="gaps.length">
      <h4 style="margin-top:12px">证据缺口</h4>
      <ul class="small">
        <li v-for="(g, i) in gaps" :key="i">{{ g }}</li>
      </ul>
    </template>

    <template v-if="decision.missing_information?.length">
      <h4 style="margin-top:12px">缺的信息</h4>
      <ul class="small">
        <li v-for="(g, i) in decision.missing_information" :key="i">{{ g }}</li>
      </ul>
    </template>

    <details v-if="decision && Object.keys(decision).length">
      <summary>evidence_decision 原始 JSON</summary>
      <pre>{{ JSON.stringify(decision, null, 2) }}</pre>
    </details>
  </div>
</template>

<style scoped>
.ev { border-top: 1px solid var(--line); padding: 9px 0; }
.ev:first-of-type { border-top: none; }
/* 被建议卡引用到的证据 —— 左边描一道边，一眼能认出"这几条真被用了"。 */
.ev.used { border-left: 2px solid var(--accent); padding-left: 9px; margin-left: -11px; }
.idx {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
  background: var(--panel-2); border: 1px solid var(--line);
  font-size: 11px; color: var(--fg-dim);
}
.ev.used .idx { background: #101c2e; border-color: #274570; color: var(--accent); }
ul { margin: 4px 0 0; padding-left: 18px; }
code { font-size: 11px; }
</style>
