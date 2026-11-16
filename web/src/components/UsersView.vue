<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import * as api from '../api/client'
import type { User } from '../types'
import { useAuth } from '../composables/useAuth'
import { passwordError } from '../passwordValidation'
const { user, clearSession } = useAuth()
const users = ref<User[]>([])
const editingId = ref('')
const form = reactive({ username: '', display_name: '', password: '', role: 'user' as 'user'|'admin', is_active: true })
const error = ref('')
const message = ref('')
const saving = ref(false)
async function load(): Promise<void> { try { users.value = await api.listUsers() } catch (e) { error.value = (e as Error).message } }
function edit(item?: User): void {
  editingId.value = item?.id || ''
  Object.assign(form, { username: item?.username || '', display_name: item?.display_name || '', password: '', role: item?.role || 'user', is_active: item?.is_active ?? true })
  error.value = ''; message.value = ''
}
async function save(): Promise<void> {
  if (form.password) { error.value = passwordError(form.password); if (error.value) return }
  saving.value = true; error.value = ''; message.value = ''
  try {
    if (editingId.value) {
      await api.updateUser(editingId.value, { display_name: form.display_name, role: form.role, is_active: form.is_active, ...(form.password ? { password: form.password } : {}) })
      if (editingId.value === user.value?.id && (form.password || !form.is_active || form.role !== user.value.role)) { clearSession(); return }
    } else await api.createUser({ ...form })
    edit(); await load(); message.value = '用户信息已保存'
  } catch (e) { error.value = (e as Error).message }
  finally { saving.value = false; form.password = '' }
}
onMounted(load)
</script>
<template>
  <div class="users-page"><header><div><h2>用户管理</h2><p>创建用户、分配系统角色、停用账号或重置密码；知识库访问权限在“成员与权限”中配置。</p></div><button @click="edit()">新建用户</button></header>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="message" class="message">{{ message }}</p>
    <div class="layout"><section><article v-for="item in users" :key="item.id"><div><strong>{{ item.display_name || item.username }}</strong><small>@{{ item.username }} · {{ item.role==='admin'?'管理员':'普通用户' }} · {{ item.is_active?'启用':'停用' }}</small></div><button @click="edit(item)">编辑</button></article><p v-if="!users.length">暂无用户</p></section>
      <form @submit.prevent="save"><h3>{{ editingId?'编辑用户':'新建用户' }}</h3><label>用户名<input v-model="form.username" :disabled="!!editingId" required maxlength="64" pattern="[A-Za-z0-9_.@-]+" title="使用英文字母、数字或 _ . @ -" autocomplete="off" /></label><label>显示名称<input v-model="form.display_name" maxlength="64" /></label><label>{{ editingId?'重置密码（留空则保留）':'初始密码' }}<input v-model="form.password" type="password" autocomplete="new-password" :required="!editingId" minlength="12" maxlength="72" /></label><small>至少 12 个字符，须含字母和数字；UTF-8 编码最多 72 字节。重置后该用户的所有会话将退出。</small><label>系统角色<select v-model="form.role"><option value="user">普通用户</option><option value="admin">管理员</option></select></label><label class="check"><input v-model="form.is_active" type="checkbox" /> 启用账号</label><button :disabled="saving">{{ saving?'正在保存…':'保存用户' }}</button></form>
    </div>
  </div>
</template>
<style scoped>
.users-page{height:100%;overflow:auto;padding:22px}header{display:flex;align-items:center;gap:16px}header div{flex:1}h2{margin:0}header p{color:#8491a3;font-size:12px}.layout{display:grid;grid-template-columns:1fr 340px;gap:16px}section,form{padding:18px;border:1px solid #e1e7f0;border-radius:12px;background:#fff}article{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #edf1f6}article div{flex:1}strong,small{display:block}small{margin-top:4px;color:#8491a3;font-size:11px}form{display:grid;gap:12px;align-self:start}h3{margin:0}label{display:grid;gap:5px;color:#526177;font-size:12px}input,select{padding:9px;border:1px solid #dce3ed;border-radius:8px}.check{display:flex;align-items:center}button{padding:8px 12px;border:1px solid #dce3ed;border-radius:8px;color:#4561d8;background:#f5f7ff;cursor:pointer}.error{color:#b42318}.message{color:#18794e}@media(max-width:850px){.layout{grid-template-columns:1fr}}
</style>
