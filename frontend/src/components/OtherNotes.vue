<script setup>
// 当天的「其他观察」框。
// ★ 它和逐条反馈的差别是**有业务后果的**，不是"多一个输入框"：
//   它的话混着多件事（"超市里他捂耳朵，晚上又踢被子"），
//   所以后端走整段抽取模式（一次话里多次事件要抽全）。
//   而它**不计入 refusal_counts** —— "晚饭他把盘子摔了"扣不到任何
//   活动头上，就不该扣。它是一条 Observation，照常进教练诊断。
defineProps({ modelValue: String })
defineEmits(['update:modelValue'])
</script>

<template>
  <div class="other">
    <label>当天的「其他观察」（选填）—— 今天其它的事，混着说就行</label>
    <textarea :value="modelValue" rows="3"
      placeholder="例如：超市里他捂耳朵不肯进，晚上睡前踢被子"
      @input="$emit('update:modelValue', $event.target.value)"></textarea>
    <p class="note">
      这里写的内容会抽成结构化事件，供教练明天的诊断读。
      <b>但它不会扣到任何一条活动的头上</b> —— 没在活动上发生的行为不该扣活动的分。
    </p>
  </div>
</template>

<style scoped>
.other { border: 1px dashed #cbd5e1; border-radius: 10px; padding: 12px 14px; background: #fafbfc; }
label { font-size: 13px; color: #374151; display: block; margin-bottom: 6px; }
textarea { width: 100%; box-sizing: border-box; font-family: inherit; font-size: 13px;
  padding: 8px 10px; border: 1px solid #d1d5db; border-radius: 6px; }
.note { font-size: 12px; color: #888; margin: 7px 0 0; }
.note b { color: #b45309; }
</style>
