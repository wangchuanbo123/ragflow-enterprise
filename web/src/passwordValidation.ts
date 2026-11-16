export function passwordError(password: string): string {
  const characters = [...password].length
  const bytes = new TextEncoder().encode(password).length
  if (characters < 12 || bytes > 72) return '密码至少 12 个字符，且 UTF-8 编码不能超过 72 字节'
  if (!/\p{L}/u.test(password) || !/[0-9]/.test(password)) return '密码须同时包含字母和数字（0–9）'
  return ''
}
