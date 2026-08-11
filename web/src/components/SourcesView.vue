<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import type { DataSource, KnowledgeBaseMember } from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const tab = ref<'sources' | 'members'>('sources')
const sources = ref<DataSource[]>([])
const members = ref<KnowledgeBaseMember[]>([])
const error = ref('')
const syncing = ref<string | null>(null)
const sourceForm = reactive({ source_type: 'git' as DataSource['source_type'], name: '', location: '', connection_ref: '', sync_interval_minutes: 0, deletion_policy: 'ignore' as 'ignore'|'delete', index_code: true })
const memberForm = reactive({ username: '', role: 'viewer' as 'viewer' | 'editor' })

async function load(): Promise<void> {
  try {
    ;[sources.value, members.value] = await Promise.all([
      api.listDataSources(props.knowledgeBaseId),
      api.listKnowledgeBaseMembers(props.knowledgeBaseId),
    ])
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
}

async function addSource(): Promise<void> {
  try {
    await api.createDataSource(props.knowledgeBaseId, {
      source_type: sourceForm.source_type,
      name: sourceForm.name,
      connection_ref: sourceForm.connection_ref || null,
      config: sourceForm.location ? { location: sourceForm.location, index_code: sourceForm.index_code } : {},
      sync_interval_minutes: sourceForm.sync_interval_minutes || null,
      deletion_policy: sourceForm.deletion_policy,
    })
    Object.assign(sourceForm, { source_type: 'git', name: '', location: '', connection_ref: '', sync_interval_minutes: 0, deletion_policy: 'ignore', index_code: true })
    await load()
  } catch (e) { error.value = (e as Error).message }
}

async function toggleSource(source: DataSource): Promise<void> {
  try { await api.updateDataSource(props.knowledgeBaseId, source.id, { status: source.status === 'active' ? 'disabled' : 'active' }); await load() } catch (e) { error.value = (e as Error).message }
}
async function checkSource(source: DataSource): Promise<void> {
  try { await api.checkDataSource(props.knowledgeBaseId, source.id); await load() } catch (e) { error.value = (e as Error).message }
}
async function syncSource(source: DataSource): Promise<void> {
  syncing.value = source.id
  try { await api.syncDataSource(props.knowledgeBaseId, source.id); await load() } catch (e) { error.value = (e as Error).message } finally { syncing.value = null }
}
async function removeSource(source: DataSource): Promise<void> {
  if (!window.confirm(`删除数据源“${source.name}”？`)) return
  try { await api.deleteDataSource(props.knowledgeBaseId, source.id); await load() } catch (e) { error.value = (e as Error).message }
}
async function addMember(): Promise<void> {
  try { await api.upsertKnowledgeBaseMember(props.knowledgeBaseId, memberForm.username, memberForm.role); memberForm.username = ''; await load() } catch (e) { error.value = (e as Error).message }
}
async function removeMember(member: KnowledgeBaseMember): Promise<void> {
  if (!window.confirm(`移除成员“${member.username}”？`)) return
  try { await api.removeKnowledgeBaseMember(props.knowledgeBaseId, member.user_id); await load() } catch (e) { error.value = (e as Error).message }
}

watch(() => props.knowledgeBaseId, load)
onMounted(load)
</script>

<template>
  <div class="sources-page">
    <div class="page-tabs"><button :class="{ active: tab === 'sources' }" @click="tab = 'sources'">数据源连接器</button><button :class="{ active: tab === 'members' }" @click="tab = 'members'">成员与权限</button></div>
    <p v-if="error" class="error">{{ error }}</p>

    <template v-if="tab === 'sources'">
      <section class="intro"><div><h2>数据源连接器</h2><p>保存数据位置和凭据引用。密码、Token 和访问密钥不会直接保存在知识库配置中。</p></div></section>
      <div class="two-column">
        <form class="form-card" @submit.prevent="addSource"><h3>添加数据源</h3><label>来源类型<select v-model="sourceForm.source_type"><option value="git">Git 仓库</option><option value="s3">S3 / MinIO</option><option value="local_directory">本地目录</option><option value="upload">手工上传</option></select></label><label>显示名称<input v-model="sourceForm.name" required placeholder="例如：产品文档仓库" /></label><label>数据位置<input v-model="sourceForm.location" :placeholder="sourceForm.source_type === 'git' ? 'https://git.example/repo.git' : sourceForm.source_type === 's3' ? 's3://bucket/prefix' : '目录位置'" /></label><label>凭据引用<input v-model="sourceForm.connection_ref" placeholder="env://RAG_DOCS_CREDENTIAL 或 file://绝对路径" /></label><label>自动同步间隔（分钟，0 为关闭）<input v-model.number="sourceForm.sync_interval_minutes" type="number" min="0" /></label><label>源端删除策略<select v-model="sourceForm.deletion_policy"><option value="ignore">保留知识库文档</option><option value="delete">同步删除知识库文档</option></select></label><label v-if="sourceForm.source_type==='git'||sourceForm.source_type==='local_directory'" class="check"><input v-model="sourceForm.index_code" type="checkbox" /> 同时构建代码符号与调用图</label><button class="primary">保存连接器</button></form>
        <section class="list-card"><header><strong>已配置 {{ sources.length }} 个数据源</strong><span>本地目录、Git、S3/MinIO 均由持久化 Worker 同步</span></header><div v-if="!sources.length" class="empty">暂无数据源配置</div><article v-for="source in sources" :key="source.id"><div class="source-icon">{{ source.source_type === 'git' ? 'G' : source.source_type === 's3' ? 'S3' : 'D' }}</div><div><strong>{{ source.name }}</strong><p>{{ source.config?.location || '未填写数据位置' }}</p><small>{{ source.source_type }} · {{ source.connection_ref || '无需凭据' }} · {{ source.sync_interval_minutes ? `每 ${source.sync_interval_minutes} 分钟` : '手工同步' }} · 删除策略 {{ source.deletion_policy }}<template v-if="source.health_detail"> · {{ source.health_detail }}</template><template v-if="source.last_sync_detail"> · 最近同步：{{ source.last_sync_detail }}</template></small></div><span :class="['state', source.last_sync_status || source.health_status || source.status]">{{ source.last_sync_status || source.health_status || source.status }}</span><button v-if="source.source_type !== 'upload'" :disabled="syncing === source.id || source.status !== 'active'" @click="syncSource(source)">{{ syncing === source.id ? '提交中…' : '同步' }}</button><button @click="checkSource(source)">检查配置</button><button @click="toggleSource(source)">{{ source.status === 'active' ? '停用' : '启用' }}</button><button class="danger" @click="removeSource(source)">删除</button></article></section>
      </div>
    </template>

    <template v-else>
      <section class="intro"><div><h2>成员与权限</h2><p>Viewer 可以查看和问答，Editor 可以管理文档与图谱；所有者权限不能在这里转移。</p></div></section>
      <div class="two-column">
        <form class="form-card" @submit.prevent="addMember"><h3>添加或更新成员</h3><label>用户名<input v-model="memberForm.username" required placeholder="输入系统中已存在的用户名" /></label><label>角色<select v-model="memberForm.role"><option value="viewer">Viewer · 查看</option><option value="editor">Editor · 编辑</option></select></label><button class="primary">保存成员</button></form>
        <section class="list-card"><header><strong>{{ members.length }} 位成员</strong></header><article v-for="member in members" :key="member.id"><div class="member-avatar">{{ (member.display_name || member.username).slice(0, 1) }}</div><div><strong>{{ member.display_name || member.username }}</strong><p>@{{ member.username }}</p></div><span class="role">{{ member.role }}</span><button v-if="member.role !== 'owner'" class="danger" @click="removeMember(member)">移除</button></article></section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.sources-page { height: 100%; overflow: auto; padding: 20px; }.page-tabs { display: flex; gap: 5px; margin-bottom: 16px; }.page-tabs button { padding: 8px 13px; border: 0; border-radius: 8px; color: #728095; background: transparent; cursor: pointer; }.page-tabs button.active { color: #3f5ee6; background: #eaf0ff; }.error { padding: 9px 12px; color: #b42318; background: #fff1f0; border-radius: 8px; }.intro h2 { margin: 0; font-size: 18px; }.intro p { margin: 5px 0 17px; color: #8491a3; font-size: 12px; }.two-column { display: grid; grid-template-columns: 330px 1fr; gap: 14px; align-items: start; }.form-card,.list-card { padding: 16px; border: 1px solid #e1e7f0; border-radius: 12px; background: white; }.form-card { display: grid; gap: 12px; }.form-card h3 { margin: 0 0 3px; font-size: 14px; }.form-card label { display: grid; gap: 5px; color: #66758a; font-size: 11px; }.form-card input,.form-card select { width: 100%; padding: 9px 10px; border: 1px solid #dce3ed; border-radius: 8px; }.primary { padding: 9px; border: 0; border-radius: 8px; background: #4f6ef7; color: white; cursor: pointer; }.list-card { padding: 0; overflow: hidden; }.list-card > header { display: flex; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid #edf1f6; }.list-card header strong { font-size: 13px; }.list-card header span { color: #97a3b3; font-size: 10px; }.list-card article { display: flex; align-items: center; gap: 11px; padding: 13px 15px; border-top: 1px solid #edf1f6; }.list-card article:first-of-type { border-top: 0; }.source-icon,.member-avatar { display: grid; place-items: center; width: 36px; height: 36px; flex: 0 0 36px; border-radius: 9px; background: #eef2ff; color: #4f6ef7; font-size: 10px; font-weight: 700; }.member-avatar { border-radius: 50%; }.list-card article > div:nth-child(2) { flex: 1; min-width: 0; }.list-card article strong,.list-card article p,.list-card article small { display: block; }.list-card article strong { color: #2b374c; font-size: 12px; }.list-card article p { margin: 3px 0; color: #7d899b; font-size: 11px; }.list-card article small { color: #9ba6b5; font-size: 9px; }.list-card article button { padding: 5px 8px; border: 1px solid #dce3ed; border-radius: 6px; background: white; color: #5e6d82; cursor: pointer; font-size: 10px; }.list-card article button.danger { color: #b42318; }.state,.role { padding: 3px 7px; border-radius: 10px; background: #eef2f7; color: #637188; font-size: 9px; }.state.active,.state.ok { color: #18794e; background: #e9f8ef; }.state.configured { color: #3156cf; background: #eaf0ff; }.state.fail { color: #b42318; background: #fff0ee; }.empty { padding: 45px; text-align: center; color: #97a3b3; font-size: 12px; }
.state.accepted,.state.unchanged { color: #18794e; background: #e9f8ef; }.state.partial { color: #986800; background: #fff6d8; }.state.failed { color: #b42318; background: #fff0ee; }
.form-card label.check { display: flex; align-items: center; gap: 7px; }.form-card label.check input { width: 14px; }
@media(max-width: 900px) { .two-column { grid-template-columns: 1fr; } }
</style>
