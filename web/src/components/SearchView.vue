<script setup lang="ts">
import { onMounted, ref } from 'vue'
import * as api from '../api/client'
import type { SearchHit } from '../types'
import { navigate, navigationState } from '../composables/useNavigation'
import { pageLabel } from '../documentPresentation'

const props = defineProps<{ knowledgeBaseId: string }>()

const query = ref(navigationState.value.query)
const hits = ref<SearchHit[]>([])
const searching = ref(false)
const searched = ref(false)
const error = ref('')
const activeDetail = ref<SearchHit | null>(null)

async function search(): Promise<void> {
  const text = query.value.trim()
  if (!text) return
  searching.value = true
  error.value = ''
  try {
    hits.value = await api.searchKnowledgeBase(props.knowledgeBaseId, text, 30)
    searched.value = true
    navigate({ query: text }, true)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    searching.value = false
  }
}

/** 拆分 snippet，将查询词高亮。 */
function highlightSnippet(snippet: string, keywords: string[]): Array<{ text: string; hit: boolean }> {
  const terms = keywords.map((t) => t.trim()).filter(Boolean)
  if (!terms.length) return [{ text: snippet, hit: false }]
  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const pattern = new RegExp(`(${escaped.join('|')})`, 'gi')
  const parts = snippet.split(pattern)
  return parts.filter((p) => p !== '').map((part) => ({ text: part, hit: pattern.test(part) }))
}

function onEnter(): void { void search() }

function imageUrl(image: { url: string }): string {
  if (/^https?:\/\//i.test(image.url)) return image.url
  if (image.url.startsWith('/')) return image.url
  return `/api/v1/objects/${encodeURIComponent(image.url)}`
}

function locationLabel(hit: SearchHit): string {
  return [hit.section, pageLabel(hit.page)].filter(Boolean).join(' · ')
}

const previewImage = ref<{ url: string; alt: string } | null>(null)
onMounted(() => { if (query.value.trim()) void search() })
</script>

<template>
  <div class="search-page">
    <div class="search-bar">
      <input v-model="query" class="search-input" placeholder="搜索知识库全文，例如：部署架构 或 权限模型" @keydown.enter="onEnter" />
      <button class="search-btn" :disabled="searching || !query.trim()" @click="search">{{ searching ? '搜索中…' : '搜索' }}</button>
    </div>
    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="!searched && !searching" class="empty">
      <div class="empty-icon">⌕</div>
      <strong>全文搜索</strong>
      <p>直接检索文档原文片段，快速定位内容位置。搜索基于 BM25 关键词索引，与问答检索共用同一套索引。</p>
    </div>

    <div v-else class="results">
      <p class="result-meta">{{ searching ? '正在搜索…' : `${hits.length} 条结果 · 相关度排序` }}</p>
      <div v-if="!hits.length && !searching" class="no-result">没有找到相关内容，试试更换关键词或减少限定词。</div>
      <article v-for="hit in hits" :key="hit.chunk_id" class="hit-card" @click="activeDetail = hit">
        <header>
          <strong class="hit-doc">{{ hit.document_name }}</strong>
          <small v-if="locationLabel(hit)" class="hit-location">{{ locationLabel(hit) }}</small>
          <small class="hit-chunk">#{{ hit.chunk_index }}</small>
        </header>
        <p class="hit-snippet">
          <template v-for="(part, index) in highlightSnippet(hit.snippet, query.split(/\s+/))" :key="index">
            <mark v-if="part.hit">{{ part.text }}</mark><template v-else>{{ part.text }}</template>
          </template>
        </p>
        <footer>
          <span class="hit-score">BM25 {{ hit.score.toFixed(2) }}</span>
          <span v-if="hit.images.length" class="hit-images">{{ hit.images.length }} 张图</span>
          <button class="hit-expand" @click.stop="activeDetail = hit">查看全文</button>
        </footer>
        <div v-if="hit.images.length" class="hit-thumbs">
          <img v-for="(image, index) in hit.images" :key="index" :src="imageUrl(image)" :alt="image.caption || image.filename" :title="image.caption || image.filename" loading="lazy" @click.stop="previewImage = { url: imageUrl(image), alt: image.caption || image.filename }" />
        </div>
      </article>
    </div>

    <div v-if="activeDetail" class="detail-mask" @click.self="activeDetail = null">
      <div class="detail-panel">
        <header><strong>{{ activeDetail.document_name }}</strong><small>{{ locationLabel(activeDetail) || '全文片段' }}</small><button @click="activeDetail = null">×</button></header>
        <p class="detail-content">{{ activeDetail.content }}</p>
        <div v-if="activeDetail.images.length" class="detail-images">
          <img v-for="(image, index) in activeDetail.images" :key="index" :src="imageUrl(image)" :alt="image.caption || image.filename" loading="lazy" @click="previewImage = { url: imageUrl(image), alt: image.caption || image.filename }" />
        </div>
      </div>
    </div>
    <div v-if="previewImage" class="detail-mask" @click.self="previewImage = null">
      <div class="image-preview-body"><img :src="previewImage.url" :alt="previewImage.alt" /><button class="preview-close" @click="previewImage = null">×</button></div>
    </div>
  </div>
</template>

<style scoped>
.search-page { height: 100%; overflow: auto; padding: 22px; }
.search-bar { display: flex; gap: 8px; max-width: 760px; margin: 0 auto 18px; }
.search-input { flex: 1; padding: 12px 14px; border: 1px solid #dce3ed; border-radius: 10px; font-size: 13px; outline: none; transition: border-color .15s, box-shadow .15s; }
.search-input:focus { border-color: #79a9f5; box-shadow: 0 0 0 3px #3f8ff018; }
.search-btn { padding: 0 22px; border: 0; border-radius: 10px; color: #fff; background: linear-gradient(135deg,#4c92f5,#5e70df); cursor: pointer; font-size: 13px; }
.search-btn:disabled { opacity: .5; cursor: not-allowed; }
.error { padding: 9px 12px; border-radius: 8px; color: #b42318; background: #fff1f0; font-size: 12px; }
.empty { display: grid; place-items: center; align-content: center; min-height: 46%; color: #8390a3; text-align: center; }
.empty-icon { display: grid; place-items: center; width: 48px; height: 48px; border: 1px solid #d8e5fb; border-radius: 14px; color: #438df5; background: linear-gradient(145deg,#fff,#edf5ff); font-size: 22px; }
.empty strong { margin-top: 12px; color: #34425a; font-size: 16px; }
.empty p { max-width: 420px; margin: 7px 0; font-size: 12px; line-height: 1.7; }
.results { max-width: 760px; margin: 0 auto; }
.result-meta { margin: 0 0 12px; color: #8c98a9; font-size: 11px; }
.no-result { padding: 28px; border: 1px dashed #dbe3ee; border-radius: 10px; color: #8c98a9; text-align: center; font-size: 12px; }
.hit-card { padding: 14px 16px; margin-bottom: 10px; border: 1px solid #e2e8f1; border-left: 3px solid #3f9cff; border-radius: 10px; background: #fff; cursor: pointer; transition: box-shadow .15s, border-color .15s; }
.hit-card:hover { border-color: #a8cdfc; box-shadow: 0 8px 22px #2f78d618; }
.hit-card header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 7px; }
.hit-doc { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #263248; font-size: 12px; }
.hit-location { color: #8b98aa; font-size: 9px; }
.hit-chunk { padding: 1px 6px; border-radius: 8px; color: #5c6fc7; background: #eef1ff; font-size: 8px; }
.hit-snippet { margin: 0; color: #5a687e; font-size: 12px; line-height: 1.75; }
.hit-snippet mark { padding: 0 2px; border-radius: 3px; color: #9a6700; background: #fff3cd; }
.hit-card footer { display: flex; align-items: center; gap: 8px; margin-top: 9px; }
.hit-score { padding: 2px 6px; border-radius: 8px; color: #207fda; background: #eaf5ff; font-size: 8px; }
.hit-images { padding: 2px 6px; border-radius: 8px; color: #6f55a5; background: #f5f0ff; font-size: 8px; }
.hit-expand { margin-left: auto; padding: 3px 9px; border: 1px solid #d5dfff; border-radius: 7px; color: #3f5ee6; background: #f3f6ff; cursor: pointer; font-size: 9px; }
.hit-thumbs { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.hit-thumbs img { width: 56px; height: 56px; object-fit: cover; border: 1px solid #dbe4f0; border-radius: 7px; cursor: zoom-in; }
.detail-mask { position: fixed; z-index: 60; inset: 0; display: grid; place-items: center; padding: 28px; background: rgba(15,23,42,.5); }
.detail-panel { width: min(720px,94vw); max-height: 84vh; overflow: auto; border-radius: 12px; background: #fff; box-shadow: 0 22px 60px rgba(16,24,40,.3); }
.detail-panel header { display: flex; align-items: center; gap: 12px; padding: 15px 18px; border-bottom: 1px solid #e7ebf1; }
.detail-panel header strong { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #263248; font-size: 13px; }
.detail-panel header small { color: #8995a6; font-size: 10px; }
.detail-panel header button { border: 0; color: #8390a3; background: transparent; font-size: 20px; cursor: pointer; }
.detail-content { padding: 18px; color: #435168; font-size: 12px; line-height: 1.9; white-space: pre-wrap; }
.detail-images { display: flex; gap: 8px; flex-wrap: wrap; padding: 0 18px 18px; }
.detail-images img { max-width: 160px; max-height: 110px; border: 1px solid #e2e8f1; border-radius: 8px; cursor: zoom-in; }
.image-preview-body { position: relative; }
.image-preview-body img { max-width: 96vw; max-height: 92vh; border-radius: 10px; }
.preview-close { position: fixed; top: 14px; right: 18px; width: 34px; height: 34px; border: 0; border-radius: 50%; color: #fff; background: rgba(255,255,255,.14); cursor: pointer; font-size: 20px; }
</style>
