<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as api from '../api/client'
import type { Folder, KnowledgeBase, KnowledgeDocument, RetrievalScope, RetrievalStrategy, Tag } from '../types'
import { useConversations } from '../composables/useConversations'
import ConversationSidebar from './ConversationSidebar.vue'
import MessageList from './MessageList.vue'
import { navigate, navigationState } from '../composables/useNavigation'

const props = defineProps<{ knowledgeBaseId: string; knowledgeBases: KnowledgeBase[] }>()
const conv = useConversations()
let disposed = false
onBeforeUnmount(() => { disposed = true })
const documents = ref<KnowledgeDocument[]>([])
const folders = ref<Folder[]>([])
const tags = ref<Tag[]>([])
const visibleConversations = computed(() =>
  conv.conversations.value.filter((item) => item.knowledge_base_id === props.knowledgeBaseId),
)

async function loadScopeOptions():Promise<void>{
  try { ;[documents.value,folders.value,tags.value]=await Promise.all([
    api.listDocuments(props.knowledgeBaseId),api.listFolders(props.knowledgeBaseId),api.listTags(props.knowledgeBaseId),
  ]) } catch (error) { if ((error as Error).name !== 'AbortError') conv.errorMsg.value = (error as Error).message }
}

onMounted(async () => {
  await conv.loadList()
  if (disposed) return
  const id = navigationState.value.conversation
  if (id && visibleConversations.value.some(item => item.id === id)) await conv.select(id)
  else if (!visibleConversations.value.some(item => item.id === conv.activeId.value)) { conv.activeId.value = null; conv.resetDetail() }
  void loadScopeOptions()
})
watch(conv.activeId, id => {
  if (navigationState.value.page === 'chat' && navigationState.value.kb === props.knowledgeBaseId) navigate({ conversation: id || '' })
})
watch(() => navigationState.value.conversation, id => {
  if (id && id !== conv.activeId.value && visibleConversations.value.some(item => item.id === id)) void conv.select(id)
  else if (!id && conv.activeId.value) { conv.activeId.value = null; conv.resetDetail() }
})

function onSend(payload: { content: string; strategy: RetrievalStrategy; scope?: Partial<RetrievalScope> }): Promise<boolean> {
  return conv.send(payload.content, payload.strategy, payload.scope, props.knowledgeBaseId)
}

function onCreate(): void {
  void conv.create(props.knowledgeBaseId).catch(() => {})
}

watch(() => props.knowledgeBaseId, () => {
  const active = conv.conversations.value.find((item) => item.id === conv.activeId.value)
  if (active?.knowledge_base_id !== props.knowledgeBaseId) {
    conv.activeId.value = null
    conv.resetDetail()
  }
  void loadScopeOptions()
})
</script>

<template>
  <div class="chat-layout">
    <ConversationSidebar
      :conversations="visibleConversations"
      :active-id="conv.activeId.value"
      :loading="conv.loadingList.value"
      @select="conv.select"
      @create="onCreate"
      @rename="conv.rename"
      @delete="conv.remove"
    />

    <div class="chat-area">
      <header class="topbar">
        <span class="topbar-title">
          {{ conv.activeId.value ? '当前会话' : '企业知识库助手' }}
        </span>
        <span class="topbar-hint">同步问答与流式问答使用同一检索工作流</span>
      </header>

      <p v-if="conv.errorMsg.value" class="global-error">{{ conv.errorMsg.value }}</p>

      <MessageList
        :messages="conv.messages.value"
        :loading="conv.loadingDetail.value"
        :sending="conv.sending.value"
        :conversation-id="conv.activeId.value"
        :knowledge-base-id="knowledgeBaseId"
        :knowledge-bases="knowledgeBases"
        :documents="documents"
        :folders="folders"
        :tags="tags"
        :submit-message="onSend"
        @stop="conv.stop"
        @feedback="({messageId,rating})=>conv.feedback(messageId,rating)"
      />
    </div>
  </div>
</template>

<style scoped>
.chat-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}
.chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid #e5e7eb;
  background: #fff;
}
.topbar-title {
  font-weight: 600;
  color: #333;
  font-size: 15px;
}
.topbar-hint { color: #9aa4b2; font-size: 11px; }
.global-error {
  margin: 0;
  padding: 8px 16px;
  background: #fff3f3;
  color: #d33;
  font-size: 13px;
  border-bottom: 1px solid #ffd6d6;
}

@media (max-width: 640px) {
  .topbar-title {
    font-size: 13px;
  }
}
</style>
