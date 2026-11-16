<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, provide, ref, watch } from 'vue'
import * as api from '../api/client'
import type { KnowledgeBase } from '../types'
import { useAuth } from '../composables/useAuth'
import ChatView from './ChatView.vue'
import DocumentsView from './DocumentsView.vue'
import JobsView from './JobsView.vue'
import ReadinessView from './ReadinessView.vue'
import RetrievalDebugView from './RetrievalDebugView.vue'
import SearchView from './SearchView.vue'
import SourcesView from './SourcesView.vue'
import CodeGraphView from './CodeGraphView.vue'
import GraphManagementView from './GraphManagementView.vue'
import BackupsView from './BackupsView.vue'
import KnowledgeBaseManager from './KnowledgeBaseManager.vue'
import OnboardingWizard from './OnboardingWizard.vue'
import AccountView from './AccountView.vue'
import UsersView from './UsersView.vue'
import AgentWorkbenchView from './AgentWorkbenchView.vue'
import { knowledgeBaseKey } from '../composables/usePermissions'
import { navigate, navigationState, type Page } from '../composables/useNavigation'
import { resetSessionRequests } from '../api/session'
import { useConversations } from '../composables/useConversations'

// 图谱引擎体积较大，仅在用户进入知识图谱页面时加载。
const GraphView = defineAsyncComponent(() => import('./GraphView.vue'))

const page = ref<Page>(navigationState.value.page)
const knowledgeBases = ref<KnowledgeBase[]>([])
const selectedKnowledgeBaseId = ref('')
const error = ref('')
const showKnowledgeBaseManager = ref(false)
const showOnboarding = ref(false)
const { user, logout } = useAuth()

const selectedKnowledgeBase = computed(() =>
  knowledgeBases.value.find((item) => item.id === selectedKnowledgeBaseId.value),
)
provide(knowledgeBaseKey, selectedKnowledgeBase)
const canEdit = computed(() => selectedKnowledgeBase.value?.capabilities?.can_edit === true)
const visibleNavigation = computed(() => navigation.filter(item => (item.id !== 'graph-management' || canEdit.value) && (item.id !== 'users' || user.value?.role === 'admin')))
const initialized = ref(false)

async function signOut(): Promise<void> {
  try { await logout() } catch (e) { error.value = (e as Error).message }
}

async function loadKnowledgeBases(preferredId?: string): Promise<void> {
  try {
    knowledgeBases.value = await api.listKnowledgeBases()
    error.value = ''
    selectedKnowledgeBaseId.value =
      knowledgeBases.value.find((item) => item.id === preferredId)?.id ??
      knowledgeBases.value.find((item) => item.id === selectedKnowledgeBaseId.value)?.id ??
      knowledgeBases.value.find((item) => item.is_default)?.id ?? knowledgeBases.value[0]?.id ?? ''
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function considerOnboarding(force = false): Promise<void> {
  const id = selectedKnowledgeBaseId.value
  if (!id || !canEdit.value) return
  try {
    const documents = await api.listDocuments(id)
    const dismissed = window.localStorage.getItem(`rag-onboarding-dismissed:${id}`)
    showOnboarding.value = documents.length === 0 && (force || !dismissed)
  } catch { /* 文档页会呈现更具体的服务错误。 */ }
}

function closeOnboarding(): void {
  if (selectedKnowledgeBaseId.value) {
    window.localStorage.setItem(`rag-onboarding-dismissed:${selectedKnowledgeBaseId.value}`, '1')
  }
  showOnboarding.value = false
}

function navigateFromOnboarding(target: 'documents'|'jobs'|'chat'): void {
  page.value = target
  closeOnboarding()
}

async function onKnowledgeBasesChanged(preferredId?: string): Promise<void> {
  await loadKnowledgeBases(preferredId)
  showKnowledgeBaseManager.value = false
  if (preferredId) await considerOnboarding(true)
}

onMounted(async () => {
  await loadKnowledgeBases(navigationState.value.kb)
  initialized.value = true
  if (page.value === 'users' && user.value?.role !== 'admin') page.value = 'chat'
  if (page.value === 'graph-management' && !canEdit.value) page.value = 'graph'
  navigate({ page: page.value, kb: selectedKnowledgeBaseId.value }, true)
  await considerOnboarding()
})

watch([page, selectedKnowledgeBaseId], ([nextPage, kb], previous) => {
  if (!initialized.value) return
  resetSessionRequests()
  if (previous && previous[1] !== kb) {
    const conv = useConversations()
    conv.stop(); conv.activeId.value = null; conv.resetDetail()
    showOnboarding.value = false
    void considerOnboarding()
  }
  if (nextPage === 'graph-management' && !canEdit.value) { page.value = 'graph'; return }
  const restoringLocation = navigationState.value.page === nextPage && navigationState.value.kb === kb
  if (!restoringLocation) navigate({ page: nextPage, kb, ...(previous && previous[1] !== kb ? { conversation: '', query: '',agentRun:'',document:'' } : {}) })
})
watch(navigationState, route => {
  if (!initialized.value) return
  page.value = route.page === 'users' && user.value?.role !== 'admin' ? 'chat' : route.page
  if (knowledgeBases.value.some(item => item.id === route.kb)) selectedKnowledgeBaseId.value = route.kb
})

const navigation: Array<{ id: Page; label: string; icon: string }> = [
  { id: 'chat', label: '知识问答', icon: '◈' },
  { id: 'agents', label: '知识智能体', icon: '✦' },
  { id: 'search', label: '全文搜索', icon: '⌕' },
  { id: 'documents', label: '文档管理', icon: '▤' },
  { id: 'sources', label: '数据源与成员', icon: '◇' },
  { id: 'jobs', label: '索引任务', icon: '◷' },
  { id: 'graph', label: '知识图谱', icon: '⌘' },
  { id: 'graph-management', label: '图谱治理', icon: '◎' },
  { id: 'code-graph', label: '代码图谱', icon: '⌗' },
  { id: 'backups', label: '备份与恢复', icon: '↶' },
  { id: 'debug', label: '检索调试', icon: '⌕' },
  { id: 'readiness', label: '运行状态', icon: '●' },
  { id: 'users', label: '用户管理', icon: '♙' },
  { id: 'account', label: '账号设置', icon: '⚙' },
]
const navigationGroups = computed(() => [
  {label:'日常使用', ids:['chat','search','documents','graph']},
  {label:'知识维护', ids:['agents','sources','jobs','graph-management','code-graph']},
  {label:'系统管理', ids:['backups','debug','readiness','users','account']},
].map(group=>({...group,items:group.ids.map(id=>visibleNavigation.value.find(item=>item.id===id)).filter((item):item is typeof navigation[number]=>Boolean(item))})))
</script>

<template>
  <div class="workspace">
    <aside class="main-nav">
      <div class="product">
        <span class="product-mark">R</span>
        <div><strong>RAG 工作台</strong><small>知识库</small></div>
      </div>
      <nav>
        <template v-for="group in navigationGroups" :key="group.label"><div class="nav-group-label">{{group.label}}</div><button
          v-for="item in group.items"
          :key="item.id"
          :class="{ active: page === item.id }"
          :title="item.label"
          :aria-label="item.label"
          @click="page = item.id"
        ><span>{{ item.icon }}</span>{{ item.label }}</button></template>
      </nav>
      <div class="account">
        <div class="avatar">{{ (user?.display_name || user?.username || 'U').slice(0, 1) }}</div>
        <div class="account-name"><strong>{{ user?.display_name || user?.username }}</strong><small>{{ user?.role }}</small></div>
        <button class="logout" title="退出" @click="signOut">↗</button>
      </div>
    </aside>

    <main class="workspace-main">
      <header class="workspace-header">
        <div>
          <strong>{{ navigation.find((item) => item.id === page)?.label }}</strong>
          <span v-if="selectedKnowledgeBase">{{ selectedKnowledgeBase.description || '集中管理企业知识与检索流程' }}</span>
        </div>
        <div class="kb-tools"><label class="kb-select">
          <span>当前知识库</span>
          <select v-model="selectedKnowledgeBaseId">
            <option v-for="kb in knowledgeBases" :key="kb.id" :value="kb.id">{{ kb.name }}</option>
          </select>
        </label><button class="header-button" @click="showKnowledgeBaseManager=true">管理</button><button v-if="canEdit" class="header-button primary" @click="showOnboarding=true">快速开始</button></div>
      </header>
      <p v-if="error" class="workspace-error">{{ error }}</p>
      <section v-if="page==='account'" class="page-host"><AccountView /></section>
      <section v-else-if="page==='users' && user?.role==='admin'" class="page-host"><UsersView /></section>
      <section v-else-if="selectedKnowledgeBaseId" :key="selectedKnowledgeBaseId" class="page-host">
        <ChatView v-if="page === 'chat'" :knowledge-base-id="selectedKnowledgeBaseId" :knowledge-bases="knowledgeBases" />
        <AgentWorkbenchView v-else-if="page === 'agents'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <SearchView v-else-if="page === 'search'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <DocumentsView v-else-if="page === 'documents'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <SourcesView v-else-if="page === 'sources'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <JobsView v-else-if="page === 'jobs'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <GraphView v-else-if="page === 'graph'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <GraphManagementView v-else-if="page === 'graph-management'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <CodeGraphView v-else-if="page === 'code-graph'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <BackupsView v-else-if="page === 'backups'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <RetrievalDebugView v-else-if="page === 'debug'" :knowledge-base-id="selectedKnowledgeBaseId" />
        <ReadinessView v-else />
      </section>
      <div v-else class="empty-workspace">暂无可访问的知识库</div>
    </main>
    <KnowledgeBaseManager v-if="showKnowledgeBaseManager" :items="knowledgeBases" :selected-id="selectedKnowledgeBaseId" :can-create="user?.role==='admin'" @close="showKnowledgeBaseManager=false" @changed="onKnowledgeBasesChanged" />
    <OnboardingWizard v-if="showOnboarding&&selectedKnowledgeBase&&canEdit" :key="selectedKnowledgeBaseId" :knowledge-base-id="selectedKnowledgeBaseId" :knowledge-base-name="selectedKnowledgeBase.name" @close="closeOnboarding" @navigate="navigateFromOnboarding" />
  </div>
</template>

<style scoped>
.nav-group-label{padding:13px 14px 5px;color:#8e9bb1;font-size:10px;letter-spacing:1px}
.workspace { display: flex; height: 100vh; background: #f5f7fb; overflow: hidden; }
.main-nav { width: 204px; flex: 0 0 204px; background: #101827; color: #dbe4f0; padding: 18px 12px 12px; display: flex; flex-direction: column; }
.product { display: flex; align-items: center; gap: 10px; padding: 0 8px 22px; }
.product-mark { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px; background: #4f6ef7; color: white; font-weight: 800; }
.product strong, .product small, .account strong, .account small { display: block; }
.product strong { font-size: 14px; color: white; }.product small { margin-top: 2px; font-size: 9px; letter-spacing: .08em; color: #738096; }
nav { display: grid; gap: 5px; overflow-y:auto; min-height:0; } nav button { display: flex; align-items: center; gap: 11px; width: 100%; border: 0; border-radius: 9px; padding: 10px 12px; background: transparent; color: #9cabc0; cursor: pointer; text-align: left; }
nav button:hover { background: #1a2638; color: white; } nav button.active { background: #263650; color: white; } nav button span { width: 18px; color: #8fa4ff; }
.account { flex-shrink:0; margin-top: auto; display: flex; align-items: center; gap: 9px; padding: 12px 7px 3px; border-top: 1px solid #263247; }.avatar { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; background: #334766; color: white; }.account-name { min-width: 0; flex: 1; }.account-name strong { overflow: hidden; text-overflow: ellipsis; font-size: 12px; color: white; }.account-name small { color: #738096; font-size: 10px; margin-top: 2px; }.logout { border: 0; background: transparent; color: #8796aa; cursor: pointer; font-size: 16px; }
.workspace-main { min-width: 0; flex: 1; display: flex; flex-direction: column; }.workspace-header { height: 66px; flex: 0 0 66px; padding: 10px 22px; display: flex; align-items: center; justify-content: space-between; background: white; border-bottom: 1px solid #e5eaf1; }.workspace-header strong, .workspace-header span { display: block; }.workspace-header strong { font-size: 17px; color: #172033; }.workspace-header div > span { margin-top: 3px; color: #8a96a8; font-size: 11px; }.kb-select { display: flex; align-items: center; gap: 9px; color: #788598; font-size: 11px; }.kb-select select { min-width: 170px; padding: 8px 30px 8px 10px; border: 1px solid #dbe2ec; border-radius: 8px; background: #fff; color: #263247; }
.kb-tools{display:flex;align-items:center;gap:7px}.header-button{padding:8px 10px;border:1px solid #dbe2ec;border-radius:8px;color:#526177;background:#fff;cursor:pointer;font-size:11px}.header-button.primary{color:#fff;border-color:#4f6ef7;background:#4f6ef7}
.page-host { min-height: 0; flex: 1; overflow: hidden; }.workspace-error { margin: 0; padding: 8px 20px; color: #b42318; background: #fff1f0; }.empty-workspace { display: grid; place-items: center; flex: 1; color: #8491a5; }
@media (max-width: 760px) { .main-nav { width: 64px; flex-basis: 64px; }.product div, nav button:not(.active)::after, nav button { font-size: 0; }.product { padding-left: 2px; }.product-mark { flex: none; } nav button span { font-size: 16px; }.account-name { display: none; }.account{flex-direction:column}.logout{font-size:18px}.workspace-header div > span, .kb-select > span { display: none; }.kb-select select { min-width: 110px; }.header-button.primary{display:none} }
</style>
