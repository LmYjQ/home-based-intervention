// 全局共享的只读状态：**只放跨页面都要用的东西。**
//
// ★ 为什么要有这一层：拆成多页之后，「今天的安排」和「进度」在不同页面，
//   但进度页要按 child 拉自己的数据、今天页要显示当天安排。
//   更重要的是**当天安排只有一份** —— 它由后端生成一次并落库
//   （`/api/card` 命中缓存就返回），所以前端绝对不能自己再拼一份：
//   两份数据一旦不一致，家长看到的"今天有 N 条"和实际提交的 N 条就对不上，
//   而 sessions 表记的是 activity_id，对不上的后果是记到错误的活动头上。
//
// 这里刻意**不缓存生成结果**：要不要重算由后端按 (日期, 画像版本) 决定，
// 前端只负责把拿到的结果放着，并且每次进页面问一次后端"今天这份还是原来那份吗"。
// 前端自己判断"我生成过了就别问了"，等于把缓存键从后端挪到前端 ——
// 而缓存键必须包含"画像有没有变"，前端根本不知道这件事。
import { ref } from 'vue'
import { api } from '../api.js'

export const CHILD = 'demo-001'

export function useShared() {
  const state = ref(null)       // /api/state
  const day = ref(null)         // /api/card 的结果：当天的一整份安排
  const today = new Date().toISOString().slice(0, 10)

  async function loadState() {
    state.value = await api.state(CHILD)
    return state.value
  }

  // 返回 `date` 那天的安排。命中后端缓存时是秒回（不再跑 LLM）。
  async function loadDay(date, { force = false } = {}) {
    const r = await api.card({ child_id: CHILD, date, force })
    day.value = r
    return r
  }

  // 反馈提交后调用。**重置当天安排** —— 不是"刷新"，是作废。
  //
  // ★ 为什么必须作废而不是留着：家长提交完今天的反馈之后，
  //   检索的输入已经变了（今天多了一条 refused，冷却计数 +1）。
  //   但**今天发出的卡片不该被追溯修改** —— 顺序不变量是
  //   "先出卡片，再收反馈，反馈影响的是明天"。
  //   所以这里作废的是"下次再进这个页面时不要拿旧缓存冒充当天的安排"，
  //   而当天这一份已经落在家长手里、也已经被提交过，不该再动。
  function invalidateDay() {
    day.value = null
  }

  return { CHILD, state, day, today, loadState, loadDay, invalidateDay }
}
