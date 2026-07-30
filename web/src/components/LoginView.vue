<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useAuth } from '../composables/useAuth'

const { state, login, user } = useAuth()

const username = ref('')
const password = ref('')
const localError = ref('')

async function onSubmit(): Promise<void> {
  localError.value = ''
  try {
    await login(username.value.trim(), password.value)
  } catch (e) {
    localError.value = (e as Error).message
  }
}

void reactive({ user })
</script>

<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="onSubmit">
      <h1 class="title">企业知识库</h1>
      <p class="subtitle">请登录后开始对话</p>

      <label class="field">
        <span>用户名</span>
        <input v-model="username" type="text" autocomplete="username" required />
      </label>

      <label class="field">
        <span>密码</span>
        <input v-model="password" type="password" autocomplete="current-password" required />
      </label>

      <p v-if="localError" class="error">{{ localError }}</p>

      <button class="submit" type="submit" :disabled="state.loading">
        {{ state.loading ? '登录中…' : '登录' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  padding: 16px;
}
.login-card {
  width: 100%;
  max-width: 360px;
  background: #fff;
  border-radius: 14px;
  padding: 32px 28px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.2);
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.title {
  margin: 0;
  font-size: 22px;
  text-align: center;
  color: #333;
}
.subtitle {
  margin: 0 0 6px;
  text-align: center;
  color: #888;
  font-size: 13px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: #555;
}
.field input {
  padding: 10px 12px;
  border: 1px solid #ddd;
  border-radius: 8px;
  font-size: 14px;
  outline: none;
  transition: border-color 0.15s;
}
.field input:focus {
  border-color: #667eea;
}
.error {
  margin: 0;
  color: #d33;
  font-size: 13px;
  text-align: center;
}
.submit {
  margin-top: 6px;
  padding: 11px;
  border: none;
  border-radius: 8px;
  background: #667eea;
  color: #fff;
  font-size: 15px;
  cursor: pointer;
  transition: background 0.15s;
}
.submit:hover:not(:disabled) {
  background: #5568d3;
}
.submit:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
