<script setup lang="ts">
import { computed } from 'vue'
import { navigate, navigationState } from '../composables/useNavigation'
import DocumentGraphManagementView from './DocumentGraphManagementView.vue'
import CodeGraphManagementView from './CodeGraphManagementView.vue'

const props = defineProps<{ knowledgeBaseId: string }>()
const graphKind = computed(() => navigationState.value.graphKind)

function selectGraph(kind: 'document' | 'code'): void {
  navigate({ page: 'graph-management', kb: props.knowledgeBaseId, graphKind: kind })
}
</script>

<template>
  <div class="governance-page">
    <header class="governance-heading">
      <div>
        <h2>图谱治理</h2>
        <p>管理图谱构建、数据质量与检索索引。</p>
      </div>
      <nav class="graph-kind-tabs" role="tablist" aria-label="治理对象">
        <button id="document-governance-tab" role="tab" :aria-selected="graphKind === 'document'" aria-controls="document-governance-panel" :class="{ active: graphKind === 'document' }" @click="selectGraph('document')">文档图谱</button>
        <button id="code-governance-tab" role="tab" :aria-selected="graphKind === 'code'" aria-controls="code-governance-panel" :class="{ active: graphKind === 'code' }" @click="selectGraph('code')">代码图谱</button>
      </nav>
    </header>
    <section :id="`${graphKind}-governance-panel`" class="governance-content" :class="{ 'code-governance-content': graphKind === 'code' }" role="tabpanel" :aria-labelledby="`${graphKind}-governance-tab`">
      <KeepAlive :key="knowledgeBaseId">
        <component :is="graphKind === 'code' ? CodeGraphManagementView : DocumentGraphManagementView" :key="graphKind" :knowledge-base-id="knowledgeBaseId" />
      </KeepAlive>
    </section>
  </div>
</template>

<style scoped>
.governance-page { display: flex; flex-direction: column; height: 100%; min-height: 0; color: #344159; background: #f7f9fc; }
.governance-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 18px 0; }
h2 { margin: 0; font-size: 17px; color: #172033; }
.governance-heading p { margin: 5px 0 0; color: #7a8799; font-size: 12px; }
.graph-kind-tabs { display: flex; flex-shrink: 0; gap: 4px; padding: 4px; border: 1px solid #e1e7f0; border-radius: 10px; background: white; }
.graph-kind-tabs button { padding: 8px 16px; border: 0; border-radius: 7px; color: #68768b; background: transparent; cursor: pointer; }
.graph-kind-tabs button.active { color: white; background: #4f6ef7; }
.governance-content { flex: 1; min-height: 0; overflow: hidden; }
.code-governance-content { overflow: auto; padding: 18px; }
@media (max-width: 650px) { .governance-heading { align-items: flex-start; flex-direction: column; gap: 12px; } }
</style>
