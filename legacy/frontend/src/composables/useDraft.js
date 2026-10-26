// 草稿：家长填一半的反馈，刷新不丢。
//
// ★ 为什么存服务端 checkpoint 而不是 localStorage
//   项目约定草稿存服务端（技术方案 §5.5 的 provenance 层）。好处是多设备、
//   刷新、换浏览器都在，而且草稿与「这一天」是同一个 thread 家族，
//   出问题能顺着 thread_id 查回去。
//   （grep localStorage / sessionStorage / indexedDB 全空 —— 这是这个文件
//     出现之前的事实：家长填一半刷新即丢。）
//
// ★ 自动保存的三个时机，缺一个都会漏
//   1. 输入停 800ms（debounce）—— 不每敲一个字打一次请求
//   2. 页面被切走/隐藏（visibilitychange）—— 家长直接关标签页时的兜底，
//      debounce 根本没来得及跑
//   3. 组件卸载 / 换日期 —— 离开这个页面前把手上这份写掉
//
// ★ 失败不打断家长
//   草稿存不上不该让家长填的东西白费一次提示框 —— 静默重试即可，
//   只在 console 里留一句。真正的反馈（提交失败）才走 err 这条路。
import { ref, watch, onUnmounted } from 'vue'
import { api } from '../api.js'

const DEBOUNCE_MS = 800

export function useDraft(childId, date, rows, otherNotes) {
  const savedAt = ref('')     // 上次存成功的时间戳，给界面显示
  const restored = ref(false) // 本次是否真的恢复过内容（避免空草稿闪一下提示）
  let timer = null
  let stopped = false

  // 什么算「有东西要存」：自由文本有字，或至少有一行点了结果/写了补充。
  // ★ 不做这个判断的话，进页面时 syncRows 填出行数组就会触发一次空保存 ——
  //   每次打开页面都白写一条空草稿，而且界面上会立刻显示「草稿已存」，
  //   家长会以为自己的输入已经存下了（其实一个字都还没填）。
  function hasContent() {
    if ((otherNotes.value || '').trim()) return true
    return (rows.value || []).some(r => r.outcome || (r.note || '').trim())
  }

  async function flush() {
    if (stopped) return
    if (timer) { clearTimeout(timer); timer = null }
    if (!hasContent()) return      // 空的不值得存
    try {
      const r = await api.draftPut({
        child_id: childId, date: date.value,
        rows: rows.value, other_notes: otherNotes.value,
      })
      savedAt.value = r.saved_at
    } catch (e) {
      // 草稿是尽力而为的：存不上不该打断家长填表
      console.warn('[draft] 自动保存失败', e.message)
    }
  }

  function schedule() {
    if (timer) clearTimeout(timer)
    timer = setTimeout(flush, DEBOUNCE_MS)
  }

  // ---- 恢复：进页面 / 换日期时读一次 ----
  async function restore() {
    try {
      const r = await api.draftGet(childId, date.value)
      const d = r.draft
      if (!d) { restored.value = false; return }
      // ★ 一份「三行全空」的草稿不叫草稿 —— 它是 syncRows 填行时顺手存的，
      //   里面没有任何家长填过的东西。恢复它只会让界面上平白显示
      //   「已恢复上次填到一半的内容」，而家长明明什么都没填过。
      const carriesContent =
        !!d.other_notes?.trim() ||
        (d.rows || []).some(x => x.outcome || (x.note || '').trim())
      if (!carriesContent) { restored.value = false; return }
      // 按 activity_id 合并而不是整体替换：
      //   草稿里的行是「上次填到一半的」，而 activities 可能已经变了
      //   （换日期会重新排计划）。整体替换会把家长刚填的、这次仍然存在的行
      //   弄丢 —— 那比不恢复更糟。
      const byId = new Map((d.rows || []).map(x => [x.activity_id, x]))
      rows.value = rows.value.map(r => ({ ...r, ...(byId.get(r.activity_id) || {}) }))
      if (d.other_notes) otherNotes.value = d.other_notes
      savedAt.value = d.saved_at || ''
      restored.value = true
    } catch (e) {
      console.warn('[draft] 恢复失败', e.message)
    }
  }

  async function clear() {
    if (timer) { clearTimeout(timer); timer = null }
    try { await api.draftDelete(childId, date.value) } catch { /* 提交已经成功，不必为此打扰家长 */ }
    savedAt.value = ''
    restored.value = false
  }

  // 深度监听：反馈行是数组，里面每一项的 outcome/note 都会被家长改
  watch([rows, otherNotes], schedule, { deep: true })

  // ★ 切走标签页时立刻落盘 —— debounce 在这种情况下根本来不及跑
  const onHide = () => { if (document.visibilityState === 'hidden') flush() }
  document.addEventListener('visibilitychange', onHide)
  onUnmounted(() => {
    document.removeEventListener('visibilitychange', onHide)
    flush()
    stopped = true
  })

  return { savedAt, restored, flush, restore, clear }
}
