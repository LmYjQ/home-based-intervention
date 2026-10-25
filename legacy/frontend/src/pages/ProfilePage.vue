<script setup>
// 建档 + 画像。**一次性动作，之后很少再动**，所以单独一页，
// 不跟每天都要用的卡片/反馈挤在一起。
//
// ★ 为什么建档完成之后还能看到画像：ProfileForm 里那份明细在建档完成时
//   被 v-if 卸载了，顶栏又只有一个可信度数字 —— 家长建完档反而看不到
//   自己建了什么。现在 ProfileView 常驻在这里。
import { ref } from 'vue'
import ProfileForm from '../components/ProfileForm.vue'
import ProfileView from '../components/ProfileView.vue'

const props = defineProps({ CHILD: String, state: Object })
const emit = defineEmits(['done'])
const redoProfile = ref(false)

function onRedoDone() {
  // 写成方法而不是模板里 `@done="redoProfile = false; loadState()"` ——
  // 那串分号表达式编译器不报错，但读的人会以为是两条语句的一部分。
  redoProfile.value = false
  emit('done')
}
</script>

<template>
  <div>
    <ProfileForm v-if="!state?.has_profile" :child-id="CHILD" @done="onRedoDone" />

    <section v-else class="bar">
      画像可信度 <b>{{ state.profile_confidence?.toFixed(2) }}</b>
      <span v-if="state.profile_confidence < 0.5" class="warn">
        —— 低于升级闸门 0.5，状态机将冻结全部升级（这是正确行为）
      </span>
      <button class="mini" @click="redoProfile = true">重新建档</button>
    </section>

    <ProfileForm v-if="redoProfile" :child-id="CHILD" @done="onRedoDone" />

    <ProfileView :state="state" />
  </div>
</template>

<style scoped>
.bar { background:#fff; border:1px solid #e5e7eb; border-radius:10px;
  padding:12px 16px; margin-bottom:18px; font-size:13px; display:flex;
  align-items:center; gap:10px; flex-wrap:wrap; }
</style>
