<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{ content: string }>()

const emit = defineEmits<{
  (e: 'citation', label: string): void
}>()

type InlineToken =
  | { type: 'text' | 'strong' | 'code'; text: string }
  | { type: 'citation'; text: string }
  | { type: 'image'; text: string; alt: string; url: string }

type AnswerBlock =
  | { type: 'heading'; level: number; text: string }
  | { type: 'paragraph' | 'summary' | 'quote'; text: string }
  | { type: 'unordered' | 'ordered'; items: string[] }
  | { type: 'code'; text: string; language: string }
  | { type: 'divider' }
  | { type: 'table'; headers: string[]; rows: string[][] }
  | { type: 'image'; text: string; alt: string; url: string }

// 行内解析：**粗体**、`代码`、[来源N]、![alt](url) 图片
function inlineTokens(text: string): InlineToken[] {
  const tokens: InlineToken[] = []
  const pattern = /(!\[((?:[^[\]]|\[[^[\]]*\])*)\]\(([^()\s]+)\)|\*\*([^*]+)\*\*|`([^`]+)`|\[((?:来源|source)\s*\d+)\])/gi
  let cursor = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) tokens.push({ type: 'text', text: text.slice(cursor, match.index) })
    if (match[3] !== undefined) {
      tokens.push({ type: 'image', text: match[0], alt: match[2] || '', url: match[3] })
    } else if (match[4]) {
      tokens.push({ type: 'strong', text: match[4] })
    } else if (match[5]) {
      tokens.push({ type: 'code', text: match[5] })
    } else {
      tokens.push({ type: 'citation', text: (match[6] || '').replace(/\s+/g, '') })
    }
    cursor = match.index + match[0].length
  }
  if (cursor < text.length) tokens.push({ type: 'text', text: text.slice(cursor) })
  return tokens.length ? tokens : [{ type: 'text', text }]
}

function tableCells(line: string): string[] {
  return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim())
}

function isTableSeparator(line: string): boolean {
  const cells = tableCells(line)
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell))
}

// 独立成行的图片 → 图片块
function standaloneImage(line: string): { alt: string; url: string } | null {
  const match = line.trim().match(/^!\[((?:[^[\]]|\[[^[\]]*\])*)\]\(([^()\s]+)\)$/)
  return match ? { alt: match[1], url: match[2] } : null
}

function parseAnswer(content: string): AnswerBlock[] {
  const lines = content.replace(/\r\n?/g, '\n').split('\n')
  const blocks: AnswerBlock[] = []
  let paragraph: string[] = []
  let codeLines: string[] = []
  let codeLanguage = ''
  let inCode = false

  const flushParagraph = () => {
    const text = paragraph.join('\n').trim()
    paragraph = []
    if (!text) return
    blocks.push({
      type: /^(?:\*\*)?(总结|结论|核心结论|简要结论)(?:\*\*)?[：:]/.test(text) ? 'summary' : 'paragraph',
      text,
    })
  }

  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index]
    const line = raw.trim()

    if (line.startsWith('```')) {
      if (inCode) {
        blocks.push({ type: 'code', text: codeLines.join('\n'), language: codeLanguage })
        codeLines = []
        codeLanguage = ''
        inCode = false
      } else {
        flushParagraph()
        inCode = true
        codeLanguage = line.slice(3).trim()
      }
      continue
    }
    if (inCode) {
      codeLines.push(raw)
      continue
    }
    if (!line) {
      flushParagraph()
      continue
    }

    const image = standaloneImage(line)
    if (image) {
      flushParagraph()
      blocks.push({ type: 'image', text: line, alt: image.alt, url: image.url })
      continue
    }

    if (line.includes('|') && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      flushParagraph()
      const headers = tableCells(line)
      const rows: string[][] = []
      index += 2
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(tableCells(lines[index]))
        index += 1
      }
      index -= 1
      blocks.push({ type: 'table', headers, rows })
      continue
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/)
    const numberedHeading = line.length <= 80 ? line.match(/^(\d+(?:\.\d+)*[.、])\s*(.+)$/) : null
    if (heading || numberedHeading) {
      flushParagraph()
      blocks.push({
        type: 'heading',
        level: heading ? heading[1].length : 3,
        text: heading ? heading[2] : `${numberedHeading![1]} ${numberedHeading![2]}`,
      })
      continue
    }
    if (/^(?:-{3,}|\*{3,})$/.test(line)) {
      flushParagraph()
      blocks.push({ type: 'divider' })
      continue
    }
    if (/^>\s?/.test(line)) {
      flushParagraph()
      blocks.push({ type: 'quote', text: line.replace(/^>\s?/, '') })
      continue
    }
    if (/^[-*•]\s+/.test(line)) {
      flushParagraph()
      const items = [line.replace(/^[-*•]\s+/, '')]
      while (index + 1 < lines.length && /^\s*[-*•]\s+/.test(lines[index + 1])) {
        index += 1
        items.push(lines[index].trim().replace(/^[-*•]\s+/, ''))
      }
      blocks.push({ type: 'unordered', items })
      continue
    }
    if (/^\d+[.)、]\s*/.test(line)) {
      flushParagraph()
      const items = [line.replace(/^\d+[.)、]\s*/, '')]
      while (index + 1 < lines.length && /^\s*\d+[.)、]\s*/.test(lines[index + 1])) {
        index += 1
        items.push(lines[index].trim().replace(/^\d+[.)、]\s*/, ''))
      }
      blocks.push({ type: 'ordered', items })
      continue
    }
    paragraph.push(raw.trim())
  }

  if (inCode) blocks.push({ type: 'code', text: codeLines.join('\n'), language: codeLanguage })
  flushParagraph()
  return blocks
}

const blocks = computed(() => parseAnswer(props.content))

// ---------- 零依赖语法高亮 ----------
// 正则扫描 token 化：注释 / 字符串 / 关键字 / 数字 / 函数名。追求够用的可读性。

const KEYWORDS = new Set([
  'if', 'else', 'elif', 'for', 'while', 'return', 'break', 'continue', 'import', 'from',
  'class', 'def', 'function', 'const', 'let', 'var', 'new', 'try', 'catch', 'finally',
  'throw', 'raise', 'with', 'as', 'in', 'is', 'not', 'and', 'or', 'None', 'True',
  'False', 'null', 'undefined', 'true', 'false', 'this', 'self', 'super', 'static',
  'async', 'await', 'yield', 'lambda', 'pass', 'match', 'case', 'switch', 'default',
  'do', 'then', 'except', 'int', 'float', 'str', 'bool', 'list', 'dict', 'set',
  'tuple', 'void', 'public', 'private', 'protected', 'extends', 'implements',
  'interface', 'type', 'enum', 'struct', 'export', 'readonly',
])

type CodeToken = { kind: string; text: string }

function tokenizeCode(code: string): CodeToken[] {
  const tokens: CodeToken[] = []
  let i = 0
  const push = (kind: string, text: string) => { if (text) tokens.push({ kind, text }) }
  while (i < code.length) {
    const rest = code.slice(i)
    let m: RegExpExecArray | null
    if ((m = /^((?:#|\/\/)[^\n]*)/.exec(rest))) { push('comment', m[1]); i += m[1].length; continue }
    if ((m = /^\/\*[\s\S]*?\*\//.exec(rest))) { push('comment', m[0]); i += m[0].length; continue }
    if ((m = /^(?:'''[\s\S]*?'''|"""[\s\S]*?""")/.exec(rest))) { push('string', m[0]); i += m[0].length; continue }
    if ((m = /^('(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)/.exec(rest))) { push('string', m[1]); i += m[1].length; continue }
    if ((m = /^(0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(rest))) { push('number', m[1]); i += m[1].length; continue }
    if ((m = /^([A-Za-z_$][A-Za-z0-9_$]*)/.exec(rest))) {
      const word = m[1]
      const nextChar = code[i + word.length] || ''
      if (KEYWORDS.has(word)) push('keyword', word)
      else if (nextChar === '(') push('fn', word)
      else push('plain', word)
      i += word.length
      continue
    }
    if ((m = /^([@#$%^&*+\-=<>!?~:;,.()[\]{}|\\/]+)/.exec(rest))) { push('symbol', m[1]); i += m[1].length; continue }
    if ((m = /^(\s+)/.exec(rest))) { push('plain', m[1]); i += m[1].length; continue }
    push('plain', code[i])
    i += 1
  }
  return tokens
}

// ---------- 代码块复制 ----------
const copiedIndex = ref<number | null>(null)
function copyCode(text: string, index: number): void {
  navigator.clipboard.writeText(text).then(() => {
    copiedIndex.value = index
    window.setTimeout(() => { if (copiedIndex.value === index) copiedIndex.value = null }, 1600)
  }).catch(() => { /* 剪贴板不可用时静默 */ })
}

// ---------- 引用点击与图片预览 ----------
function onCitationClick(label: string): void {
  emit('citation', label)
}

const previewImage = ref<{ url: string; alt: string } | null>(null)
function openImagePreview(url: string, alt: string): void {
  previewImage.value = { url: safeImageUrl(url), alt }
}
function safeImageUrl(url: string): string {
  if (/^https?:\/\//i.test(url)) return url
  if (url.startsWith('/')) return url
  return `/api/v1/objects/${encodeURIComponent(url)}`
}
</script>

<template>
  <div class="answer-content">
    <template v-for="(block, blockIndex) in blocks" :key="blockIndex">
      <component
        :is="`h${Math.min(block.level + 1, 4)}`"
        v-if="block.type === 'heading'"
        :class="['answer-heading', `level-${block.level}`]"
      >
        <template v-for="(token, index) in inlineTokens(block.text)" :key="index">
          <span v-if="token.type === 'citation'" class="inline citation" role="button" tabindex="0" @click="onCitationClick(token.text)" @keydown.enter="onCitationClick(token.text)">{{ token.text }}</span>
          <img v-else-if="token.type === 'image'" class="inline-image" :src="safeImageUrl(token.url)" :alt="token.alt" loading="lazy" @click="openImagePreview(token.url, token.alt)" />
          <span v-else :class="['inline', token.type]">{{ token.text }}</span>
        </template>
      </component>

      <p v-else-if="block.type === 'paragraph'" class="answer-paragraph">
        <template v-for="(token, index) in inlineTokens(block.text)" :key="index">
          <span v-if="token.type === 'citation'" class="inline citation" role="button" tabindex="0" @click="onCitationClick(token.text)" @keydown.enter="onCitationClick(token.text)">{{ token.text }}</span>
          <img v-else-if="token.type === 'image'" class="inline-image" :src="safeImageUrl(token.url)" :alt="token.alt" loading="lazy" @click="openImagePreview(token.url, token.alt)" />
          <span v-else :class="['inline', token.type]">{{ token.text }}</span>
        </template>
      </p>

      <div v-else-if="block.type === 'summary'" class="answer-summary">
        <span class="summary-mark">✓</span>
        <p><template v-for="(token, index) in inlineTokens(block.text)" :key="index">
          <span v-if="token.type === 'citation'" class="inline citation" role="button" tabindex="0" @click="onCitationClick(token.text)" @keydown.enter="onCitationClick(token.text)">{{ token.text }}</span>
          <img v-else-if="token.type === 'image'" class="inline-image" :src="safeImageUrl(token.url)" :alt="token.alt" loading="lazy" @click="openImagePreview(token.url, token.alt)" />
          <span v-else :class="['inline', token.type]">{{ token.text }}</span>
        </template></p>
      </div>

      <blockquote v-else-if="block.type === 'quote'">
        <template v-for="(token, index) in inlineTokens(block.text)" :key="index">
          <span v-if="token.type === 'citation'" class="inline citation" role="button" tabindex="0" @click="onCitationClick(token.text)" @keydown.enter="onCitationClick(token.text)">{{ token.text }}</span>
          <img v-else-if="token.type === 'image'" class="inline-image" :src="safeImageUrl(token.url)" :alt="token.alt" loading="lazy" @click="openImagePreview(token.url, token.alt)" />
          <span v-else :class="['inline', token.type]">{{ token.text }}</span>
        </template>
      </blockquote>

      <ul v-else-if="block.type === 'unordered'" class="answer-list">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
          <template v-for="(token, index) in inlineTokens(item)" :key="index">
            <span v-if="token.type === 'citation'" class="inline citation" role="button" tabindex="0" @click="onCitationClick(token.text)" @keydown.enter="onCitationClick(token.text)">{{ token.text }}</span>
            <img v-else-if="token.type === 'image'" class="inline-image" :src="safeImageUrl(token.url)" :alt="token.alt" loading="lazy" @click="openImagePreview(token.url, token.alt)" />
            <span v-else :class="['inline', token.type]">{{ token.text }}</span>
          </template>
        </li>
      </ul>

      <ol v-else-if="block.type === 'ordered'" class="answer-list ordered">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
          <template v-for="(token, index) in inlineTokens(item)" :key="index">
            <span v-if="token.type === 'citation'" class="inline citation" role="button" tabindex="0" @click="onCitationClick(token.text)" @keydown.enter="onCitationClick(token.text)">{{ token.text }}</span>
            <img v-else-if="token.type === 'image'" class="inline-image" :src="safeImageUrl(token.url)" :alt="token.alt" loading="lazy" @click="openImagePreview(token.url, token.alt)" />
            <span v-else :class="['inline', token.type]">{{ token.text }}</span>
          </template>
        </li>
      </ol>

      <div v-else-if="block.type === 'table'" class="answer-table-wrap">
        <table class="answer-table">
          <thead><tr><th v-for="(header, index) in block.headers" :key="index">{{ header }}</th></tr></thead>
          <tbody><tr v-for="(row, rowIndex) in block.rows" :key="rowIndex"><td v-for="(cell, index) in row" :key="index">{{ cell }}</td></tr></tbody>
        </table>
      </div>

      <div v-else-if="block.type === 'code'" class="answer-code">
        <span v-if="block.language" class="code-language">{{ block.language }}</span>
        <button class="code-copy" :class="{ copied: copiedIndex === blockIndex }" @click="copyCode(block.text, blockIndex)">{{ copiedIndex === blockIndex ? '已复制' : '复制' }}</button>
        <pre><code><span v-for="(token, index) in tokenizeCode(block.text)" :key="index" :class="['tk', token.kind]">{{ token.text }}</span></code></pre>
      </div>

      <figure v-else-if="block.type === 'image'" class="answer-figure">
        <img :src="safeImageUrl(block.url)" :alt="block.alt" loading="lazy" @click="openImagePreview(block.url, block.alt)" />
        <figcaption v-if="block.alt">{{ block.alt }}</figcaption>
      </figure>

      <hr v-else-if="block.type === 'divider'" />
    </template>

    <div v-if="previewImage" class="image-preview-mask" @click.self="previewImage = null">
      <img :src="previewImage.url" :alt="previewImage.alt" @click="previewImage = null" />
      <button class="preview-close" @click="previewImage = null">×</button>
    </div>
  </div>
</template>

<style scoped>
.answer-content { color: #263248; font-size: 14px; line-height: 1.85; }
.answer-heading { margin: 24px 0 10px; color: #172238; font-weight: 700; line-height: 1.45; letter-spacing: -.01em; }
.answer-heading:first-child { margin-top: 0; }
.answer-heading.level-1 { font-size: 20px; }
.answer-heading.level-2 { font-size: 17px; }
.answer-heading.level-3,.answer-heading.level-4 { font-size: 15px; }
.answer-paragraph { margin: 0 0 14px; white-space: pre-line; }
.inline.strong { color: #16233a; font-weight: 700; }
.inline.code { margin: 0 2px; padding: 2px 5px; border: 1px solid #dce5f2; border-radius: 5px; color: #3155c6; background: #f4f7fc; font-family: ui-monospace,SFMono-Regular,Consolas,monospace; font-size: .9em; }
.inline.citation { display: inline-block; margin: 0 3px; padding: 1px 6px; border-radius: 9px; color: #1677ff; background: #eaf4ff; font-size: 11px; font-weight: 650; vertical-align: 1px; cursor: pointer; transition: background .15s, color .15s; }
.inline.citation:hover { background: #1677ff; color: #fff; }
.inline.citation:active { transform: translateY(1px); }
.inline-image { max-width: 100%; max-height: 24em; margin: 4px 0; border: 1px solid #e2e8f1; border-radius: 8px; vertical-align: top; cursor: zoom-in; transition: opacity .15s; }
.inline-image:hover { opacity: .92; }
.answer-summary { display: flex; gap: 10px; margin: 18px 0; padding: 13px 15px; border: 1px solid #dbe9ff; border-radius: 10px; background: linear-gradient(135deg,#f5f9ff,#fbfdff); }
.answer-summary p { margin: 0; }
.summary-mark { display: grid; place-items: center; width: 22px; height: 22px; flex: 0 0 22px; margin-top: 2px; border-radius: 50%; color: #fff; background: #3c8dff; font-size: 12px; font-weight: 700; }
.answer-list { margin: 5px 0 16px; padding-left: 22px; }
.answer-list li { margin: 7px 0; padding-left: 3px; }
.answer-list li::marker { color: #438df5; font-weight: 700; }
blockquote { margin: 14px 0; padding: 10px 14px; border-left: 3px solid #73a7ff; color: #52637b; background: #f7faff; }
hr { height: 1px; margin: 22px 0; border: 0; background: #edf0f5; }
.answer-table-wrap { max-width: 100%; margin: 14px 0 18px; overflow-x: auto; border: 1px solid #e2e8f1; border-radius: 10px; }
.answer-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.answer-table th,.answer-table td { padding: 10px 12px; border-bottom: 1px solid #e8edf4; text-align: left; vertical-align: top; }
.answer-table th { color: #34435a; background: #f5f7fb; font-weight: 650; white-space: nowrap; }
.answer-table tbody tr:last-child td { border-bottom: 0; }
.answer-code { position: relative; margin: 14px 0 18px; overflow: hidden; border: 1px solid #202d42; border-radius: 11px; background: #111b2b; }
.answer-code pre { max-width: 100%; margin: 0; padding: 18px; overflow-x: auto; color: #dbe7f6; font: 12px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace; }
.answer-code pre code { display: block; }
.code-language { position: absolute; top: 6px; right: 9px; color: #7f91aa; font-size: 9px; text-transform: uppercase; }
.code-copy { position: absolute; top: 6px; right: 60px; padding: 2px 8px; border: 1px solid #33445f; border-radius: 6px; color: #a5b6ce; background: rgba(26,38,58,.85); cursor: pointer; font-size: 9px; opacity: 0; transition: opacity .15s; }
.answer-code:hover .code-copy { opacity: 1; }
.code-copy.copied { color: #7ed4a0; border-color: #2f5c42; }
.tk.comment { color: #6b7c96; font-style: italic; }
.tk.string { color: #9ad48f; }
.tk.keyword { color: #79a9f5; }
.tk.number { color: #e5b567; }
.tk.fn { color: #62d0e6; }
.tk.symbol { color: #c9a9f2; }
.tk.plain { color: #dbe7f6; }
.answer-figure { margin: 14px 0 18px; text-align: center; }
.answer-figure img { max-width: 100%; max-height: 30em; border: 1px solid #e2e8f1; border-radius: 10px; cursor: zoom-in; transition: opacity .15s; }
.answer-figure img:hover { opacity: .92; }
.answer-figure figcaption { margin-top: 7px; color: #8c98a9; font-size: 11px; }
.image-preview-mask { position: fixed; z-index: 90; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(15,23,42,.78); cursor: zoom-out; }
.image-preview-mask img { max-width: 96vw; max-height: 92vh; border-radius: 10px; }
.preview-close { position: fixed; top: 14px; right: 18px; width: 34px; height: 34px; border: 0; border-radius: 50%; color: #fff; background: rgba(255,255,255,.14); cursor: pointer; font-size: 20px; }
@media (max-width: 700px) {
  .answer-content { font-size: 13px; line-height: 1.75; }
  .answer-heading.level-1 { font-size: 18px; }
  .answer-summary { padding: 11px 12px; }
  .inline-image { max-height: 18em; }
}
</style>
