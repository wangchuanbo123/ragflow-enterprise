<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import * as api from '../api/client'
import type { Folder, Tag } from '../types'
const props = defineProps<{ knowledgeBaseId: string; folders: Folder[]; tags: Tag[] }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const kind = ref<'folder'|'tag'>('folder')
const editingId = ref('')
const form = reactive({ name: '', parent_id: '', color: '#4f6ef7' })
const error = ref('')
const busy = ref(false)
const parentChoices = computed(() => {
  const current = props.folders.find(item => item.id === editingId.value)
  return props.folders.filter(item => !current || (item.id !== current.id && !item.path.startsWith(`${current.path}/`)))
})
function edit(type: 'folder'|'tag', item?: Folder|Tag): void {
  kind.value = type; editingId.value = item?.id || ''; error.value = ''
  Object.assign(form, { name: item?.name || '', parent_id: item && 'parent_id' in item ? item.parent_id || '' : '', color: item && 'color' in item ? item.color || '#4f6ef7' : '#4f6ef7' })
}
async function save(): Promise<void> {
  busy.value = true; error.value = ''
  try {
    if (kind.value === 'folder') {
      const payload = { name: form.name.trim(), parent_id: form.parent_id || null }
      if (editingId.value) await api.updateFolder(props.knowledgeBaseId, editingId.value, payload)
      else await api.createFolder(props.knowledgeBaseId, payload)
    } else {
      const payload = { name: form.name.trim(), color: form.color }
      if (editingId.value) await api.updateTag(props.knowledgeBaseId, editingId.value, payload)
      else await api.createTag(props.knowledgeBaseId, payload)
    }
    emit('changed'); edit(kind.value)
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function remove(type: 'folder'|'tag', item: Folder|Tag): Promise<void> {
  const detail = type === 'folder' ? '目录内文档会移至根目录；有子目录时请先移动或删除子目录。' : '文档内容将保留，并解除此标签。'
  if (!window.confirm(`删除${type==='folder'?'文件夹':'标签'}“${item.name}”？${detail}`)) return
  busy.value = true; error.value = ''
  try {
    if (type === 'folder') await api.deleteFolder(props.knowledgeBaseId, item.id)
    else await api.deleteTag(props.knowledgeBaseId, item.id)
    emit('changed'); if (editingId.value === item.id) edit(type)
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>
<template>
  <div class="mask" @click.self="emit('close')"><section class="dialog"><header><h2>文件夹与标签</h2><button @click="emit('close')">关闭</button></header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="catalog"><section><header><b>文件夹</b><button @click="edit('folder')">新建文件夹</button></header><article v-for="item in folders" :key="item.id"><span>{{ item.path || item.name }}</span><button :disabled="busy" @click="edit('folder',item)">编辑</button><button :disabled="busy" @click="remove('folder',item)">删除</button></article><small v-if="!folders.length">尚无文件夹</small></section>
    <section><header><b>标签</b><button @click="edit('tag')">新建标签</button></header><article v-for="item in tags" :key="item.id"><span :style="{color:item.color||undefined}">{{ item.name }}</span><button :disabled="busy" @click="edit('tag',item)">编辑</button><button :disabled="busy" @click="remove('tag',item)">删除</button></article><small v-if="!tags.length">尚无标签</small></section></div>
    <form @submit.prevent="save"><h3>{{ editingId?'编辑':'新建' }}{{ kind==='folder'?'文件夹':'标签' }}</h3><label>名称<input v-model="form.name" required :maxlength="kind==='folder'?128:64" /></label><label v-if="kind==='folder'">上级文件夹<select v-model="form.parent_id"><option value="">根目录</option><option v-for="item in parentChoices" :key="item.id" :value="item.id">{{item.path}}</option></select></label><label v-else>颜色<input v-model="form.color" type="color" /></label><button :disabled="busy||!form.name.trim()">保存{{kind==='folder'?'文件夹':'标签'}}</button></form>
  </section></div>
</template>
<style scoped>
.mask{position:fixed;inset:0;z-index:40;display:grid;place-items:center;padding:20px;background:#10182780}.dialog{width:min(800px,100%);max-height:90vh;overflow:auto;padding:20px;border-radius:14px;background:#fff}header,article{display:flex;align-items:center;gap:8px}h2,header b,article span{flex:1}h2{font-size:18px}.catalog{display:grid;grid-template-columns:1fr 1fr;gap:16px}.catalog section{padding:12px;border:1px solid #e1e7f0;border-radius:10px}article{padding:8px 0;border-bottom:1px solid #edf1f6;font-size:12px}article span{overflow-wrap:anywhere}form{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-top:18px;padding:16px;background:#f6f8fc;border-radius:10px}form h3{width:100%;margin:0;font-size:14px}label{display:grid;gap:5px;font-size:12px}input,select{padding:8px;border:1px solid #dce3ed;border-radius:7px}button{padding:7px 9px;border:1px solid #dce3ed;border-radius:7px;color:#4561d8;background:#f5f7ff;cursor:pointer}button:disabled{opacity:.5}.error{color:#b42318}small{color:#8491a3}@media(max-width:650px){.catalog{grid-template-columns:1fr}}
</style>
