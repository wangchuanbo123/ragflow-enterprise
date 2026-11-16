<script setup lang="ts">
import { ref } from 'vue'
import * as api from '../api/client'
import { useAuth } from '../composables/useAuth'
import { passwordError } from '../passwordValidation'
const { user, clearSession } = useAuth()
const currentPassword = ref('')
const newPassword = ref('')
const confirmation = ref('')
const error = ref('')
const saving = ref(false)
async function save(): Promise<void> {
  error.value = ''
  if (newPassword.value !== confirmation.value) { error.value = '两次输入的新密码不一致'; return }
  error.value = passwordError(newPassword.value)
  if (error.value) return
  saving.value = true
  try {
    await api.changePassword(currentPassword.value, newPassword.value)
    currentPassword.value = ''; newPassword.value = ''; confirmation.value = ''
    clearSession()
  } catch (e) { error.value = (e as Error).message }
  finally { saving.value = false }
}
</script>
<template>
  <div class="settings-page"><h2>账号设置</h2><p>{{ user?.display_name || user?.username }} · @{{ user?.username }}</p>
    <form @submit.prevent="save"><h3>修改密码</h3><p>保存后需要使用新密码重新登录，其他已登录设备也将退出。</p>
      <label>当前密码<input v-model="currentPassword" type="password" autocomplete="current-password" required /></label>
      <label>新密码<input v-model="newPassword" type="password" autocomplete="new-password" minlength="12" maxlength="72" required /></label>
      <small>至少 12 个字符，须含字母和数字；UTF-8 编码最多 72 字节。</small>
      <label>确认新密码<input v-model="confirmation" type="password" autocomplete="new-password" minlength="12" maxlength="72" required /></label>
      <p v-if="error" class="error" role="alert">{{ error }}</p><button :disabled="saving">{{ saving ? '正在保存…' : '保存并重新登录' }}</button>
    </form>
  </div>
</template>
<style scoped>
.settings-page{height:100%;overflow:auto;padding:24px}.settings-page>p,form>p{color:#758296;font-size:12px}form{display:grid;gap:14px;max-width:480px;padding:22px;border:1px solid #e1e7f0;border-radius:12px;background:white}h3,form p{margin:0}label{display:grid;gap:6px;color:#526177;font-size:12px}input{padding:10px;border:1px solid #dce3ed;border-radius:8px}button{padding:10px;border:0;border-radius:8px;color:white;background:#4f6ef7;cursor:pointer}button:disabled{opacity:.5}.error{color:#b42318}
</style>
