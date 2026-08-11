<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import * as api from '../api/client'
import type { KnowledgeBase } from '../types'
import { useAuth } from '../composables/useAuth'
import ChatView from './ChatView.vue'
import DocumentsView from './DocumentsView.vue'
import JobsView from './JobsView.vue'
import GraphView from './GraphView.vue'
import ReadinessView from './ReadinessView.vue'
import RetrievalDebugView from './RetrievalDebugView.vue'
import SourcesView from './SourcesView.vue'
import CodeGraphView from './CodeGraphView.vue'
import GraphManagementView from './GraphManagementView.vue'

type Page = 'chat' | 'documents' | 'sources' | 'jobs' | 'graph' | 'graph-management' | 'code-graph' | 'debug' | 'readiness'
const page = ref<Page>('chat')
const knowledgeBases = ref<KnowledgeBase[]>([])
const selectedKnowledgeBaseId = ref('')
const error = ref('')
const { user, logout } = useAuth()

const selectedKnowledgeBase = computed(() =>
  knowledgeBases.value.find((item) => item.id === selectedKnowledgeBaseId.value),
)

onMounted(async () => {
  try {
    knowledgeBases.value = await api.listKnowledgeBases()
    selectedKnowledgeBaseId.value =
      knowledgeBases.value.find((item) => item.is_default)?.id ?? knowledgeBases.value[0]?.id ?? ''
  } catch (e) {
    error.value = (e as Error).message
  }
})

const navigation: Array<{ id: Page; label: string; icon: string }> = [
  { id: 'chat', label: '知识问答', icon: '◈' },
  { id: 'documents', label: '文档管理', icon: '▤' },
  { id: 'sources', label: '数据源与成员', icon: '◇' },
  { id: 'jobs', label: '索引任务', icon: '◷' },
  { id: 'graph', label: '知识图谱', icon: '⌘' },
  { id: 'graph-management', label: '图谱治理', icon: '◎' },
  { id: 'code-graph', label: '代码图谱', icon: '⌗' },
  { id: 'debug', label: '检索调试', icon: '⌕' },
  { id: 'readiness', label: '运行状态', icon: '●' },
]
</script>

<template>
  <div class="workspace">
    <aside class="main-nav">
      <div class="product">
        <span class="product-mark">R</span>
        <div><strong>RAG 工作台</strong><small>Enterprise Ready</small></div>
      </div>
      <nav>
        <button
          v-for="item in navigation"
          :key="item.id"
          :class="{ active: page === item.id }"
          @click="page = item.id"
        ><span>{{ item.icon }}</span>{{ item.label }}</button>
      </nav>
      <div class="account">
        <div class="avatar">{{ (user?.display_name || user?.username || 'U').slice(0, 1) }}</div>
        <div class="account-name"><strong>{{ user?.display_name || user?.username }}</strong><small>{{ user?.role }}</small></div>
        <button class="logout" title="退出" @click="logout">↗</button>
      </div>
    </aside>

    <main class="workspace-main">
      <header class="workspace-header">
        <div>
          <strong>{{ navigation.find((item) => item.id === page)?.label }}</strong>
          <span v-if="selectedKnowledgeBase">{{ selectedKnowledgeBase.description || '集中管理企业知识与检索流程' }}</span>
        </div>
        <label class="kb-select">
          <span>当前知识库</span>
          <select v-model="selectedKnowledgeBaseId">
            <option v-for="kb in knowledgeBases" :key="kb.id" :value="kb.id">{{ kb.name }}</option>
          </select>
        </label>
      </header>
      <p v-if="error" class="workspace-error">{{ error }}</p>
      <section v-if="selectedKnowledgeBaseId" class="page-host">
        <ChatView v-if="page === 'chat'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <DocumentsView v-else-if="page === 'documents'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <SourcesView v-else-if="page === 'sources'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <JobsView v-else-if="page === 'jobs'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <GraphView v-else-if="page === 'graph'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <GraphManagementView v-else-if="page === 'graph-management'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <CodeGraphView v-else-if="page === 'code-graph'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <RetrievalDebugView v-else-if="page === 'debug'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <ReadinessView v-else />
      </section>
      <div v-else class="empty-workspace">暂无可访问的知识库</div>
    </main>
  </div>
</template>

<style scoped>
.workspace { display: flex; height: 100vh; background: #f5f7fb; overflow: hidden; }
.main-nav { width: 204px; flex: 0 0 204px; background: #101827; color: #dbe4f0; padding: 18px 12px 12px; display: flex; flex-direction: column; }
.product { display: flex; align-items: center; gap: 10px; padding: 0 8px 22px; }
.product-mark { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px; background: #4f6ef7; color: white; font-weight: 800; }
.product strong, .product small, .account strong, .account small { display: block; }
.product strong { font-size: 14px; color: white; }.product small { margin-top: 2px; font-size: 9px; letter-spacing: .08em; color: #738096; }
nav { display: grid; gap: 5px; } nav button { display: flex; align-items: center; gap: 11px; width: 100%; border: 0; border-radius: 9px; padding: 10px 12px; background: transparent; color: #9cabc0; cursor: pointer; text-align: left; }
nav button:hover { background: #1a2638; color: white; } nav button.active { background: #263650; color: white; } nav button span { width: 18px; color: #8fa4ff; }
.account { margin-top: auto; display: flex; align-items: center; gap: 9px; padding: 12px 7px 3px; border-top: 1px solid #263247; }.avatar { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; background: #334766; color: white; }.account-name { min-width: 0; flex: 1; }.account-name strong { overflow: hidden; text-overflow: ellipsis; font-size: 12px; color: white; }.account-name small { color: #738096; font-size: 10px; margin-top: 2px; }.logout { border: 0; background: transparent; color: #8796aa; cursor: pointer; font-size: 16px; }
.workspace-main { min-width: 0; flex: 1; display: flex; flex-direction: column; }.workspace-header { height: 66px; flex: 0 0 66px; padding: 10px 22px; display: flex; align-items: center; justify-content: space-between; background: white; border-bottom: 1px solid #e5eaf1; }.workspace-header strong, .workspace-header span { display: block; }.workspace-header strong { font-size: 17px; color: #172033; }.workspace-header div > span { margin-top: 3px; color: #8a96a8; font-size: 11px; }.kb-select { display: flex; align-items: center; gap: 9px; color: #788598; font-size: 11px; }.kb-select select { min-width: 170px; padding: 8px 30px 8px 10px; border: 1px solid #dbe2ec; border-radius: 8px; background: #fff; color: #263247; }
.page-host { min-height: 0; flex: 1; overflow: hidden; }.workspace-error { margin: 0; padding: 8px 20px; color: #b42318; background: #fff1f0; }.empty-workspace { display: grid; place-items: center; flex: 1; color: #8491a5; }
@media (max-width: 760px) { .main-nav { width: 64px; flex-basis: 64px; }.product div, nav button:not(.active)::after, nav button { font-size: 0; }.product { padding-left: 2px; }.product-mark { flex: none; } nav button span { font-size: 16px; }.account-name, .logout { display: none; }.workspace-header div > span, .kb-select > span { display: none; }.kb-select select { min-width: 130px; } }
</style>
