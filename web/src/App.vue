<script setup lang="ts">
import { ref } from "vue";
import { ApiError, gotoWithToken } from "./api";

// 任何页面拿到 401 都走这里：弹令牌输入，跳 /?token= 让 hub 种 Cookie
const needToken = ref(false);
const token = ref("");
const message = ref("");

export type AuthGate = (err: unknown) => boolean;
function onError(err: unknown): boolean {
  if (err instanceof ApiError && err.status === 401) {
    needToken.value = true;
    return true;
  }
  message.value = err instanceof Error ? err.message : String(err);
  return false;
}
defineExpose({ onError });
</script>

<template>
  <div class="page">
    <router-view v-slot="{ Component }">
      <component :is="Component" :on-error="onError" />
    </router-view>

    <el-dialog v-model="needToken" title="需要访问令牌" width="420px" :close-on-click-modal="false">
      <p class="muted">这个 hub 配了 serve.token。填一次，之后靠 Cookie 放行。</p>
      <el-input v-model="token" placeholder="serve.token" show-password @keyup.enter="gotoWithToken(token)" />
      <template #footer>
        <el-button type="primary" :disabled="!token" @click="gotoWithToken(token)">进入</el-button>
      </template>
    </el-dialog>

    <el-alert v-if="message" :title="message" type="error" show-icon closable @close="message = ''" />
  </div>
</template>
