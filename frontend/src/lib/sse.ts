import type { Item, Meta, SearchPayload } from '../types';

export interface SSEEvent {
  event: string;
  data: string;
}

/**
 * Stateful SSE parser. `feed()` is called with each network chunk (which may
 * split an event mid-frame or contain several frames) and returns whichever
 * complete events are now available. The partial tail is buffered until the
 * rest of it arrives — this is the bit that's easy to get wrong with SSE-over-fetch.
 */
export function createSSEParser() {
  let buffer = '';

  function parseFrame(raw: string): SSEEvent | null {
    let event = 'message';
    const dataLines: string[] = [];
    for (const line of raw.split('\n')) {
      if (line === '' || line.startsWith(':')) continue; // blank / comment
      const sep = line.indexOf(':');
      const field = sep === -1 ? line : line.slice(0, sep);
      // SSE: a single leading space after the colon is stripped.
      const value = sep === -1 ? '' : line.slice(sep + 1).replace(/^ /, '');
      if (field === 'event') event = value;
      else if (field === 'data') dataLines.push(value);
    }
    return dataLines.length ? { event, data: dataLines.join('\n') } : null;
  }

  return function feed(chunk: string): SSEEvent[] {
    // Normalize CRLF/CR to LF so frame splitting is uniform.
    buffer += chunk.replace(/\r\n?/g, '\n');
    const events: SSEEvent[] = [];
    let boundary = buffer.indexOf('\n\n');
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseFrame(frame);
      if (parsed) events.push(parsed);
      boundary = buffer.indexOf('\n\n');
    }
    return events;
  };
}

export interface StreamHandlers {
  onItem: (item: Item) => void;
  onMeta: (meta: Meta) => void;
}

/**
 * POST the search payload and consume the SSE response body. EventSource can't
 * POST, so we read the ReadableStream ourselves and drive the chunk-safe parser.
 */
export async function streamSearch(
  apiBase: string,
  payload: SearchPayload,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${apiBase}/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`search failed: ${res.status} ${res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const feed = createSSEParser();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    for (const ev of feed(decoder.decode(value, { stream: true }))) {
      if (ev.event === 'item') handlers.onItem(JSON.parse(ev.data) as Item);
      else if (ev.event === 'meta') handlers.onMeta(JSON.parse(ev.data) as Meta);
    }
  }
}
