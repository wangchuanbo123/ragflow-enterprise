/** Branding for system metadata. Never apply this to request IDs or source evidence. */
export function codecodeText(value: string): string {
  return value.replace(/opencode/gi, match => match === 'OPENCODE' ? 'CODECODE' : 'codecode')
}

const referenceField = /(?:^id$|_ids?$|^url$|_url$|^uri$|^model$|^model_id$|^href$|^path$|_path$|^fingerprint$|_fingerprint$|^instructions$|^content$|^quote$|^preview$|^question$|^reference_answer$)/i

export function isSystemReferenceField(key: string): boolean {
  return referenceField.test(key)
}

/** Make a presentation copy of system diagnostics without changing operational references. */
export function codecodeSystemData<T>(value: T): T {
  if (typeof value === 'string') return codecodeText(value) as T
  if (Array.isArray(value)) return value.map(item => codecodeSystemData(item)) as T
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key,
      isSystemReferenceField(key) ? item : codecodeSystemData(item),
    ])) as T
  }
  return value
}
