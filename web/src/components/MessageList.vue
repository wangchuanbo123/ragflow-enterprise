<script setup lang="ts">
import { ref } from 'vue'
import type { Message } from '../types'

defineProps<{
  messages: Message[]
  loading: boolean
  sending: boolean
}>()

const emit = defineEmits<{ (e: 'retry', content: string): void }>()

const draft = ref('')

function submit(): void {
  const text = draft.value.trim()
  if (!text) return
  emit('retry', text)
  draft.value = ''
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submit()
  }
}

function toggleSources(e: Event): void {
  const btn = e.currentTarget as HTMLElement
  const panel = btn.parentElement?.querySelector('.sources-body') as HTMLElement | null
  if (panel) {
    const open = panel.style.display === 'none' || !panel.style.display
    panel.style.display = open ? 'block' : 'none'
    btn.textContent = (open ? '▾' : '▸') + ' 引用来源'
  }
}
</script>

<template>
  <section class="chat-main">
    <div class="messages">
      <div v-if="loading" class="state-tip">加载历史消息…</div>
      <div v-else-if="!messages.length" class="state-tip">
        开始新的对话，向知识库提问吧。
      </div>

      <template v-else>
        <div
          v-for="m in messages"
          :key="m.id"
          :class="['bubble', m.role]"
        >
          <div class="role">{{ m.role === 'user' ? '我' : '助手' }}</div>
          <!-- 纯文本展示，使用 pre-wrap 保留排版，避免 v-html 注入 -->
          <div class="content">{{ m.content }}</div>

          <div v-if="m.status === 'streaming'" class="cursor">▍</div>
          <div v-if="m.status === 'failed'" class="failed">
            回答生成失败：{{ m.error_message || '请重试' }}
          </div>

          <div v-if="m.sources && m.sources.length" class="sources">
            <button class="sources-toggle" @click="toggleSources">▸ 引用来源</button>
            <div class="sources-body" style="display: none">
              <div v-for="(s, i) in m.sources" :key="i" class="source-item">
                <div class="source-name">{{ i + 1 }}. {{ s.source }}</div>
                <div class="source-preview">{{ s.preview }}</div>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <div class="composer">
      <textarea
        v-model="draft"
        class="input"
        rows="1"
        placeholder="输入问题，Enter 发送，Shift+Enter 换行"
        :disabled="sending"
        @keydown="onKeydown"
      ></textarea>
      <button class="send-btn" :disabled="sending || !draft.trim()" @click="submit">
        {{ sending ? '生成中…' : '发送' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 18px 18px 8px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.state-tip {
  color: #999;
  text-align: center;
  margin-top: 40px;
  font-size: 14px;
}
.bubble {
  max-width: 78%;
  padding: 12px 14px;
  border-radius: 12px;
  line-height: 1.6;
  font-size: 14px;
  word-break: break-word;
}
.bubble.user {
  align-self: flex-end;
  background: #667eea;
  color: #fff;
}
.bubble.assistant {
  align-self: flex-start;
  background: #f1f3f7;
  color: #1f2330;
}
.role {
  font-size: 11px;
  opacity: 0.7;
  margin-bottom: 4px;
}
.content {
  white-space: pre-wrap;
}
.cursor {
  display: inline-block;
  animation: blink 1s steps(2, start) infinite;
  color: #888;
}
@keyframes blink {
  to {
    visibility: hidden;
  }
}
.failed {
  margin-top: 6px;
  color: #d33;
  font-size: 13px;
}
.sources {
  margin-top: 8px;
  font-size: 12px;
}
.sources-toggle {
  border: none;
  background: transparent;
  color: #5568d3;
  cursor: pointer;
  padding: 0;
  font-size: 12px;
}
.sources-body {
  margin-top: 6px;
  border-left: 2px solid #c9d2f5;
  padding-left: 10px;
}
.source-item {
  margin-bottom: 6px;
}
.source-name {
  font-weight: 600;
  color: #444;
}
.source-preview {
  color: #888;
  margin-top: 2px;
}
.composer {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid #e5e7eb;
  background: #fff;
}
.input {
  flex: 1;
  resize: none;
  padding: 10px 12px;
  border: 1px solid #ddd;
  border-radius: 8px;
  font-size: 14px;
  font-family: inherit;
  outline: none;
  max-height: 140px;
}
.input:focus {
  border-color: #667eea;
}
.send-btn {
  border: none;
  background: #667eea;
  color: #fff;
  border-radius: 8px;
  padding: 0 18px;
  font-size: 14px;
  cursor: pointer;
}
.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 640px) {
  .bubble {
    max-width: 92%;
  }
}
</style>
