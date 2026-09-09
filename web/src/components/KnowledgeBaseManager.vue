<script setup lang="ts">
import { reactive, ref } from 'vue'
import * as api from '../api/client'
import type { KnowledgeBase } from '../types'

const props = withDefaults(defineProps<{ items: KnowledgeBase[]; selectedId: string; canCreate?: boolean }>(), { canCreate: false })
const emit = defineEmits<{
  (event: 'close'): void
  (event: 'changed', selectedId?: string): void
}>()
const mode = ref<'list'|'create'|'edit'>('list')
const editingId = ref('')
const form = reactive({ name: '', description: '' })
const loading = ref(false)
const error = ref('')

function startCreate(): void {
  mode.value = 'create'; editingId.value = ''; Object.assign(form, { name: '', description: '' }); error.value = ''
}
function startEdit(item: KnowledgeBase): void {
  mode.value = 'edit'; editingId.value = item.id; Object.assign(form, { name: item.name, description: item.description || '' }); error.value = ''
}

async function save(): Promise<void> {
  loading.value = true; error.value = ''
  try {
    if (mode.value === 'create') {
      const item = await api.createKnowledgeBase({ name: form.name, description: form.description || null })
      emit('changed', item.id)
    } else {
      await api.updateKnowledgeBase(editingId.value, { name: form.name, description: form.description || null })
      emit('changed', editingId.value)
    }
    mode.value = 'list'
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}
async function archive(item: KnowledgeBase): Promise<void> {
  if (!window.confirm(`归档知识库“${item.name}”？文档和图谱会保留，但普通入口将不再显示。`)) return
  loading.value = true; error.value = ''
  try { await api.archiveKnowledgeBase(item.id); emit('changed', item.id === props.selectedId ? undefined : props.selectedId) }
  catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}
</script>

<template>
  <div class="mask" @click.self="emit('close')">
    <section class="dialog">
      <header><div><strong>知识库管理</strong><small>创建独立知识空间，分别管理文档、图谱、代码与成员</small></div><button @click="emit('close')">×</button></header>
      <p v-if="error" class="error">{{error}}</p>
      <template v-if="mode==='list'">
        <div class="list"><article v-for="item in items" :key="item.id"><div><b>{{item.name}}</b><small>{{item.description||'暂无描述'}} · 独立知识空间</small></div><span v-if="item.is_default">初始库</span><button @click="startEdit(item)">编辑</button><button v-if="!item.is_default" class="danger" @click="archive(item)">归档</button></article></div>
        <footer v-if="canCreate"><button class="primary" @click="startCreate">＋ 创建知识库</button></footer>
      </template>
      <form v-else @submit.prevent="save">
        <label>知识库名称<input v-model="form.name" required maxlength="128" placeholder="例如：航电产品知识库" /></label>
        <label>用途说明<textarea v-model="form.description" maxlength="1000" placeholder="说明这个知识库包含什么、供哪些人员使用" /></label>
        <footer><button type="button" @click="mode='list'">返回</button><button class="primary" :disabled="loading">{{loading?'正在保存…':'保存'}}</button></footer>
      </form>
    </section>
  </div>
</template>

<style scoped>
.mask{position:fixed;z-index:40;inset:0;display:grid;place-items:center;padding:20px;background:#10182780;backdrop-filter:blur(3px)}.dialog{width:min(680px,100%);max-height:85vh;overflow:auto;border:1px solid #dfe5ee;border-radius:16px;background:#fff;box-shadow:0 24px 70px #1018273d}.dialog>header{display:flex;align-items:center;padding:18px 20px;border-bottom:1px solid #edf1f6}.dialog>header div{flex:1}.dialog>header strong,.dialog>header small{display:block}.dialog>header strong{font-size:16px}.dialog>header small{margin-top:3px;color:#8b97a8;font-size:10px}.dialog>header button{border:0;background:transparent;color:#8491a3;font-size:22px;cursor:pointer}.list{padding:8px 20px}.list article{display:flex;align-items:center;gap:8px;padding:13px 0;border-bottom:1px solid #edf1f6}.list article>div{min-width:0;flex:1}.list b,.list small{display:block}.list small{margin-top:4px;overflow:hidden;color:#8995a7;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.list span{padding:3px 7px;border-radius:8px;color:#3155c6;background:#eaf0ff;font-size:9px}form{display:grid;gap:13px;padding:20px}label{display:grid;gap:5px;color:#5d6b80;font-size:11px}input,textarea{box-sizing:border-box;width:100%;padding:10px;border:1px solid #dce3ed;border-radius:8px;font:inherit}textarea{min-height:90px;resize:vertical}label small{color:#98a3b2;font-size:9px}footer{display:flex;justify-content:flex-end;gap:8px;padding:14px 20px;border-top:1px solid #edf1f6}button{padding:7px 10px;border:1px solid #dce3ed;border-radius:7px;color:#526177;background:#fff;cursor:pointer}button.primary{color:#fff;border-color:#4f6ef7;background:#4f6ef7}button.danger{color:#b42318}.error{margin:12px 20px 0;padding:9px;color:#b42318;background:#fff1f0;border-radius:8px;font-size:11px}
</style>
