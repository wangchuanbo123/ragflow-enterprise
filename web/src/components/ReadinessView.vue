<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import * as api from '../api/client'
import type { Readiness } from '../types'

const readiness = ref<Readiness | null>(null)
const loading = ref(false)
const error = ref('')
const entries = computed(() => Object.entries(readiness.value?.components ?? {}))
const labels: Record<string, string> = { database: '元数据数据库', object_store: '对象存储', task_queue: '任务队列', cache: '缓存', vector_store: '向量库', model_profiles: '模型配置', ollama: '本地模型服务', index_worker: '索引 Worker' }

async function load(): Promise<void> {
  loading.value = true
  try { readiness.value = await api.getReadiness(); error.value = '' } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
onMounted(load)
</script>

<template>
  <div class="ready-page">
    <div class="ready-head"><div><h2>运行状态</h2><p>验证模型、数据库、向量库、对象存储与任务执行器是否可用。</p></div><button @click="load">重新检查</button></div>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="loading" class="loading">正在检查各组件…</div>
    <template v-else-if="readiness">
      <section :class="['summary', readiness.status]"><div class="pulse"></div><div><strong>{{ readiness.status === 'ready' ? '系统已就绪' : '系统尚未完全就绪' }}</strong><p>检查时间：{{ new Date(readiness.checked_at).toLocaleString() }}</p></div></section>
      <div class="component-grid">
        <article v-for="[name, component] in entries" :key="name">
          <header><span>{{ labels[name] || name }}</span><b :class="component.status">{{ component.status }}</b></header>
          <p>{{ component.detail || (component.status === 'ok' ? '连接与配置正常' : '需要检查配置或服务状态') }}</p>
          <details v-if="name === 'model_profiles' && component.profiles"><summary>查看模型角色</summary><pre>{{ JSON.stringify(component.profiles, null, 2) }}</pre></details>
        </article>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ready-page { height: 100%; overflow: auto; padding: 22px; }.ready-head { display: flex; justify-content: space-between; align-items: center; }.ready-head h2 { margin: 0; font-size: 18px; }.ready-head p { margin: 5px 0 0; color: #8b97a8; font-size: 12px; }.ready-head button { padding: 8px 12px; border: 1px solid #dce3ed; border-radius: 8px; background: white; cursor: pointer; }.error { color: #b42318; }.loading { padding: 50px; text-align: center; color: #8c98aa; }.summary { display: flex; align-items: center; gap: 13px; margin: 20px 0; padding: 18px; border: 1px solid #e1e7f0; border-radius: 12px; background: white; }.summary.ready { border-color: #bde5cc; background: #f4fcf7; }.summary .pulse { width: 13px; height: 13px; border-radius: 50%; background: #e15241; box-shadow: 0 0 0 6px #fbe4e0; }.summary.ready .pulse { background: #24a467; box-shadow: 0 0 0 6px #dff5e8; }.summary strong { color: #243047; }.summary p { margin: 4px 0 0; color: #8b97a8; font-size: 11px; }.component-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }.component-grid article { min-height: 120px; padding: 15px; border: 1px solid #e1e7f0; border-radius: 11px; background: white; }.component-grid header { display: flex; justify-content: space-between; color: #2b374d; font-size: 13px; }.component-grid b { padding: 2px 7px; border-radius: 9px; background: #fff0ee; color: #b42318; font-size: 9px; text-transform: uppercase; }.component-grid b.ok { background: #e9f8ef; color: #18794e; }.component-grid p { color: #8491a3; font-size: 11px; line-height: 1.5; }.component-grid summary { color: #4f6ef7; cursor: pointer; font-size: 10px; }.component-grid pre { max-height: 200px; overflow: auto; padding: 8px; background: #f6f8fb; font-size: 9px; }.error { padding: 9px; background: #fff1f0; border-radius: 8px; }
</style>
