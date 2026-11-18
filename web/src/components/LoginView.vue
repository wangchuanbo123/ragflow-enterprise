<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import * as api from '../api/client'
import { useAuth } from '../composables/useAuth'
import { passwordError } from '../passwordValidation'

const { state, login } = useAuth()
const mode = ref<'login' | 'register'>('login')
const username = ref('')
const displayName = ref('')
const password = ref('')
const confirmation = ref('')
const submitting = ref(false)
const localError = ref('')
const message = ref('')
const busy = computed(() => submitting.value || state.loading)
let disposed = false
onBeforeUnmount(() => { disposed = true })

function switchMode(next: 'login' | 'register'): void {
  if (busy.value) return
  mode.value = next
  password.value = ''; confirmation.value = ''; localError.value = ''; message.value = ''
}

async function onSubmit(): Promise<void> {
  if (busy.value) return
  localError.value = ''; message.value = ''
  const name = username.value.trim()
  if (!name) { localError.value = '请输入用户名'; return }
  if (mode.value === 'register') {
    if (!/^[A-Za-z0-9_.@-]{1,64}$/.test(name)) { localError.value = '用户名须为 1–64 个英文字母、数字或 _ . @ -'; return }
    localError.value = passwordError(password.value)
    if (localError.value) return
    if (password.value !== confirmation.value) { localError.value = '两次输入的密码不一致'; return }
  }
  submitting.value = true
  try {
    if (mode.value === 'register') {
      await api.registerUser({ username: name, password: password.value, display_name: displayName.value.trim() })
      if (disposed) return
      username.value = name; password.value = ''; confirmation.value = ''; mode.value = 'login'
      message.value = '注册申请已提交，等待管理员审核，通过后即可登录。'
    } else await login(name, password.value)
  } catch (cause) {
    if (!disposed) localError.value = (cause as Error).message
  } finally {
    if (!disposed) submitting.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="onSubmit">
      <h1 class="title">企业知识库</h1>
      <nav class="auth-modes" aria-label="账号操作">
        <button type="button" :class="{ active: mode === 'login' }" :aria-pressed="mode === 'login'" :disabled="busy" @click="switchMode('login')">登录</button>
        <button type="button" :class="{ active: mode === 'register' }" :aria-pressed="mode === 'register'" :disabled="busy" @click="switchMode('register')">申请注册</button>
      </nav>
      <p class="subtitle">{{ mode === 'register' ? '提交注册申请，管理员审核通过后即可登录。' : '请登录后开始对话' }}</p>
      <label class="field"><span>用户名</span><input v-model="username" type="text" autocomplete="username" required maxlength="64" :disabled="busy" /></label>
      <label v-if="mode === 'register'" class="field"><span>显示名称</span><input v-model="displayName" type="text" autocomplete="name" maxlength="64" :disabled="busy" placeholder="可选，留空使用用户名" /></label>
      <label class="field"><span>密码</span><input v-model="password" type="password" :autocomplete="mode === 'register' ? 'new-password' : 'current-password'" required :minlength="mode === 'register' ? 12 : undefined" :maxlength="mode === 'register' ? 72 : 128" :disabled="busy" /></label>
      <label v-if="mode === 'register'" class="field"><span>确认密码</span><input v-model="confirmation" type="password" autocomplete="new-password" required minlength="12" maxlength="72" :disabled="busy" /></label>
      <small v-if="mode === 'register'" class="password-help">密码至少 12 个字符，须含字母和数字，UTF-8 编码最多 72 字节。</small>
      <p v-if="localError" class="error" role="alert">{{ localError }}</p>
      <p v-if="message" class="message" role="status">{{ message }}</p>
      <button class="submit" type="submit" :disabled="busy">{{ busy ? (mode === 'register' ? '提交中…' : '登录中…') : (mode === 'register' ? '提交注册申请' : '登录') }}</button>
    </form>
  </div>
</template>

<style scoped>
.login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 16px; }
.login-card { width: 100%; max-width: 380px; background: #fff; border-radius: 14px; padding: 32px 28px; box-shadow: 0 12px 40px #0003; display: flex; flex-direction: column; gap: 14px; }
.title { margin: 0; font-size: 22px; text-align: center; color: #333; }
.auth-modes { display: flex; gap: 4px; padding: 4px; border-radius: 9px; background: #f0f2fa; }
.auth-modes button { flex: 1; padding: 8px; border: 0; border-radius: 6px; background: transparent; color: #758099; cursor: pointer; }
.auth-modes button.active { color: #4f60c5; background: white; box-shadow: 0 1px 4px #354c6c14; }
.subtitle { margin: 0 0 6px; text-align: center; color: #888; font-size: 13px; line-height: 1.6; }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: #555; }
.field input { min-width: 0; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; outline: none; }
.field input:focus { border-color: #667eea; }
.password-help { color: #7c879a; font-size: 11px; line-height: 1.6; }
.error, .message { margin: 0; padding: 10px; border-radius: 7px; font-size: 13px; line-height: 1.6; }
.error { color: #b42318; background: #fff1f0; }.message { color: #18794e; background: #ecf9f1; }
.submit { margin-top: 6px; padding: 11px; border: none; border-radius: 8px; background: #667eea; color: #fff; font-size: 15px; cursor: pointer; }
.submit:hover:not(:disabled) { background: #5568d3; }button:disabled { opacity: .6; cursor: not-allowed; }
</style>
