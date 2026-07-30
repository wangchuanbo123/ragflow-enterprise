<script setup lang="ts">
import { ref } from 'vue'
import type { Conversation } from '../types'

defineProps<{
  conversations: Conversation[]
  activeId: string | null
  loading: boolean
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'create'): void
  (e: 'rename', id: string, title: string): void
  (e: 'delete', id: string): void
}>()

const editingId = ref<string | null>(null)
const editingTitle = ref('')
const menuOpenId = ref<string | null>(null)

function startRename(c: Conversation): void {
  editingId.value = c.id
  editingTitle.value = c.title
  menuOpenId.value = null
}

function commitRename(): void {
  const id = editingId.value
  const title = editingTitle.value.trim()
  if (id && title) emit('rename', id, title)
  editingId.value = null
}

function cancelRename(): void {
  editingId.value = null
}

function confirmDelete(id: string): void {
  if (window.confirm('确定删除该会话？删除后无法恢复。')) {
    emit('delete', id)
  }
  menuOpenId.value = null
}
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-header">
      <span class="brand">会话列表</span>
      <button class="new-btn" @click="emit('create')" title="新建会话">+ 新建</button>
    </div>

    <div v-if="loading" class="empty-tip">加载中…</div>
    <div v-else-if="!conversations.length" class="empty-tip">暂无会话</div>

    <ul v-else class="conv-list">
      <li
        v-for="c in conversations"
        :key="c.id"
        :class="['conv-item', { active: c.id === activeId }]"
        @click="editingId !== c.id && emit('select', c.id)"
      >
        <input
          v-if="editingId === c.id"
          v-model="editingTitle"
          class="rename-input"
          @click.stop
          @keyup.enter="commitRename"
          @keyup.esc="cancelRename"
          @blur="commitRename"
        />
        <template v-else>
          <span class="conv-title">{{ c.title }}</span>
          <button class="menu-btn" @click.stop="menuOpenId = menuOpenId === c.id ? null : c.id">⋯</button>
          <div v-if="menuOpenId === c.id" class="menu" @click.stop>
            <button @click="startRename(c)">重命名</button>
            <button class="danger" @click="confirmDelete(c.id)">删除</button>
          </div>
        </template>
      </li>
    </ul>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 240px;
  flex-shrink: 0;
  background: #f7f8fa;
  border-right: 1px solid #e5e7eb;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 14px;
  border-bottom: 1px solid #e5e7eb;
}
.brand {
  font-weight: 600;
  color: #333;
  font-size: 14px;
}
.new-btn {
  border: none;
  background: #667eea;
  color: #fff;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  cursor: pointer;
}
.new-btn:hover {
  background: #5568d3;
}
.empty-tip {
  padding: 20px;
  color: #999;
  text-align: center;
  font-size: 13px;
}
.conv-list {
  list-style: none;
  margin: 0;
  padding: 6px;
  overflow-y: auto;
  flex: 1;
}
.conv-item {
  position: relative;
  display: flex;
  align-items: center;
  padding: 10px 10px;
  border-radius: 8px;
  cursor: pointer;
  color: #444;
  font-size: 14px;
  margin-bottom: 2px;
}
.conv-item:hover {
  background: #eceef1;
}
.conv-item.active {
  background: #e3e7fb;
  color: #2b3a8c;
  font-weight: 500;
}
.conv-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.menu-btn {
  border: none;
  background: transparent;
  color: #999;
  cursor: pointer;
  font-size: 16px;
  padding: 0 4px;
}
.menu {
  position: absolute;
  right: 6px;
  top: 36px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
  z-index: 10;
  overflow: hidden;
}
.menu button {
  border: none;
  background: #fff;
  padding: 8px 14px;
  text-align: left;
  cursor: pointer;
  font-size: 13px;
}
.menu button:hover {
  background: #f3f4f6;
}
.menu button.danger {
  color: #d33;
}
.rename-input {
  flex: 1;
  padding: 4px 6px;
  border: 1px solid #667eea;
  border-radius: 4px;
  font-size: 14px;
  outline: none;
}

@media (max-width: 640px) {
  .sidebar {
    width: 180px;
  }
}
</style>
