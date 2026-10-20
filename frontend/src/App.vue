<script setup>
// 三页外壳。**页面按使用频率与使用逻辑切，不是按组件切。**
//
//   建档：一次性动作 —— 建档 + 看画像（证据引文、来源、置信度）
//   今天：每天的主流程 —— 一整天的安排 → 逐条反馈 → 自动收尾
//   进度：跨天的事 —— 状态机与日结
//
// ★ 为什么砍掉了「候选」页：
//   它本来是"从候选表里挑几条加进今天"的暂存区。而现在一次就把当天
//   装箱满（Python 贪心，零 token），家长看到的就是当天该做的全部 ——
//   还需要自己去候选表里挑，说明系统没把"今天该做什么"这件事做完。
//   候选的可解释性（为什么推它 / 为什么没推）没有丢：
//   每条活动卡片上就带着"为什么推它"，没推荐的走 /api/plan 查。
//   单独开一页反而制造了一个决策点，让家长以为选错了有代价。
//
// ★ 跨页数据归 App 层（composables/useShared.js），页面只是它的投影。
//   当天安排只有一份：后端生成一次并落库，页面绝不自己再拼一份 ——
//   两份不一致的话，"显示 N 条"和"提交 N 条"会对不上，
//   而 sessions 记的是 activity_id，对不上的后果是记到错误的活动头上。
import { ref, onMounted } from 'vue'
import { api } from './api.js'
import { useShared } from './composables/useShared.js'
import ProfilePage from './pages/ProfilePage.vue'
import TodayPage from './pages/TodayPage.vue'
import ProgressView from './components/ProgressView.vue'

const { CHILD, state, day, today, loadState, loadDay, invalidateDay }
  = useShared()
const setPlan = (p) => { day.value = p }

const TABS = [
  { k: 'profile', label: '建档' },
  { k: 'today', label: '今天' },
  { k: 'progress', label: '进度' },
]
// ★ 标签写进 URL hash：刷新/前进后退保持在同一页，也能把某一页直接发给别人。
const VALID = ['profile', 'today', 'progress']
const fromHash = () => {
  const k = (location.hash || '').replace('#', '')
  return VALID.includes(k) ? k : null
}
const tab = ref(fromHash() || 'today')
function go(k) {
  tab.value = k
  if (location.hash !== '#' + k) location.hash = k
}
window.addEventListener('hashchange', () => {
  const k = fromHash()
  if (k && k !== tab.value) tab.value = k
})
const noLlm = ref(true)

// 建档之前，"今天/进度"两页一律不挂载。
// 它们每个都会发一次注定 400 的请求（"还没有这个孩子的画像"），
// 在 console 里留一串噪音，而且界面上看着像可以用。
const ready = () => !!state.value?.has_profile

async function boot() {
  await loadState()
  noLlm.value = state.value.no_llm
}

async function toggleNoLlm() {
  const r = await api.setNoLlm(!noLlm.value)
  noLlm.value = r.no_llm
}

async function onProfileDone() {
  await boot()
  // 重新建档后画像变了 —— 之前生成的当天安排是按旧画像排的，必须作废。
  // 后端也会比对 profile_id 自己判，但前端手里那份得先扔掉，
  // 否则页面会先渲染出旧的那一份再被新的替换（家长会看到内容闪一下）。
  invalidateDay()
  go('today')      // 建完档直接进主流程，不用再点一次
}

onMounted(boot)
</script>

<template>
  <div class="wrap">
    <header>
      <h1>家长干预教练</h1>
      <div class="ctrl">
        <label>日期 <input type="date" v-model="today" /></label>
        <label class="chk">
          <input type="checkbox" :checked="noLlm" @change="toggleNoLlm" />
          离线模式（不调模型，0.0s）
        </label>
        <span class="mode" v-if="state">DEMO_MODE={{ state.llm_mode }}</span>
        <button class="mini" @click="boot">刷新</button>
      </div>
    </header>

    <nav class="tabs">
      <button v-for="t in TABS" :key="t.k"
              :class="[t.k, { on: tab === t.k }]"
              :disabled="t.k !== 'profile' && !ready()"
              @click="go(t.k)">{{ t.label }}</button>
      <span v-if="!ready()" class="dim">先建档</span>
    </nav>

    <p v-if="state?.no_llm && tab === 'today'" class="banner">
      当前离线：卡片用活动定义直接生成，事件抽取一条都不编，日结只报计数事实。
      下面看到的一切仍然是真的 —— sessions、状态机输入、检索记忆切片都照常写库。
    </p>

    <ProfilePage v-if="tab === 'profile'" :CHILD="CHILD" :state="state" @done="onProfileDone" />

    <template v-else-if="ready()">
      <TodayPage v-if="tab === 'today'" :CHILD="CHILD" :date="today"
                 :has-profile="ready()" :plan="day" :set-plan="setPlan"
                 @refresh-state="loadState()" />

      <ProgressView v-else :child-id="CHILD" />
    </template>
  </div>
</template>

<style>
* { box-sizing: border-box; }
body { margin: 0; background: #f6f7f9; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; color: #111; }
.wrap { max-width: 900px; margin: 0 auto; padding: 20px 16px 60px; }
header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
h1 { font-size: 20px; margin: 0; }
.ctrl { display: flex; align-items: center; gap: 14px; font-size: 13px; flex-wrap: wrap; }
.ctrl .chk { display: flex; align-items: center; gap: 5px; }
.mode { color: #9ca3af; font-size: 12px; }

/* 标签栏 */
.tabs { display: flex; align-items: center; gap: 6px; margin-bottom: 16px;
  border-bottom: 1px solid #e5e7eb; padding-bottom: 8px; }
.tabs button { border: 1px solid transparent; background: transparent; color: #6b7280;
  padding: 6px 16px; font-size: 14px; border-radius: 6px; cursor: pointer; }
.tabs button:hover:not(:disabled) { background: #f3f4f6; }
.tabs button.on { background: #2563eb; color: #fff; font-weight: 600; }
.tabs button:disabled { color: #c3c7ce; cursor: not-allowed; }
.tabs .dim { margin-left: auto; }

.banner { background:#eff6ff; border:1px solid #bfdbfe; color:#1e40af;
  font-size:13px; padding:8px 12px; border-radius:8px; margin:0 0 14px; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 18px; margin-bottom: 18px; }
.sec { font-size: 15px; margin: 22px 0 8px; color: #374151; }
.hint { font-size: 12px; color: #888; margin: 0 0 12px; }
.warn { color: #b45309; }
.dim { color: #888; font-size: 12px; }
.err { color: #dc2626; font-size: 13px; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; white-space: pre-wrap;
  background: #f9fafb; padding: 10px 12px; border-radius: 6px; margin: 4px 0; }
h4 { font-size: 13px; margin: 12px 0 4px; color: #374151; }
button.mini { background: #f3f4f6; border: 1px solid #d1d5db; color: #374151;
  border-radius: 5px; padding: 4px 11px; font-size: 12px; cursor: pointer; }
code { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
input[type=date] { padding: 4px 6px; border: 1px solid #d1d5db; border-radius: 5px; }
</style>
