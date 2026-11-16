/** Storage/API page coordinates are zero based. Only presentation is one based. */
export function pageLabel(page: number | string | null | undefined): string {
  if (page === null || page === undefined || page === '') return ''
  const value = Number(page)
  return Number.isInteger(value) && value >= 0 ? `第 ${value + 1} 页` : String(page)
}

export const documentAccept = '.txt,.pdf,.md,.docx,.html,.htm'
export const supportedDocumentFormats = 'TXT、PDF、Markdown、DOCX、HTML'

export function documentUploadError(file: Pick<File, 'name' | 'size'>): string | undefined {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!documentAccept.split(',').includes(extension)) return `不支持此文件格式；支持 ${supportedDocumentFormats}。截图请先转为 PDF。`
  if (!file.size) return '文件为空，请选择包含内容的文档。'
  return undefined
}
