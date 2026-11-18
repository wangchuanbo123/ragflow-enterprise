<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import * as api from '../api/client'
import type { User } from '../types'
import { useAuth } from '../composables/useAuth'
import { passwordError } from '../passwordValidation'

const { user, clearSession } = useAuth()
const users = ref<User[]>([])
const editingId = ref('')
const form = reactive({ username: '', display_name: '', password: '', role: 'user' as 'user' | 'admin', is_active: true })
const reviewNotes = reactive<Record<string, string>>({})
const error = ref('')
const message = ref('')
const saving = ref(false)
const loading = ref(false)
const reviewingId = ref('')
const pendingUsers = computed(() => users.value.filter(item => item.registration_status === 'pending'))
const accountUsers = computed(() => users.value.filter(item => item.registration_status !== 'pending'))
const busy = computed(() => saving.value || Boolean(reviewingId.value))
let disposed = false
let loadRequest = 0
onBeforeUnmount(() => { disposed = true; ++loadRequest })

function accountStatus(item: User): string {
  if (item.registration_status === 'pending') return '待审核'
  if (item.registration_status === 'rejected') return '注册已拒绝'
  return item.is_active ? '启用' : '停用'
}

function dateText(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : '时间未记录'
}

async function load(): Promise<void> {
  const request = ++loadRequest
  loading.value = true; error.value = ''
  try {
    const result = await api.listUsers()
    if (!disposed && request === loadRequest) users.value = result
  } catch (cause) {
    if (!disposed && request === loadRequest) error.value = (cause as Error).message
  } finally {
    if (!disposed && request === loadRequest) loading.value = false
  }
}

function edit(item?: User): void {
  if (busy.value || (item && item.registration_status && item.registration_status !== 'approved')) return
  editingId.value = item?.id || ''
  Object.assign(form, { username: item?.username || '', display_name: item?.display_name || '', password: '', role: item?.role || 'user', is_active: item?.is_active ?? true })
  error.value = ''; message.value = ''
}

async function save(): Promise<void> {
  if (busy.value || loading.value) return
  if (form.password) { error.value = passwordError(form.password); if (error.value) return }
  else if (!editingId.value) { error.value = '请输入初始密码'; return }
  saving.value = true; error.value = ''; message.value = ''
  try {
    if (editingId.value) {
      const result = await api.updateUser(editingId.value, { display_name: form.display_name, role: form.role, is_active: form.is_active, ...(form.password ? { password: form.password } : {}) })
      if (disposed) return
      users.value = users.value.map(item => item.id === result.id ? result : item)
      if (editingId.value === user.value?.id && (form.password || !form.is_active || form.role !== user.value.role)) { clearSession(); return }
    } else {
      await api.createUser({ ...form, username: form.username.trim() })
      if (disposed) return
    }
    editingId.value = ''
    Object.assign(form, { username: '', display_name: '', password: '', role: 'user', is_active: true })
    await load()
    if (!disposed) message.value = '用户信息已保存'
  } catch (cause) { if (!disposed) error.value = (cause as Error).message }
  finally { if (!disposed) { saving.value = false; form.password = '' } }
}

async function review(item: User, decision: 'approved' | 'rejected'): Promise<void> {
  if (busy.value || loading.value || item.registration_status !== 'pending') return
  reviewingId.value = item.id; error.value = ''; message.value = ''
  try {
    const note = (reviewNotes[item.id] || '').trim()
    const result = await api.reviewUserRegistration(item.id, { decision, ...(note ? { note } : {}) })
    if (disposed) return
    users.value = users.value.map(current => current.id === result.id ? result : current)
    delete reviewNotes[item.id]
    await load()
    if (!disposed) message.value = decision === 'approved' ? `已通过 ${item.username} 的注册申请，用户现在可以登录。` : `已拒绝 ${item.username} 的注册申请。`
  } catch (cause) { if (!disposed) error.value = (cause as Error).message }
  finally { if (!disposed) reviewingId.value = '' }
}

onMounted(load)
</script>

<template>
  <div class="users-page">
    <header class="page-heading"><div><h2>用户管理</h2><p>审核注册申请、管理系统账号；知识库访问权限在“成员与权限”中配置。</p></div><button :disabled="loading || busy" @click="load">刷新</button><button :disabled="busy" @click="edit()">新建用户</button></header>
    <p v-if="error" class="notice error" role="alert">{{ error }}</p>
    <p v-if="message" class="notice message" role="status">{{ message }}</p>

    <section class="registration-requests" aria-labelledby="registration-review-title">
      <header><div><h3 id="registration-review-title">待审核注册申请 <span class="pending-count">{{ pendingUsers.length }}</span></h3><p>审核通过后启用普通用户账号。</p></div></header>
      <p v-if="loading && !users.length" class="empty">正在读取用户…</p>
      <p v-else-if="!pendingUsers.length" class="empty">暂无待审核注册申请</p>
      <article v-for="item in pendingUsers" :key="item.id" class="registration-request">
        <div class="applicant"><strong>{{ item.display_name || item.username }}</strong><small>@{{ item.username }} · 申请时间：{{ dateText(item.created_at) }}</small><span class="status pending">待审核</span></div>
        <div class="review-controls"><input v-model="reviewNotes[item.id]" :aria-label="`${item.username} 的审核备注`" maxlength="500" placeholder="审核备注（可选）" :disabled="busy" /><div><button class="approve" :disabled="busy || loading" @click="review(item, 'approved')">{{ reviewingId === item.id ? '处理中…' : '审核通过' }}</button><button class="reject" :disabled="busy || loading" @click="review(item, 'rejected')">拒绝申请</button></div></div>
      </article>
    </section>

    <div class="layout">
      <section class="account-list"><header><h3>用户账号</h3><span>{{ accountUsers.length }} 个</span></header>
        <article v-for="item in accountUsers" :key="item.id"><div><strong>{{ item.display_name || item.username }}</strong><small>@{{ item.username }} · {{ item.role === 'admin' ? '管理员' : '普通用户' }} · <span :class="{ rejected: item.registration_status === 'rejected' }">{{ accountStatus(item) }}</span></small><small v-if="item.registration_reviewed_at">审核时间：{{ dateText(item.registration_reviewed_at) }}</small><small v-if="item.registration_review_note">审核备注：{{ item.registration_review_note }}</small></div><button v-if="!item.registration_status || item.registration_status === 'approved'" :disabled="busy" @click="edit(item)">编辑</button></article>
        <p v-if="!accountUsers.length && !loading" class="empty">暂无用户账号</p>
      </section>
      <form class="account-form" @submit.prevent="save"><h3>{{ editingId ? '编辑用户' : '新建用户' }}</h3>
        <fieldset :disabled="busy || loading">
          <label>用户名<input v-model="form.username" :disabled="!!editingId" required maxlength="64" pattern="[A-Za-z0-9_.@-]+" title="使用英文字母、数字或 _ . @ -" autocomplete="off" /></label>
          <label>显示名称<input v-model="form.display_name" maxlength="64" /></label>
          <label>{{ editingId ? '重置密码（留空则保留）' : '初始密码' }}<input v-model="form.password" type="password" autocomplete="new-password" :required="!editingId" minlength="12" maxlength="72" /></label>
          <small>至少 12 个字符，须含字母和数字；UTF-8 编码最多 72 字节。重置后该用户的所有会话将退出。</small>
          <label>系统角色<select v-model="form.role"><option value="user">普通用户</option><option value="admin">管理员</option></select></label>
          <label class="check"><input v-model="form.is_active" type="checkbox" /> 启用账号</label>
          <button :disabled="busy || loading">{{ saving ? '正在保存…' : '保存用户' }}</button>
        </fieldset>
      </form>
    </div>
  </div>
</template>

<style scoped>
.users-page { height: 100%; overflow: auto; padding: 22px; color: #344159; }
header { display: flex; align-items: center; gap: 12px; }header > div { flex: 1; }.page-heading { margin-bottom: 16px; }h2,h3,p { margin: 0; }h2 { font-size: 18px; }h3 { font-size: 14px; }header p { margin-top: 6px; color: #8491a3; font-size: 12px; }
.layout { display: grid; grid-template-columns: 1fr 340px; gap: 16px; }section,form { padding: 18px; border: 1px solid #e1e7f0; border-radius: 12px; background: #fff; }.registration-requests { margin-bottom: 16px; }.pending-count { display: inline-block; min-width: 22px; margin-left: 5px; padding: 3px 6px; border-radius: 10px; text-align: center; color: #96601d; background: #fff3d9; font-size: 11px; }
article { display: flex; align-items: center; gap: 12px; padding: 14px 0; border-bottom: 1px solid #edf1f6; }article:last-child { border-bottom: 0; }article > div { flex: 1; min-width: 0; }strong,small { display: block; }strong { font-size: 13px; overflow-wrap: anywhere; }small { margin-top: 4px; color: #8491a3; font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.status { display: inline-block; margin-top: 6px; padding: 3px 6px; border-radius: 5px; font-size: 10px; }.pending { color: #96601d; background: #fff3d9; }.rejected { color: #b42318; }.review-controls { display: flex; align-items: center; gap: 10px; }.review-controls input { flex: 1; min-width: 0; }.review-controls > div { display: flex; gap: 7px; flex-shrink: 0; }.account-list header { justify-content: space-between; }.account-list header > span { color: #8491a3; font-size: 11px; }
.account-form { align-self: start; }.account-form h3 { margin-bottom: 12px; }fieldset { display: grid; gap: 12px; min-width: 0; margin: 0; padding: 0; border: 0; }label { display: grid; gap: 5px; color: #526177; font-size: 12px; }input,select { box-sizing: border-box; min-width: 0; max-width: 100%; padding: 9px; border: 1px solid #dce3ed; border-radius: 8px; font: inherit; font-size: 12px; }.check { display: flex; align-items: center; }button { padding: 8px 12px; border: 1px solid #dce3ed; border-radius: 8px; color: #4561d8; background: #f5f7ff; font-size: 12px; white-space: nowrap; cursor: pointer; }button:disabled { opacity: .5; cursor: not-allowed; }button.approve { color: #fff; border-color: #4f6ef7; background: #4f6ef7; }button.reject { color: #b42318; border-color: #f2cbc7; background: #fff6f5; }
.notice { margin: 10px 0; padding: 10px 12px; border-radius: 8px; font-size: 12px; line-height: 1.6; }.error { color: #b42318; background: #fff1f0; }.message { color: #18794e; background: #ecf9f1; }.empty { padding: 18px 0; color: #8491a3; font-size: 12px; }
@media (max-width: 1050px) { .review-controls { align-items: stretch; flex-direction: column; } }
@media (max-width: 850px) { .layout { grid-template-columns: 1fr; }.registration-request { align-items: stretch; flex-direction: column; width: 100%; }.registration-request > div { width: 100%; }.page-heading { flex-wrap: wrap; }.page-heading > div { flex-basis: 100%; } }
</style>
