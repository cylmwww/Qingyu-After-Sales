export type SSEEvent = { event: string; data: unknown }
export class SSEParser {
  private buffer = ''
  feed(chunk: string): SSEEvent[] {
    this.buffer += chunk
    const events: SSEEvent[] = []
    let match: RegExpExecArray | null
    while ((match = /\r?\n\r?\n/.exec(this.buffer))) {
      const block = this.buffer.slice(0, match.index)
      this.buffer = this.buffer.slice(match.index + match[0].length)
      const lines = block.split(/\r?\n/)
      const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
      if (data) events.push({ event: lines.find(line => line.startsWith('event:'))?.slice(6).trim() || 'message', data: JSON.parse(data) })
    }
    return events
  }
}
