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

  // ---- 测试账号（手动测试台用）----
  listChildren: (params = {}) => {
    const query = new URLSearchParams(params).toString()
    return request(`/api/children${query ? `?${query}` : ''}`)
  },

  createChild: (payload = {}) =>
    request('/api/children', { method: 'POST', body: payload }),

  // 这个孩子的会话列表（新的在前）
  listChildConversations: (childId) =>
    request(`/api/children/${childId}/conversations`),

  createConversation: (payload) =>
    request('/api/conversations', { method: 'POST', body: payload }),

  sendMessage: (conversationId, payload) =>
    request(`/api/conversations/${conversationId}/messages`, {
      method: 'POST',
      body: payload,
    }),

  // 回答一轮追问：answers 是 [{question, answer}, ...]
  answerFollowups: (conversationId, answers) =>
    request(`/api/conversations/${conversationId}/messages`, {
      method: 'POST',
      body: { message: '', answers },
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

  // 反馈不足时，回答追问（turn_id 必须和第一次提交时一致）
  resumeFeedback: (trialId, payload) =>
    request(`/api/trials/${trialId}/feedback/resume`, {
      method: 'POST',
      body: payload,
    }),

  getFeedback: (trialId, turnId) =>
    request(`/api/trials/${trialId}/feedback/${encodeURIComponent(turnId)}`),

  // 反馈里补充新情况 → 从"生成建议卡"之前分叉，只重出方案。
  // turn_id 是幂等键，同一次修订重试要沿用同一个值。
  revisePlan: (trialId, payload) =>
    request(`/api/trials/${trialId}/revise`, { method: 'POST', body: payload }),

  // ---- LLM 设置（前端可改，立刻生效）----
  getLLMSettings: () => request('/api/settings/llm'),

  updateLLMSettings: (payload) =>
    request('/api/settings/llm', { method: 'POST', body: payload }),

  // ---- 调试（阶段 5）----
  debugState: (conversationId, threadId) => {
    const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : ''
    return request(`/api/debug/conversations/${conversationId}/state${query}`)
  },

  debugCheckpoints: (conversationId, { threadId, limit = 50 } = {}) => {
    const query = new URLSearchParams({ limit })
    if (threadId) query.set('thread_id', threadId)
    return request(
      `/api/debug/conversations/${conversationId}/checkpoints?${query}`,
    )
  },

  debugFork: (conversationId, payload) =>
    request(`/api/debug/conversations/${conversationId}/fork`, {
      method: 'POST',
      body: payload,
    }),

  // ---- 长期记忆（阶段 3）----
  listMemories: (childId, familyId, params = {}) => {
    const query = new URLSearchParams({ family_id: familyId, ...params })
    return request(`/api/children/${childId}/memories?${query}`)
  },

  confirmMemory: (childId, memoryId, payload) =>
    request(`/api/children/${childId}/memories/${memoryId}/confirm`, {
      method: 'POST',
      body: payload,
    }),

  correctMemory: (childId, memoryId, payload) =>
    request(`/api/children/${childId}/memories/${memoryId}`, {
      method: 'PATCH',
      body: payload,
    }),

  deleteMemory: (childId, memoryId, payload) =>
    request(`/api/children/${childId}/memories/${memoryId}/delete`, {
      method: 'POST',
      body: payload,
    }),
}

// 流式发消息（SSE）。
//
// **推的是进度，不是半截答案**：抽取类调用走 json_schema 严格约束解码，
// 模型必须保证 JSON 合法才能吐 token，中途吐出来是半截 JSON，家长看不懂。
// 所以这条流解决的是"它卡在哪儿" —— 每推一条 progress 就更新一下提示。
//
// onProgress({stage, label, kind, attempt}) 在每条 progress 事件时调用。
// 返回最终结果（和 sendMessage 的返回结构一样）。
//
// **fetch 不能直接读 SSE**：EventSource 只支持 GET，而且不能带 body。
// 所以这里用 fetch + ReadableStream 自己解 SSE 的帧格式。
export async function streamFeedback(trialId, payload, { onProgress } = {}) {
  return streamSSE(`/api/trials/${trialId}/feedback/stream`, payload, { onProgress })
}

export async function streamMessage(conversationId, payload, { onProgress } = {}) {
  return streamSSE(
    `/api/conversations/${conversationId}/messages/stream`,
    payload,
    { onProgress },
  )
}

// SSE 的公共部分。
//
// **为什么抽出来**：反馈和聊天都要边跑边推进度，
// 帧格式、错误处理、"流结束了但没收到结果"这些写两遍一定会走样。
async function streamSSE(url, payload, { onProgress } = {}) {
  let resp
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    })
  } catch (e) {
    throw new Error(`连不上后端：${e.message}（确认 make dev 已经起了）`)
  }

  if (!resp.ok) {
    const text = await resp.text()
    let detail = `HTTP ${resp.status}`
    try {
      const data = JSON.parse(text)
      if (typeof data?.detail === 'string') detail = `HTTP ${resp.status}：${data.detail}`
    } catch {
      if (text) detail = `HTTP ${resp.status}：${text.slice(0, 300)}`
    }
    throw new Error(detail)
  }

  // **不一定是 SSE**：反馈那条流在"已经在追问上了"或"这次反馈已经处理完"
  // 的时候直接返回普通 JSON（不用重跑图）。按 content-type 分流 ——
  // 硬按 SSE 解会得到"流结束了但没收到结果"。
  const contentType = resp.headers.get('content-type') || ''
  if (!contentType.includes('text/event-stream')) {
    return JSON.parse((await resp.text()) || 'null')
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result = null
  let failure = null

  // SSE 的帧用空行分隔，每帧是若干行 "field: value"。
  // 这里按空行切帧，帧内逐行解。
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let idx
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)

      let event = 'message'
      const dataLines = []
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      }
      if (!dataLines.length) continue
      const payloadText = dataLines.join('\n')

      if (event === 'progress') {
        try {
          onProgress?.(JSON.parse(payloadText))
        } catch {
          // 进度解析失败不该中断整条流
        }
      } else if (event === 'result') {
        result = JSON.parse(payloadText)
      } else if (event === 'error') {
        try {
          failure = JSON.parse(payloadText).message
        } catch {
          failure = payloadText
        }
      }
    }
  }

  if (failure) throw new Error(failure)
  if (!result) throw new Error('流结束了但没收到结果')
  return result
}