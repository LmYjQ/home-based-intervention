<script setup>
// 今天做哪条 + 怎么退路。30-45s（教练两次串行 LLM），走 job + SSE。
import { ref } from 'vue'
import { runJob } from '../api.js'
import JobProgress from './JobProgress.vue'
import AgentTrace from './AgentTrace.vue'

const props = defineProps({ childId: String, date: String, activityId: String })
const emit = defineEmits(['done'])

const steps = ref([])
const running = ref(false)
const out = ref(null)
const err = ref('')

async function load() {
  running.value = true; steps.value = []; err.value = ''
  try {
    const r = await runJob('/api/card', {
      child_id: props.childId, date: props.date,
      activity_id: props.activityId || null,
    }, (d) => steps.value.push(d))
    out.value = r.result
    emit('done', out.value)
  } catch (e) { err.value = e.message } finally { running.value = false }
}
defineExpose({ load })
</script>

<template>
  <section class="card">
    <div class="head">
      <h2>今天的家长卡片</h2>
      <button class="mini" :disabled="running" @click="load">
        {{ running ? '生成中…' : out ? '重新生成' : '生成今天的卡片' }}
      </button>
    </div>
    <JobProgress :steps="steps" :done="!running && steps.length > 0" />
    <p v-if="err" class="err">{{ err }}</p>

    <div v-if="out?.card">
      <div class="title">
        <span class="id">{{ out.card.activity_id }}</span>
        <b>{{ out.card.title }}</b>
        <span class="dim">{{ out.card.total_minutes }} 分钟</span>
        <span v-if="out.degraded" class="warn">降级卡片（模型不可用，用活动定义直接生成）</span>
      </div>

      <p class="why"><b>为什么今天给他做这个：</b>{{ out.card.why_today }}</p>
      <p class="why"><span class="dim">检索理由：{{ out.why }}</span></p>
      <p v-if="out.adjustment" class="adjust">★ 调整：{{ out.adjustment }}</p>

      <div v-if="out.analysis?.signals?.length" class="signals">
        <div class="sh">教练诊断（推理链可校验）</div>
        <div v-for="(s, i) in out.analysis.signals" :key="i" class="signal" :class="{ void: s.kind === 'none' }">
          <span class="tag">{{ s.kind }}</span>{{ s.interpretation }}
          <span class="imp">→ {{ s.impact }}</span>
          <span class="fn">功能 {{ s.function }}</span>
          <div class="quote">引文：「{{ s.evidence_quote }}」</div>
        </div>
        <p v-if="out.unverified_quotes?.length" class="warn">
          ★ {{ out.unverified_quotes.length }} 条引文未通过原文核对，已作废（仍显示，
          让人看得见模型"想说什么"，而不是假装它没说过）
        </p>
      </div>

      <ol class="steps">
        <li v-for="s in out.card.steps" :key="s.step_id">
          <b>{{ s.step_id }}.</b> {{ s.do }} → <i>{{ s.say ? `说“${s.say}”` : '不说话' }}</i>，等 {{ s.wait_sec }} 秒
          <div class="ok">做到了：{{ s.if_ok }}</div>
          <div class="no">没做到：{{ s.if_not }}</div>
        </li>
      </ol>

      <div v-if="out.card.prompt_ladder?.length" class="ladder">
        <b>提示阶梯</b>（从轻到重 —— 家长在这上面点"用到第几级"）
        <ol><li v-for="(p, i) in out.card.prompt_ladder" :key="i">{{ p }}</li></ol>
      </div>

      <div class="refuse">
        <b>★ 如果他不配合：</b>
        <div>先认：{{ out.card.if_child_refuses.recognize }}</div>
        <div>降要求：{{ out.card.if_child_refuses.reduce_demand }}</div>
        <div>给他选：{{ (out.card.if_child_refuses.choices || []).join(' / ') }}</div>
        <div class="abandon">什么时候放弃：{{ out.card.if_child_refuses.abandon_signal }}</div>
      </div>

      <div class="check">
        <b>怎么算成功：</b>{{ (out.card.success_check || []).join('、') }}
      </div>

      <AgentTrace :traces="out.traces" />
    </div>
  </section>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 18px; margin-bottom: 18px; }
.head { display: flex; align-items: center; gap: 12px; }
h2 { margin: 0; font-size: 17px; }
.title { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin: 14px 0 8px; font-size: 16px; }
.id { font-family: ui-monospace, monospace; font-size: 12px; color: #6b7280; }
.dim { color: #888; font-size: 12px; }
.warn { color: #b45309; font-size: 12px; }
.why { font-size: 14px; margin: 6px 0; }
.adjust { font-size: 13px; background: #eff6ff; border-left: 3px solid #3b82f6; padding: 6px 10px; }
.signals { background: #f9fafb; border-radius: 8px; padding: 10px 14px; margin: 12px 0; font-size: 13px; }
.sh { color: #6b7280; font-size: 12px; margin-bottom: 6px; }
.signal { padding: 4px 0; }
.signal.void { opacity: .5; text-decoration: line-through; }
.tag { display: inline-block; font-size: 11px; background: #e5e7eb; border-radius: 10px;
  padding: 1px 8px; margin-right: 7px; }
.imp { color: #2563eb; }
.fn { color: #9ca3af; font-size: 12px; margin-left: 8px; }
.quote { color: #9ca3af; font-size: 12px; }
.steps { padding-left: 22px; font-size: 14px; }
.steps li { margin-bottom: 10px; }
.ok { color: #15803d; font-size: 13px; }
.no { color: #b45309; font-size: 13px; }
.ladder { font-size: 13px; background: #f9fafb; padding: 8px 12px; border-radius: 6px; margin: 10px 0; }
.ladder ol { margin: 4px 0 0; padding-left: 20px; }
.refuse { background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px;
  padding: 10px 14px; font-size: 13px; margin: 12px 0; }
.abandon { color: #b45309; }
.check { font-size: 14px; }
.err { color: #dc2626; font-size: 13px; }
button.mini { background: #2563eb; color: #fff; border: 0; border-radius: 6px;
  padding: 6px 14px; font-size: 13px; cursor: pointer; }
button.mini:disabled { background: #9ca3af; }
</style>
