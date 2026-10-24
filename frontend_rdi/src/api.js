// 后端接口封装。
//
// 只做三件事：拼 URL、把 HTTP 错误变成可读的异常、把响应原样透传。
// 不在这里做业务判断 —— 手动测试台的价值就在于"后端返回什么就显示什么"，
// 前端加工过的东西会掩盖真实问题。

async function request(path, { method = 'GET', body } = {}) {
  let resp
  try {
    resp = await fetch(path, {
      method,
      headers: body ? { 'content-type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    throw new Error(`连不上后端：${e.message}（确认 make dev 已经起了）`)
  }

  const text = await resp.text()
  let data = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      // 后端 500 有时返回 HTML 堆栈，原样带出去比"解析失败"有用
      throw new Error(`HTTP ${resp.status} · 非 JSON 响应：\n${text.slice(0, 800)}`)
    }
  }

  if (!resp.ok) {
    const detail = data?.detail
    throw new Error(
      typeof detail === 'string' ? `HTTP ${resp.status}：${detail}` : `HTTP ${resp.status}`,
    )
  }
  return data
}

export const api = {
  health: () => request('/health'),

  createConversation: (payload) =>
    request('/api/conversations', { method: 'POST', body: payload }),

  sendMessage: (conversationId, payload) =>
    request(`/api/conversations/${conversationId}/messages`, {
      method: 'POST',
      body: payload,
    }),

  selectPlan: (conversationId, payload) =>
    request(`/api/conversations/${conversationId}/select-plan`, {
      method: 'POST',
      body: payload,
    }),

  getConversation: (conversationId) => request(`/api/conversations/${conversationId}`),

  getPlan: (planId) => request(`/api/plans/${planId}`),

  submitFeedback: (trialId, payload) =>
    request(`/api/trials/${trialId}/feedback`, { method: 'POST', body: payload }),
}