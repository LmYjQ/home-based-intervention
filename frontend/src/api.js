// 全项目唯一的 fetch 出口。组件里不出现 URL 字面量。
//
// ★ 这里刻意不做任何业务计算：
//   quality 由后端 feedback.quality_for() 算好后返回，前端只显示。
//   前端再算一遍就出现了第二套口径，而状态机的升级闸门拿平均 quality
//   比 0.5 / 0.75 —— 两套口径必然打架（阶段 4b 的坑）。

const BASE = ''   // 走 vite proxy，同源，不需要处理 CORS

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* 非 JSON 错误体 */ }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  setNoLlm: (no_llm) => req(`/api/config?no_llm=${no_llm}`, { method: 'POST' }),

  state: (cid) => req(`/api/state/${cid}`),
  profile: (body) => req('/api/profile', { method: 'POST', body: JSON.stringify(body) }),
  // 慢路径，走 job + SSE。force=true 让后端忽略已生成的那份重新排。
  card: (body) => req('/api/card', { method: 'POST', body: JSON.stringify(body) }),

  feedbacks: (body) => req('/api/feedbacks', { method: 'POST', body: JSON.stringify(body) }),
  close: (cid, on) => req(`/api/close/${cid}?on=${on}`, { method: 'POST' }),
  summaries: (cid) => req(`/api/summaries/${cid}`),
  progress: (cid) => req(`/api/progress/${cid}`),
  transitions: (cid, item) =>
    req(`/api/transitions/${cid}${item ? `?skill_item=${encodeURIComponent(item)}` : ''}`),

  // ---- 观察链（Phase 1）----
  // ★ run / resume 是**两阶段**：run 可能返回 phase:"clarify"（图停在
  //   interrupt 上等家长回答），那不是错误是正常结果，要再调 resume。
  //   两者的连续性靠 (child_id, date) → thread_id，不靠 job_id ——
  //   前端不持有任何 interrupt 状态，那是服务端的事（刷新就没了）。
  observeRun: (body) => req('/api/observe/run', { method: 'POST', body: JSON.stringify(body) }),
  observeResume: (body) =>
    req('/api/observe/resume', { method: 'POST', body: JSON.stringify(body) }),
  // 刷新页面后靠它知道「还有个问题没答」—— 纯读 checkpoint，毫秒级
  observeStatus: (cid, date) =>
    req(`/api/observe/status?child_id=${encodeURIComponent(cid)}&date=${encodeURIComponent(date)}`),

  // 草稿（存服务端 checkpoint，不是 localStorage）
  draftGet: (cid, date) =>
    req(`/api/draft?child_id=${encodeURIComponent(cid)}&date=${encodeURIComponent(date)}`),
  draftPut: (body) => req('/api/draft', { method: 'PUT', body: JSON.stringify(body) }),
  draftDelete: (cid, date) =>
    req(`/api/draft?child_id=${encodeURIComponent(cid)}&date=${encodeURIComponent(date)}`,
        { method: 'DELETE' }),
}

// ★ 慢路径统一走 SSE。两条慢路径（建档 18-47s、出卡片 30-45s）共用这一份。
//   返回 {steps, result, error}；onStep 每次收到一行进度就调一次。
export function streamJob(jobId, onStep) {
  return new Promise((resolve, reject) => {
    const es = new EventSource(`/api/jobs/${jobId}/stream`)
    let last = null
    es.onmessage = (ev) => {
      const d = JSON.parse(ev.data)
      if (d.text) onStep?.(d)
      if (d.status && d.status !== 'running') {
        last = d
        es.close()
        if (d.status === 'error') reject(new Error(d.error))
        else resolve(d)
      }
    }
    es.onerror = () => { es.close(); reject(new Error('SSE 断了（服务器可能重启）')) }
  })
}

// 建档 → job_id → SSE 的两步合起来
export async function runJob(path, body, onStep) {
  const { job_id } = await req(path, { method: 'POST', body: JSON.stringify(body) })
  return streamJob(job_id, onStep)
}
