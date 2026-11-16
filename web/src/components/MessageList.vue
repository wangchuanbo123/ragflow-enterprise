<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useScopedPageState } from '../composables/useScopedPageState'
import { useAuth } from '../composables/useAuth'
import * as api from '../api/client'
import type { ChunkDetail, CodeSnippet, Folder, KnowledgeBase, KnowledgeDocument, Message, RetrievalScope, RetrievalStrategy, SourceImage, SourceItem, Tag } from '../types'
import AnswerContent from './AnswerContent.vue'
import { pageLabel } from '../documentPresentation'
import { navigate } from '../composables/useNavigation'

const props = defineProps<{
  messages: Message[]
  loading: boolean
  sending: boolean
  conversationId?: string | null
  knowledgeBaseId: string
  knowledgeBases: KnowledgeBase[]
  documents: KnowledgeDocument[]
  folders: Folder[]
  tags: Tag[]
  submitMessage: (payload: { content: string; strategy: RetrievalStrategy; scope?: Partial<RetrievalScope> }) => Promise<boolean>
}>()

const emit = defineEmits<{
  (e: 'stop'):void
  (e:'feedback', payload:{messageId:string;rating:'helpful'|'unhelpful'}):void
}>()

type ComposerState = { draft: string; strategy: RetrievalStrategy; scopeKind: 'all'|'knowledge_base'|'document'|'folder'|'tag'; scopeValue: string }
const composer = useScopedPageState<ComposerState>(() => ['chat',props.knowledgeBaseId,props.conversationId || 'new'],
  () => ({draft:'',strategy:'standard',scopeKind:'knowledge_base',scopeValue:props.knowledgeBaseId}), value => {
    const candidate = value as ComposerState | null
    return candidate && typeof candidate.draft==='string' && candidate.draft.length<=100000 && ['fast','standard','deep'].includes(candidate.strategy) && ['all','knowledge_base','document','folder','tag'].includes(candidate.scopeKind) && typeof candidate.scopeValue==='string' ? candidate : null
  })
const draft = computed({get:()=>composer.value.draft,set:value=>composer.value.draft=value})
const strategy = computed({get:()=>composer.value.strategy,set:value=>composer.value.strategy=value})
const scopeKind = computed({get:()=>composer.value.scopeKind,set:value=>composer.value.scopeKind=value})
const scopeValue = computed({get:()=>composer.value.scopeValue,set:value=>composer.value.scopeValue=value})
const {user}=useAuth()
let pendingComposer:{state:ComposerState;account:string;kb:string;conversation:string|null|undefined;target?:string}|null=null
watch(()=>props.conversationId,(id,previous)=>{
  const pending=pendingComposer
  if(id&&!previous&&pending&&pending.account===(user.value?.id||'')&&pending.kb===props.knowledgeBaseId){
    pending.target=id
    composer.value={...pending.state,draft:''}
  }
},{flush:'sync'})
const selectedSource = ref<SourceItem|null>(null)
let sourceRequest = 0
const sourceError = ref('')
const sourceLoading = ref(false)
const chunkDetail = ref<ChunkDetail | null>(null)
const toast = ref('')
const messagesElement = ref<HTMLElement | null>(null)
const followOutput = ref(true)
let scrollFrame: number | null = null
const channelNames: Record<string, string> = { text: '文档检索', graph: '知识图谱', code: '代码检索' }
const strategyNames: Record<RetrievalStrategy, string> = { fast: '快速', standard: '标准', deep: '深度' }
const scopeError = computed(() => {
  if (scopeKind.value === 'all') return props.knowledgeBases.length ? '' : '没有可检索的知识库，请先创建或加入知识库。'
  if (scopeValue.value.trim()) return ''
  return `请先选择${({ knowledge_base: '知识库', document: '文档', folder: '文件夹', tag: '标签' } as const)[scopeKind.value]}，再发送问题。`
})

async function submit(): Promise<void> {
  const text = draft.value.trim()
  if (!text || props.sending || scopeError.value) return
  const pending={state:{...composer.value},account:user.value?.id||'',kb:props.knowledgeBaseId,conversation:props.conversationId,target:undefined as string|undefined}
  pendingComposer=pending
  const payload={content:text,strategy:strategy.value,scope:retrievalScope()}
  draft.value=''
  try{
    const accepted=await props.submitMessage(payload)
    if(!accepted&&pending.account===(user.value?.id||'')&&pending.kb===props.knowledgeBaseId&&(props.conversationId===pending.conversation||props.conversationId===pending.target)&&!draft.value)draft.value=pending.state.draft
  }catch{
    if(pending.account===(user.value?.id||'')&&pending.kb===props.knowledgeBaseId&&(props.conversationId===pending.conversation||props.conversationId===pending.target)&&!draft.value)draft.value=pending.state.draft
  }finally{if(pendingComposer===pending)pendingComposer=null}
}

function retrievalScope():Partial<RetrievalScope>|undefined{
  if(scopeError.value)return undefined
  const selection_kind=scopeKind.value
  if(selection_kind==='all')return {selection_kind,knowledge_base_ids:props.knowledgeBases.map(item=>item.id)}
  if(selection_kind==='knowledge_base')return {selection_kind,knowledge_base_ids:[scopeValue.value]}
  if(selection_kind==='document')return {selection_kind,knowledge_base_ids:[props.knowledgeBaseId],document_ids:[scopeValue.value]}
  if(selection_kind==='folder')return {selection_kind,knowledge_base_ids:[props.knowledgeBaseId],folder_id:scopeValue.value}
  return {selection_kind,knowledge_base_ids:[props.knowledgeBaseId],tag_ids:[scopeValue.value]}
}

function scopeName():string{
  if(scopeKind.value==='knowledge_base')return props.knowledgeBases.find(item=>item.id===scopeValue.value)?.name||'选择知识库'
  if(scopeKind.value==='document')return props.documents.find(item=>item.id===scopeValue.value)?.original_filename||'选择文档'
  if(scopeKind.value==='folder')return props.folders.find(item=>item.id===scopeValue.value)?.name||'选择文件夹'
  if(scopeKind.value==='tag')return props.tags.find(item=>item.id===scopeValue.value)?.name||'选择标签'
  return `全部知识库（${props.knowledgeBases.length} 个）`
}

function onScopeKindChange():void{
  scopeValue.value=scopeKind.value==='knowledge_base'?props.knowledgeBaseId:''
}

async function openSource(source:SourceItem):Promise<void>{
  const requested=++sourceRequest
  selectedSource.value=source;chunkDetail.value=null;sourceError.value=''
  if(!source.document_id||!source.chunk_id)return
  sourceLoading.value=true
  try{
    const detail=await api.getChunkDetail(source.knowledge_base_id||props.knowledgeBaseId,source.chunk_id,1)
    if(requested===sourceRequest)chunkDetail.value=detail
  }catch(error){if(requested===sourceRequest&&(error as Error).name!=='AbortError')sourceError.value=(error as Error).message}
  finally{if(requested===sourceRequest)sourceLoading.value=false}
}

/** 点击答案中的 [来源N] 引用 → 滚动到证据卡片并高亮。 */
function scrollToSource(label:string, messageId:string):void{
  const normalize=(value:string)=>value.replace(/[\s\[\]]/g,'').toLowerCase()
  const normalized=normalize(label)
  const index=props.messages.reduce<number>((acc,message,mIdx)=>{
    if(acc>=0)return acc
    if(message.role!=='assistant')return acc
    if(message.id!==messageId)return acc
    const found=(message.sources||[]).findIndex((item,index)=>normalize(item.citation_id||`来源${index+1}`)===normalized)
    return found>=0?mIdx*1000+found:acc
  },-1)
  if(index<0){toast.value='未找到对应证据卡片';window.setTimeout(()=>toast.value='',1500);return}
  const messageIndex=Math.floor(index/1000)
  const sourceIndex=index%1000
  void nextTick(()=>{
    const container=messagesElement.value
    if(!container)return
    const cards=container.querySelectorAll<HTMLElement>(`[data-source-key="${messageIndex}-${sourceIndex}"]`)
    const card=cards[0]
    if(!card){toast.value='未找到对应证据卡片';window.setTimeout(()=>toast.value='',1500);return}
    card.scrollIntoView({behavior:'smooth',block:'center'})
    card.classList.add('flash')
    window.setTimeout(()=>card.classList.remove('flash'),1800)
  })
}

async function copyAnswer(content:string):Promise<void>{
  try{await navigator.clipboard.writeText(content);toast.value='回答已复制'}catch{toast.value='复制失败，请手动选择文本'}
  window.setTimeout(()=>toast.value='',1600)
}

function exportAnswer(message:Message):void{
  const evidence=(message.sources||[]).map((s,i)=>`[${i+1}] ${s.source}\n${s.preview||''}`).join('\n\n')
  const blob=new Blob([`${message.content}\n\n参考来源\n${evidence}`],{type:'text/plain;charset=utf-8'})
  const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=`知识库回答-${new Date().toISOString().slice(0,10)}.txt`;anchor.click();URL.revokeObjectURL(url)
}

function askSuggestion(content:string):void{if(!props.sending&&!scopeError.value)void props.submitMessage({content,strategy:strategy.value,scope:retrievalScope()})}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    void submit()
  }
}

function sourceLabel(source: SourceItem, index: number): string {
  return source.citation_id || `来源${index + 1}`
}

function sourceLocation(source: SourceItem): string {
  return [source.section, pageLabel(source.page)].filter(Boolean).join(' · ')
}

function openSourceDocument(): void {
  const source=selectedSource.value
  if(source?.document_id)navigate({page:'documents',kb:source.knowledge_base_id||props.knowledgeBaseId,document:source.document_id,chunk:source.chunk_id||''})
}

const diagnosticNames: Record<string,string> = {vector:'向量检索',bm25:'关键词检索',reranker:'重排',embedding:'问题向量',graph:'知识图谱',code:'代码检索'}
const diagnosticStatuses: Record<string,string> = {ok:'正常',degraded:'已降级',error:'不可用',skipped:'已跳过'}

function sourceChannels(source: SourceItem): string[] {
  return [...new Set(source.retrieval_channels || [])].map((item) => channelNames[item] || item)
}

function sourceRankLabel(index: number, total: number): string {
  if (index === 0) return '高'
  if (index < Math.max(2, Math.ceil(total / 3))) return '中'
  return '一般'
}

function messageIndex(m: Message): number {
  return props.messages.findIndex((item) => item.id === m.id)
}

function sourceImages(source: SourceItem) {
  return (source.images || []).filter((image) => image && typeof image.url === 'string')
}

function messageImages(message: Message): SourceImage[] {
  const candidates = message.images?.length
    ? message.images
    : (message.sources || []).flatMap(source => source.images || [])
  const seen = new Set<string>()
  return candidates.filter(image => {
    if (!image?.url || seen.has(image.url)) return false
    seen.add(image.url)
    return true
  })
}

function messageCodeSnippets(message: Message): CodeSnippet[] {
  const candidates = message.code_snippets?.length
    ? message.code_snippets
    : (message.sources || []).flatMap(source => source.code_snippets || [])
  const seen = new Set<string>()
  return candidates.filter(snippet => {
    const key = `${snippet.path}:${snippet.start_line}:${snippet.end_line}:${snippet.content}`
    if (!snippet?.content || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function snippetLocation(snippet: CodeSnippet): string {
  const lines = snippet.start_line == null
    ? ''
    : snippet.line_scope === 'file'
      ? `第 ${snippet.start_line}${snippet.end_line && snippet.end_line !== snippet.start_line ? `–${snippet.end_line}` : ''} 行`
      : `Chunk 内第 ${snippet.start_line}${snippet.end_line && snippet.end_line !== snippet.start_line ? `–${snippet.end_line}` : ''} 行`
  return [snippet.path, lines, snippet.revision ? `版本 ${snippet.revision}` : ''].filter(Boolean).join(' · ')
}

async function copySnippet(snippet: CodeSnippet): Promise<void> {
  try { await navigator.clipboard.writeText(snippet.content); toast.value='原始代码已复制' }
  catch { toast.value='复制失败，请手动选择代码' }
  window.setTimeout(()=>toast.value='',1600)
}

function imageUrl(image: { url: string }): string {
  if (/^https?:\/\//i.test(image.url)) return image.url
  if (image.url.startsWith('/')) return image.url
  return `/api/v1/objects/${encodeURIComponent(image.url)}`
}

const drawerPreview = ref<{ url: string; alt: string } | null>(null)
function openImagePreview(url: string, alt: string): void {
  drawerPreview.value = { url, alt }
}

function scheduleScroll(force = false): void {
  if ((!force && !followOutput.value) || scrollFrame != null) return
  scrollFrame = window.requestAnimationFrame(() => {
    scrollFrame = null
    void nextTick(() => {
      const element = messagesElement.value
      if (element) element.scrollTop = element.scrollHeight
    })
  })
}

function onMessagesScroll(): void {
  const element = messagesElement.value
  if (!element) return
  followOutput.value = element.scrollHeight - element.scrollTop - element.clientHeight < 120
}

watch(() => props.messages.length, () => {
  followOutput.value = true
  scheduleScroll(true)
})
watch(() => {
  const last = props.messages[props.messages.length - 1]
  return last ? `${last.id}:${last.content.length}:${last.status}` : ''
}, () => scheduleScroll())

onBeforeUnmount(() => {
  sourceRequest++
  if (scrollFrame != null) window.cancelAnimationFrame(scrollFrame)
})
</script>

<template>
  <section class="chat-main">
    <div ref="messagesElement" class="messages" @scroll="onMessagesScroll">
      <div v-if="loading" class="state-tip"><span class="state-spinner"></span>正在加载历史消息…</div>
      <div v-else-if="!messages.length" class="empty-state">
        <div class="empty-icon">✦</div>
        <strong>开始探索企业知识库</strong>
        <p>输入业务问题，我会结合文档、知识图谱与代码给出有依据的回答。</p>
      </div>

      <template v-else>
        <article v-for="m in messages" :key="m.id" :class="['message', m.role]">
          <template v-if="m.role === 'user'">
            <div class="user-label">我</div>
            <div class="user-bubble">{{ m.content }}</div>
          </template>

          <template v-else>
            <header class="assistant-header">
              <span class="assistant-avatar">AI</span>
              <div><strong>知识库助手</strong><small>基于本次选择的检索范围</small></div>
              <span v-if="m.status === 'completed'" class="answer-state">回答完成</span>
              <span v-else-if="['stopped','interrupted'].includes(m.status)" class="answer-state stopped">已停止</span>
            </header>

            <div class="answer-body">
              <div v-if="m.status === 'streaming'" class="streaming-answer">{{ m.content }}</div>
              <AnswerContent v-else :content="m.content" @citation="label => scrollToSource(label,m.id)" />
              <span v-if="m.status === 'streaming'" class="cursor">▍</span>
              <div v-if="m.status === 'failed'" class="failed">
                <strong>回答生成失败</strong>
                <span>{{ m.error_message || '请稍后重试' }}</span>
              </div>
              <div v-if="m.status==='completed'&&(!m.sources||!m.sources.length)" class="no-evidence"><b>未找到可核验来源</b><span>本次回答没有返回文档证据，建议缩小问题范围或切换为深度检索。</span></div>
              <div v-if="m.route?.retrieval_diagnostics?.some(item=>['degraded','error'].includes(item.status))" class="quality-notice" role="status">部分检索服务已降级或不可用，结果可能不完整；请展开“检索状态”查看原因。</div>
              <div v-if="m.route?.evidence_status==='unavailable'" class="quality-notice">检索服务不可用，本次未能核实资料。请稍后重试或联系管理员。</div>
              <div v-else-if="m.route?.evidence_status==='insufficient'" class="quality-notice">本次证据不足，请补充资料或调整问题。</div>
              <p v-if="m.status==='completed'&&m.route?.citation_validation?.status==='valid'" class="citation-validation">引用编号已校验；结论与原文是否一致仍需核对。</p>
              <p v-else-if="m.route?.citation_validation?.status==='invalid'" class="quality-notice">回答含无效引用编号：{{m.route.citation_validation.invalid_ids?.join('、')}}。请核对来源后使用。</p>
              <p v-else-if="m.status==='completed'&&m.sources?.length" class="citation-validation">引用支持关系未校验，请结合原文核对。</p>
              <div v-if="['completed','stopped','interrupted'].includes(m.status)" class="answer-actions">
                <button @click="copyAnswer(m.content)">复制</button><button @click="exportAnswer(m)">导出</button>
                <span></span><button :class="{selected:m.feedback==='helpful'}" :disabled="!m.id" @click="emit('feedback',{messageId:m.id,rating:'helpful'})">有帮助</button><button :class="{selected:m.feedback==='unhelpful'}" :disabled="!m.id" @click="emit('feedback',{messageId:m.id,rating:'unhelpful'})">需改进</button>
              </div>
              <div v-if="m.status==='completed'" class="suggestions"><span>继续追问</span><button @click="askSuggestion('请总结关键结论，并按重要程度排序。')">总结关键结论</button><button @click="askSuggestion('有哪些相关风险和处理措施？')">风险与措施</button><button @click="askSuggestion('这些结论分别来自哪些文档？')">核对来源</button></div>
            </div>

            <section v-if="messageImages(m).length" class="artifact-panel image-artifacts">
              <header><div><b>相关图片</b><small>来自本次回答命中的文档证据</small></div><span>{{messageImages(m).length}} 张</span></header>
              <div class="artifact-images">
                <figure v-for="(image,index) in messageImages(m)" :key="image.url">
                  <img :src="imageUrl(image)" :alt="image.caption||image.filename||`图片 ${index+1}`" loading="lazy" @click="openImagePreview(imageUrl(image),image.caption||image.filename||`图片 ${index+1}`)" />
                  <figcaption><b>{{image.caption||image.filename||`图片 ${index+1}`}}</b><small>{{[image.knowledge_base_name,pageLabel(image.page),image.citation_id].filter(Boolean).join(' · ')}}</small></figcaption>
                </figure>
              </div>
            </section>

            <section v-if="messageCodeSnippets(m).length" class="artifact-panel code-artifacts">
              <header><div><b>相关代码</b><small>原样取自文档或代码仓库，可直接复制</small></div><span>{{messageCodeSnippets(m).length}} 段</span></header>
              <article v-for="(snippet,index) in messageCodeSnippets(m)" :key="`${snippet.path}-${snippet.start_line}-${index}`" class="code-artifact">
                <header><div><b>{{snippet.qualified_name||snippet.path||`代码片段 ${index+1}`}}</b><small>{{snippetLocation(snippet)}}</small></div><span :class="{generated:!snippet.is_verbatim}">{{snippet.is_verbatim?'原文代码':'AI 生成'}}</span><button @click="copySnippet(snippet)">复制</button></header>
                <pre><code>{{snippet.content}}</code></pre>
              </article>
            </section>

            <details v-if="m.sources && m.sources.length" class="source-panel" open>
              <summary>
                <span class="source-heading"><b>参考来源</b><small>回答中的编号可对应下方证据</small></span>
                <span class="source-count">{{ m.sources.length }} 条证据</span>
              </summary>
              <div class="source-scroll">
                <article v-for="(source, index) in m.sources" :key="`${source.knowledge_base_id||''}:${source.chunk_id || `${source.source}-${index}`}`" :data-source-key="`${messageIndex(m)}-${index}`" class="source-card" role="button" tabindex="0" @click="openSource(source)" @keydown.enter="openSource(source)">
                  <div class="source-index">{{ sourceLabel(source, index) }}</div>
                  <small v-if="source.knowledge_base_name" class="source-kb">{{source.knowledge_base_name}}</small>
                  <strong class="source-title" :title="source.source">{{ source.source }}</strong>
                  <small v-if="sourceLocation(source)" class="source-location">{{ sourceLocation(source) }}</small>
                  <div v-if="sourceImages(source).length" class="source-thumbs">
                    <img v-for="(image, imageIndex) in sourceImages(source)" :key="imageIndex" :src="imageUrl(image)" :alt="image.caption || image.filename" :title="image.caption || image.filename" loading="lazy" @click.stop="openImagePreview(imageUrl(image), image.caption || image.filename)" />
                  </div>
                  <p class="source-preview">{{ source.preview || '该证据暂无文本预览。' }}</p>
                  <div v-if="source.graph_facts && source.graph_facts.length" class="source-fact">{{ source.graph_facts[0] }}</div>
                  <footer>
                    <span v-for="channel in sourceChannels(source)" :key="channel" class="source-tag">{{ channel }}</span>
                    <span class="score-tag">排序等级 {{ sourceRankLabel(index, m.sources.length) }}</span>
                  </footer>
                </article>
              </div>
            </details>

            <details v-if="m.route" class="route-panel">
              <summary><span>检索状态</span><small>{{ strategyNames[m.route.strategy || 'standard'] }}模式 · 路由评分 {{ Math.round(m.route.confidence * 100) }} 分</small></summary>
              <div class="route-body">
                <div class="route-tags">
                  <span v-for="channel in m.route.selected_channels" :key="channel">{{ channelNames[channel] || channel }}</span>
                  <span v-if="m.route.query_embedding?.shared">问题向量已共享</span>
                  <span v-if="m.route.rewrite?.applied">已执行问题改写</span>
                </div>
                <p v-if="m.route.reasons.length">{{ m.route.reasons.join('；') }}</p>
                <div v-if="m.route.channel_results" class="channel-results">
                  <div v-for="(result, name) in m.route.channel_results" :key="name">
                    <b>{{ channelNames[name] || name }}</b><span>{{ result.count }} 条 · {{ result.elapsed_ms }} ms</span>
                  </div>
                </div>
                <div v-if="m.route.retrieval_diagnostics?.length" class="channel-results"><div v-for="(result,index) in m.route.retrieval_diagnostics" :key="index"><b>{{diagnosticNames[result.channel]||result.channel}} · {{diagnosticStatuses[result.status]||result.status}}</b><span>{{result.count}} 条 · {{result.elapsed_ms}} ms</span><small v-if="result.status!=='ok'">{{result.detail||result.code}}</small></div></div>
              </div>
            </details>
          </template>
        </article>
      </template>
    </div>

    <div class="scope-bar">
      <span>回答范围</span><select v-model="scopeKind" @change="onScopeKindChange">
        <option value="all">全部知识库</option><option value="knowledge_base">指定知识库</option><option value="document">指定文档</option><option value="folder">指定文件夹</option><option value="tag">指定标签</option>
      </select>
      <select v-if="scopeKind==='knowledge_base'" v-model="scopeValue"><option v-for="item in knowledgeBases" :key="item.id" :value="item.id">{{item.name}}</option></select>
      <select v-if="scopeKind==='document'" v-model="scopeValue"><option value="">请选择文档</option><option v-for="item in documents" :key="item.id" :value="item.id">{{item.original_filename}}</option></select>
      <select v-if="scopeKind==='folder'" v-model="scopeValue"><option value="">请选择文件夹</option><option v-for="item in folders" :key="item.id" :value="item.id">{{item.name}}</option></select>
      <select v-if="scopeKind==='tag'" v-model="scopeValue"><option value="">请选择标签</option><option v-for="item in tags" :key="item.id" :value="item.id">{{item.name}}</option></select>
      <small>当前：{{scopeName()}}</small>
    </div>
    <p v-if="scopeError" class="scope-error" role="alert">{{scopeError}}</p>
    <div class="composer">
      <select v-model="strategy" class="strategy" aria-label="检索策略">
        <option value="fast">快速</option>
        <option value="standard">标准</option>
        <option value="deep">深度</option>
      </select>
      <textarea
        v-model="draft"
        class="input"
        rows="1"
        placeholder="输入问题，Enter 发送，Shift+Enter 换行"
        :disabled="sending"
        @keydown="onKeydown"
      ></textarea>
      <button v-if="sending" class="send-btn stop" @click="emit('stop')"><span>停止生成</span></button>
      <button v-else class="send-btn" :disabled="!draft.trim()||!!scopeError" @click="submit">
        <span>发送</span>
      </button>
    </div>
    <div v-if="toast" class="toast">{{toast}}</div>
    <div v-if="drawerPreview" class="drawer-mask" @click.self="drawerPreview=null"><div class="image-preview-body"><img :src="drawerPreview.url" :alt="drawerPreview.alt" /><button class="preview-close" @click="drawerPreview=null">×</button></div></div>
    <div v-if="selectedSource" class="drawer-mask" @click.self="selectedSource=null"><aside class="source-drawer"><header><div><strong>{{selectedSource.source}}</strong><small>{{sourceLocation(selectedSource) || '来源证据'}}</small></div><button @click="selectedSource=null">×</button></header><div class="drawer-body">
      <div class="evidence-id">{{selectedSource.citation_id||'来源详情'}}</div>
      <button v-if="selectedSource.document_id" class="open-document" @click="openSourceDocument">在文档中定位此片段</button>
      <p v-if="sourceLoading">正在读取原始片段…</p>
      <template v-else>
        <div v-if="chunkDetail?.previous?.length" class="context-block prev"><span class="context-label">前文 · #{{ chunkDetail.previous[chunkDetail.previous.length-1].chunk_index }}</span><p>{{ chunkDetail.previous[chunkDetail.previous.length-1].content }}</p></div>
        <p v-if="sourceError" class="failed">{{sourceError}}</p>
        <p class="full-evidence current">{{ chunkDetail?.content || selectedSource.preview || '暂无可显示的原始片段。' }}</p>
        <div v-if="chunkDetail?.next?.length" class="context-block next"><span class="context-label">后文 · #{{ chunkDetail.next[0].chunk_index }}</span><p>{{ chunkDetail.next[0].content }}</p></div>
        <div v-if="chunkDetail?.images?.length" class="drawer-images">
          <img v-for="(image, index) in chunkDetail.images" :key="index" :src="imageUrl(image)" :alt="image.caption || image.filename" loading="lazy" @click="openImagePreview(imageUrl(image), image.caption || image.filename)" />
        </div>
      </template>
      <dl><dt>文档编号</dt><dd>{{selectedSource.document_id||'—'}}</dd><dt>片段编号</dt><dd>{{selectedSource.chunk_id||'—'}}</dd><dt>检索通道</dt><dd>{{sourceChannels(selectedSource).join('、')||'—'}}</dd></dl></div></aside></div>
  </section>
</template>

<style scoped>
.scope-error{margin:0;padding:6px 20px;color:#a45113;background:#fff7e8;font-size:12px}.quality-notice{margin:10px 0;padding:9px 12px;border-radius:8px;background:#fff4df;color:#875010;font-size:12px}.citation-validation{font-size:11px;color:#68788a}.channel-results small{display:block;color:#946418}.open-document{margin:8px 0;padding:7px 10px;border:1px solid #cbdaf1;border-radius:7px;background:#f4f8ff;color:#315ea4}
.chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; background: #f7f9fc; }
.messages { min-height: 0; flex: 1; display: flex; flex-direction: column; gap: 20px; overflow-y: auto; padding: 24px clamp(18px,3vw,42px) 18px; }
.state-tip { display: flex; justify-content: center; align-items: center; gap: 8px; margin-top: 45px; color: #8c98a9; font-size: 13px; }
.state-spinner { width: 13px; height: 13px; border: 2px solid #dce5f3; border-top-color: #4d8ff7; border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.empty-state { display: grid; place-items: center; align-content: center; min-height: 55%; color: #8390a3; text-align: center; }
.empty-state strong { margin-top: 12px; color: #34425a; font-size: 17px; }
.empty-state p { max-width: 440px; margin: 7px 0; font-size: 12px; }
.empty-icon { display: grid; place-items: center; width: 50px; height: 50px; border: 1px solid #d8e5fb; border-radius: 15px; color: #438df5; background: linear-gradient(145deg,#fff,#edf5ff); box-shadow: 0 8px 24px #397ee51c; font-size: 23px; }
.message { box-sizing: border-box; width: 100%; flex: 0 0 auto; margin: 0 auto; }
.message.user { display: flex; justify-content: flex-end; align-items: flex-end; gap: 8px; }
.user-label { display: grid; place-items: center; width: 26px; height: 26px; order: 2; border-radius: 50%; color: #fff; background: #6a79dc; font-size: 10px; }
.user-bubble { max-width: min(72%,760px); padding: 11px 15px; border-radius: 13px 13px 3px 13px; color: #fff; background: linear-gradient(135deg,#6579e8,#5670dc); box-shadow: 0 5px 14px #5067cb25; font-size: 13px; line-height: 1.65; white-space: pre-wrap; }
.message.assistant { overflow: hidden; border: 1px solid #e3e8f0; border-radius: 15px; background: #fff; box-shadow: 0 5px 24px #23395d0b; }
.assistant-header { display: flex; align-items: center; gap: 10px; padding: 13px 18px; border-bottom: 1px solid #edf0f5; background: linear-gradient(90deg,#fbfdff,#fff); }
.assistant-avatar { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 9px; color: #fff; background: linear-gradient(145deg,#4498ff,#616ee8); box-shadow: 0 4px 11px #397ee530; font-size: 10px; font-weight: 750; }
.assistant-header div { flex: 1; }
.assistant-header strong,.assistant-header small { display: block; }
.assistant-header strong { color: #27354a; font-size: 12px; }
.assistant-header small { margin-top: 2px; color: #9aa5b4; font-size: 9px; }
.answer-state { padding: 3px 8px; border-radius: 9px; color: #247651; background: #eaf8f0; font-size: 9px; }
.answer-state.stopped{color:#765b25;background:#fff5da}
.answer-body { padding: 22px clamp(18px,2.5vw,34px) 18px; }
.streaming-answer { color: #263248; font-size: 14px; line-height: 1.8; white-space: pre-wrap; word-break: break-word; }
.cursor { display: inline-block; color: #438df5; animation: blink 1s steps(2,start) infinite; }
@keyframes blink { to { visibility: hidden; } }
.failed { display: flex; flex-direction: column; gap: 2px; margin-top: 12px; padding: 11px 13px; border: 1px solid #ffd7d3; border-radius: 9px; color: #b42318; background: #fff6f5; font-size: 11px; }
.no-evidence{display:flex;flex-direction:column;gap:3px;margin-top:12px;padding:10px 12px;border:1px solid #f2d995;border-radius:8px;color:#7a5b19;background:#fffaf0;font-size:10px}.answer-actions{display:flex;align-items:center;gap:6px;margin-top:14px}.answer-actions span{flex:1}.answer-actions button,.suggestions button{padding:5px 8px;border:1px solid #e0e6ef;border-radius:7px;color:#66758a;background:#fff;cursor:pointer;font-size:9px}.answer-actions button.selected{color:#315ddd;border-color:#b9c9ff;background:#eef3ff}.answer-actions button:disabled{opacity:.4}.suggestions{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:10px}.suggestions>span{color:#9aa5b4;font-size:9px}.suggestions button{color:#4962c7;background:#f7f9ff}
.source-panel { margin: 0 clamp(18px,2.5vw,34px); padding: 17px 0 19px; border-top: 1px solid #edf0f5; }
.artifact-panel{margin:0 clamp(18px,2.5vw,34px);padding:16px 0;border-top:1px solid #edf0f5}.artifact-panel>header{display:flex;align-items:center;gap:10px;margin-bottom:11px}.artifact-panel>header div{flex:1}.artifact-panel>header b,.artifact-panel>header small{display:block}.artifact-panel>header b{color:#27354a;font-size:13px}.artifact-panel>header small{margin-top:2px;color:#9aa5b4;font-size:9px}.artifact-panel>header>span{color:#7f8da1;font-size:10px}.artifact-images{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}.artifact-images figure{margin:0;overflow:hidden;border:1px solid #e1e7f0;border-radius:10px;background:#fafcff}.artifact-images img{display:block;width:100%;height:140px;object-fit:contain;background:#f3f6fa;cursor:zoom-in}.artifact-images figcaption{padding:8px}.artifact-images figcaption b,.artifact-images figcaption small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.artifact-images figcaption b{color:#3a485d;font-size:10px}.artifact-images figcaption small{margin-top:3px;color:#8c98a9;font-size:8px}.code-artifact{margin-top:9px;overflow:hidden;border:1px solid #27364d;border-radius:10px;background:#111b2b}.code-artifact>header{display:flex;align-items:center;gap:8px;padding:9px 11px;background:#182437}.code-artifact>header div{min-width:0;flex:1}.code-artifact>header b,.code-artifact>header small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.code-artifact>header b{color:#dbe7f6;font-size:10px}.code-artifact>header small{margin-top:2px;color:#8291a8;font-size:8px}.code-artifact>header span{padding:2px 6px;border-radius:7px;color:#78d39d;background:#183c2a;font-size:8px}.code-artifact>header span.generated{color:#e9c56f;background:#453719}.code-artifact>header button{padding:3px 8px;border:1px solid #40516b;border-radius:6px;color:#b8c6d9;background:#24334a;cursor:pointer;font-size:8px}.code-artifact pre{max-height:340px;margin:0;padding:14px;overflow:auto;color:#dbe7f6;font:11px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre}.code-artifact code{font:inherit}
details > summary { list-style: none; cursor: pointer; }
details > summary::-webkit-details-marker { display: none; }
.source-panel > summary { display: flex; align-items: center; gap: 12px; margin-bottom: 13px; }
.source-heading { flex: 1; }
.source-heading b,.source-heading small { display: block; }
.source-heading b { color: #27354a; font-size: 13px; }
.source-heading small { margin-top: 2px; color: #9aa5b4; font-size: 9px; }
.source-count { color: #8c98a9; font-size: 10px; }
.source-count::after { content: '⌃'; display: inline-block; margin-left: 7px; color: #7f8da1; transition: transform .2s; }
.source-panel:not([open]) .source-count::after { transform: rotate(180deg); }
.source-scroll { display: flex; gap: 10px; overflow-x: auto; padding: 2px 2px 9px; scroll-snap-type: x proximity; scrollbar-color: #cfdaea transparent; scrollbar-width: thin; }
.source-card { box-sizing: border-box; display: flex; flex: 1 0 250px; flex-direction: column; width: 250px; min-height: 220px; padding: 13px 13px 11px; overflow: hidden; border: 1px solid #e0e6ef; border-left: 3px solid #3f9cff; border-radius: 10px; background: #fff; box-shadow: 0 2px 7px #263b5c08; scroll-snap-align: start; transition: border-color .2s,box-shadow .2s,transform .2s; }
.source-card:hover { border-color: #a8cdfc; box-shadow: 0 8px 22px #2f78d618; transform: translateY(-2px); }
.source-index { margin-bottom: 7px; color: #1685ee; font-size: 10px; font-weight: 700; }
.source-kb { align-self: flex-start; margin: -2px 0 6px; padding: 2px 6px; border-radius: 7px; color: #5c6fc7; background: #eef1ff; font-size: 8px; }
.source-title { display: -webkit-box; overflow: hidden; color: #263248; font-size: 11px; line-height: 1.45; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.source-location { margin-top: 4px; overflow: hidden; color: #8b98aa; font-size: 8px; text-overflow: ellipsis; white-space: nowrap; }
.source-preview { display: -webkit-box; flex: 1; margin: 8px 0 7px; overflow: hidden; color: #6e7c90; font-size: 9px; line-height: 1.65; -webkit-box-orient: vertical; -webkit-line-clamp: 7; }
.source-fact { margin: 0 0 7px; padding: 5px 6px; overflow: hidden; border-radius: 5px; color: #6f55a5; background: #f5f0ff; font-size: 8px; text-overflow: ellipsis; white-space: nowrap; }
.source-card footer { display: flex; gap: 4px; flex-wrap: wrap; min-height: 17px; }
.source-tag,.score-tag { padding: 2px 6px; border-radius: 8px; color: #207fda; background: #eaf5ff; font-size: 8px; }
.score-tag { color: #66758a; background: #f0f3f7; }
.route-panel { margin: 0 clamp(18px,2.5vw,34px) 18px; border-top: 1px solid #edf0f5; color: #728095; font-size: 10px; }
.route-panel > summary { display: flex; justify-content: space-between; padding: 11px 0 0; }
.route-panel > summary span::before { content: '›'; display: inline-block; margin-right: 5px; transition: transform .2s; }
.route-panel[open] > summary span::before { transform: rotate(90deg); }
.route-panel > summary small { color: #9ba6b5; }
.route-body { padding: 11px 12px; margin-top: 9px; border-radius: 9px; background: #f7f9fc; }
.route-tags { display: flex; gap: 5px; flex-wrap: wrap; }
.route-tags span { padding: 3px 7px; border-radius: 8px; color: #5067ca; background: #eaf0ff; }
.route-body p { margin: 8px 0 0; line-height: 1.6; }
.channel-results { display: grid; grid-template-columns: repeat(auto-fit,minmax(140px,1fr)); gap: 6px; margin-top: 9px; }
.channel-results div { display: flex; justify-content: space-between; padding: 6px 8px; border: 1px solid #e5eaf1; border-radius: 7px; background: #fff; }
.channel-results b { color: #4a5870; }
.scope-bar{display:flex;align-items:center;gap:7px;padding:8px clamp(18px,3vw,42px) 0;border-top:1px solid #e1e7f0;color:#7b8798;background:#fff;font-size:9px}.scope-bar select{max-width:220px;padding:5px 7px;border:1px solid #e0e5ed;border-radius:7px;color:#536176;background:#fbfcfe;font-size:10px}.scope-bar small{overflow:hidden;color:#9aa5b4;text-overflow:ellipsis;white-space:nowrap}.composer { display: flex; gap: 8px; padding: 8px clamp(18px,3vw,42px) 12px; background: #fff; box-shadow: 0 -4px 18px #23395d08; }
.strategy { align-self: stretch; padding: 0 10px; border: 1px solid #dce3ed; border-radius: 9px; color: #526075; background: #f9fbfd; outline: none; }
.input { flex: 1; max-height: 140px; padding: 10px 12px; resize: none; border: 1px solid #dce3ed; border-radius: 9px; color: #2f3d52; background: #fbfcfe; font: 13px/1.5 inherit; outline: none; transition: border-color .2s,box-shadow .2s; }
.input:focus { border-color: #79a9f5; background: #fff; box-shadow: 0 0 0 3px #3f8ff018; }
.send-btn { min-width: 72px; border: 0; border-radius: 9px; color: #fff; background: linear-gradient(135deg,#4c92f5,#5e70df); box-shadow: 0 4px 12px #476ed52b; cursor: pointer; font-size: 12px; }
.send-btn:disabled { opacity: .5; box-shadow: none; cursor: not-allowed; }
.send-btn.stop{background:#fff;color:#b33a32;border:1px solid #e8b8b4;box-shadow:none}.toast{position:fixed;z-index:60;bottom:78px;left:50%;padding:8px 13px;border-radius:9px;color:#fff;background:#25344edb;transform:translateX(-50%);font-size:11px}.drawer-mask{position:fixed;z-index:50;inset:0;background:#15213a48}
.source-drawer{position:absolute;top:0;right:0;bottom:0;width:min(540px,90vw);overflow:auto;background:#fff;box-shadow:-10px 0 34px #16233a24}
.source-drawer>header{display:flex;align-items:center;gap:12px;padding:18px;border-bottom:1px solid #e7ebf1}
.source-drawer>header div{min-width:0;flex:1}
.source-drawer strong,.source-drawer small{display:block}
.source-drawer strong{overflow:hidden;color:#263248;text-overflow:ellipsis;white-space:nowrap}
.source-drawer small{margin-top:4px;color:#8995a6;font-size:10px}
.source-drawer header button{border:0;color:#8390a3;background:transparent;font-size:22px;cursor:pointer}
.drawer-body{padding:20px}
.evidence-id{display:inline-block;padding:4px 8px;border-radius:8px;color:#2571d5;background:#ebf4ff;font-size:10px}
.full-evidence{padding:14px;border:1px solid #e5eaf1;border-radius:10px;color:#435168;background:#fafbfd;font-size:12px;line-height:1.8;white-space:pre-wrap}
.full-evidence.current{border-color:#a8cdfc;background:#f4f9ff}
.context-block{margin:10px 0;padding:10px 12px;border-left:3px solid #cdd8e8;border-radius:0 8px 8px 0;color:#7a8798;background:#f7f9fc}
.context-block.prev{border-left-color:#a8cdfc}
.context-block.next{border-left-color:#a8cdfc}
.context-block p{margin:4px 0 0;font-size:11px;line-height:1.7;white-space:pre-wrap;max-height:130px;overflow:auto}
.context-label{color:#5c78c9;font-size:9px;font-weight:700}
.drawer-images{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.drawer-images img{max-width:140px;max-height:100px;border:1px solid #e2e8f1;border-radius:8px;cursor:zoom-in}
.drawer-body dl{display:grid;grid-template-columns:80px 1fr;gap:8px;margin-top:18px;font-size:10px}
.drawer-body dt{color:#97a2b2}
.drawer-body dd{margin:0;overflow-wrap:anywhere;color:#526177}
.image-preview-body{position:relative;display:grid;place-items:center;width:100%;height:100%;padding:24px;box-sizing:border-box}
.image-preview-body img{max-width:96vw;max-height:92vh;border-radius:10px}
.preview-close{position:fixed;top:14px;right:18px;width:34px;height:34px;border:0;border-radius:50%;color:#fff;background:rgba(255,255,255,.14);cursor:pointer;font-size:20px}
.source-thumbs{display:flex;gap:6px;margin:8px 0 0}
.source-thumbs img{width:54px;height:54px;object-fit:cover;border:1px solid #dbe4f0;border-radius:7px;cursor:zoom-in;transition:transform .15s}
.source-thumbs img:hover{transform:scale(1.06)}
@keyframes sourceFlash{0%,100%{box-shadow:0 2px 7px #263b5c08}30%{box-shadow:0 0 0 3px #79a9f566,0 8px 22px #2f78d63d}}
.source-card.flash{animation:sourceFlash 1.8s ease}
@media (max-width: 700px) {
  .messages { padding: 14px 10px; }
  .message.assistant { border-radius: 11px; }
  .assistant-header { padding: 11px 13px; }
  .answer-body { padding: 17px 14px 12px; }
  .source-panel,.route-panel { margin-left: 14px; margin-right: 14px; }
  .source-card { flex-basis: 210px; width: 210px; }
  .user-bubble { max-width: 85%; }
  .composer { padding: 9px; }
  .scope-bar{padding:7px 9px 0}.scope-bar small{display:none}
  .strategy { width: 62px; padding: 0 4px; }
  .send-btn { min-width: 58px; }
}
</style>
