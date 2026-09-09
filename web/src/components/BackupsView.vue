<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import * as api from '../api/client'
import type { BackupSettings, KnowledgeBackup, KnowledgeDocument } from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const backups = ref<KnowledgeBackup[]>([])
const documents = ref<KnowledgeDocument[]>([])
const settings = ref<BackupSettings | null>(null)
const documentId = ref('')
const label = ref('')
const loading = ref(false)
const error = ref('')
const message = ref('')

const documentNames = computed(() => Object.fromEntries(documents.value.map((item) => [item.id, item.original_filename])))

async function load(): Promise<void> {
  loading.value = true
  try {
    ;[backups.value, documents.value, settings.value] = await Promise.all([
      api.listBackups(props.knowledgeBaseId), api.listDocuments(props.knowledgeBaseId), api.getBackupSettings(props.knowledgeBaseId),
    ])
    if (!documents.value.some((item) => item.id === documentId.value)) documentId.value = documents.value[0]?.id ?? ''
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function create(type: 'knowledge_base' | 'document_graph'): Promise<void> {
  if (type === 'document_graph' && !documentId.value) return
  loading.value = true
  try {
    const backup = await api.createBackup(props.knowledgeBaseId, {
      backup_type: type,
      document_id: type === 'document_graph' ? documentId.value : null,
      label: label.value.trim() || null,
    })
    message.value = `备份已完成：v${backup.version_no}`
    label.value = ''
    await load()
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function restore(item: KnowledgeBackup): Promise<void> {
  const scope = item.backup_type === 'knowledge_base' ? '整个知识库' : `文档图谱“${documentNames.value[item.document_id || ''] || item.document_id}”`
  if (!window.confirm(`确定把${scope}回退到 v${item.version_no}？\n系统会先自动生成一份“回退前保护”备份。`)) return
  loading.value = true
  try {
    await api.restoreBackup(props.knowledgeBaseId, item.id)
    message.value = `已回退到 v${item.version_no}，受影响的检索索引正在自动重建。`
    await load()
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function remove(item: KnowledgeBackup): Promise<void> {
  if (!window.confirm(`永久删除备份 v${item.version_no}？该版本删除后不能用于回退。`)) return
  loading.value = true
  try { await api.deleteBackup(props.knowledgeBaseId, item.id); message.value = `已删除备份 v${item.version_no}`; await load() }
  catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

function size(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function triggerName(trigger: KnowledgeBackup['trigger']): string {
  return { manual: '手工', monthly: '每月自动', pre_restore: '回退前保护' }[trigger]
}

watch(() => props.knowledgeBaseId, load)
onMounted(load)
</script>

<template>
  <div class="backup-page">
    <section class="intro">
      <div><h2>备份与恢复</h2><p>知识库备份包含文档、Chunk、Schema、文档图谱和代码图谱；文档图谱备份只保存所选文档的实体、关系及人工修订。</p></div>
      <span :class="{ enabled: settings?.monthly_enabled }">{{ settings?.monthly_enabled ? `每 ${settings.interval_days} 天自动备份` : '自动备份已关闭' }}</span>
    </section>
    <p v-if="error" class="error">{{ error }}</p><p v-if="message" class="message">{{ message }}</p>

    <div class="actions">
      <section><header><b>整个知识库</b><small>完整灾备和整体回退</small></header><label>版本备注（可选）<input v-model="label" placeholder="例如：上线前稳定版本" /></label><button :disabled="loading" @click="create('knowledge_base')">立即备份整个知识库</button></section>
      <section><header><b>单文档知识图谱</b><small>只保护一个文档的实体、关系和证据</small></header><label>选择来源文档<select v-model="documentId"><option v-for="document in documents" :key="document.id" :value="document.id">{{ document.original_filename }}</option></select></label><button :disabled="loading || !documentId" @click="create('document_graph')">备份所选文档图谱</button></section>
    </div>

    <section class="versions">
      <header><div><b>备份版本</b><small>自动备份最多保留 {{ settings?.retention_versions ?? 12 }} 个；手工备份仅由管理员删除</small></div><button @click="load">刷新</button></header>
      <div v-if="!backups.length" class="empty">还没有备份，建议先创建一份整个知识库备份。</div>
      <article v-for="item in backups" :key="item.id">
        <div :class="['type', item.backup_type]"><span>{{ item.backup_type === 'knowledge_base' ? 'KB' : '图' }}</span></div>
        <div class="main"><b>{{ item.backup_type === 'knowledge_base' ? '整个知识库' : (documentNames[item.document_id || ''] || '文档图谱') }} · v{{ item.version_no }}</b><p>{{ item.label || '未填写版本备注' }}</p><small>{{ triggerName(item.trigger) }} · {{ new Date(item.created_at).toLocaleString() }} · {{ size(item.file_size) }}<template v-if="item.restored_at"> · 已于 {{ new Date(item.restored_at).toLocaleString() }} 回退</template></small></div>
        <span class="ready">{{ item.status === 'ready' ? '可恢复' : item.status }}</span>
        <button :disabled="loading" @click="restore(item)">回退到此版本</button><button class="danger" :disabled="loading" @click="remove(item)">删除</button>
      </article>
    </section>
  </div>
</template>

<style scoped>
.backup-page{height:100%;overflow:auto;padding:20px;color:#334159}.intro{display:flex;align-items:center;justify-content:space-between}.intro h2{margin:0;font-size:18px}.intro p{max-width:850px;margin:5px 0 16px;color:#8491a3;font-size:11px}.intro>span{padding:6px 10px;border-radius:12px;color:#7a8799;background:#eef2f7;font-size:10px}.intro>span.enabled{color:#18794e;background:#eaf8f0}.error,.message{padding:9px 12px;border-radius:8px;font-size:11px}.error{color:#b42318;background:#fff1f0}.message{color:#18794e;background:#ecf9f1}.actions{display:grid;grid-template-columns:1fr 1fr;gap:12px}.actions section,.versions{border:1px solid #e1e7f0;border-radius:12px;background:white}.actions section{display:grid;grid-template-columns:1fr auto;gap:12px;padding:15px}.actions header{grid-column:1/-1}.actions b,.actions small{display:block}.actions b{font-size:13px}.actions small{margin-top:3px;color:#8b97a8;font-size:9px}.actions label{display:grid;gap:5px;color:#6f7d90;font-size:10px}.actions input,.actions select{box-sizing:border-box;width:100%;padding:8px;border:1px solid #dce3ed;border-radius:7px}.actions button{align-self:end;padding:9px 12px;border:0;border-radius:8px;color:white;background:#4f6ef7;cursor:pointer}.actions button:disabled{opacity:.5}.versions{margin-top:13px;overflow:hidden}.versions>header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid #edf1f6}.versions header b,.versions header small{display:block}.versions header b{font-size:13px}.versions header small{margin-top:3px;color:#8c98a9;font-size:9px}.versions>header button{border:0;color:#4f6ef7;background:transparent;cursor:pointer}.versions article{display:flex;align-items:center;gap:11px;padding:12px 15px;border-top:1px solid #edf1f6}.versions article:first-of-type{border-top:0}.type{display:grid;place-items:center;width:36px;height:36px;flex:0 0 36px;border-radius:9px;color:#3156cf;background:#eaf0ff;font-size:10px;font-weight:700}.type.document_graph{color:#7652ad;background:#f3edff}.main{min-width:0;flex:1}.main b,.main p,.main small{display:block}.main b{font-size:11px}.main p{margin:3px 0;color:#748196;font-size:10px}.main small{color:#98a3b2;font-size:9px}.ready{padding:3px 7px;border-radius:9px;color:#18794e;background:#eaf8f0;font-size:9px}.versions article button{padding:6px 8px;border:1px solid #dce3ed;border-radius:7px;color:#506076;background:white;cursor:pointer;font-size:9px}.versions article button.danger{color:#b42318}.empty{padding:45px;text-align:center;color:#98a4b3;font-size:11px}@media(max-width:850px){.actions{grid-template-columns:1fr}.versions article{align-items:flex-start;flex-wrap:wrap}.main{min-width:calc(100% - 60px)}}
</style>
