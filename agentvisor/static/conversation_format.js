// This formatter deliberately accepts a small Markdown subset and never raw HTML.
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character =>
 ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));

function inline(source) {
 const tokens = [], protect = html => `\u0000${tokens.push(html)-1}\u0000`;
 let text = String(source).replace(/\u0000/g, '');
 text = text.replace(/`([^`\n]+)`/g, (_,code) => protect(`<code>${escapeHtml(code)}</code>`));
 text = text.replace(/\[([^\]\n]+)\]\(([^\s)]+)\)/g, (whole,label,url) => {
  try {
   const parsed = new URL(url);
   if (!['http:','https:'].includes(parsed.protocol)) return whole;
   return protect(`<a href="${escapeHtml(parsed.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
  } catch { return whole; }
 });
 text = escapeHtml(text).replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
 return text.replace(/\u0000(\d+)\u0000/g, (_,index) => tokens[Number(index)] || '');
}

export function renderMarkdown(value) {
 const lines = String(value ?? '').replace(/\r\n?/g,'\n').split('\n');
 const output = []; let paragraph = [], list = null, code = null, fence = '', language = '';
 const flushParagraph = () => { if (paragraph.length) output.push(`<p>${paragraph.map(inline).join('<br>')}</p>`); paragraph = []; };
 const flushList = () => { if (list) output.push(`<${list.type}>${list.items.map(line => `<li>${inline(line)}</li>`).join('')}</${list.type}>`); list = null; };
 const flushCode = () => { output.push(`<pre><code${language ? ` data-language="${escapeHtml(language)}"` : ''}>${escapeHtml(code.join('\n'))}</code></pre>`); code = null; };
 for (const line of lines) {
  if (code !== null) { if (line.trim().startsWith(fence)) flushCode(); else code.push(line); continue; }
  const opening = line.match(/^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$/);
  if (opening) { flushParagraph(); flushList(); fence = opening[1]; language = opening[2]; code = []; continue; }
  if (!line.trim()) { flushParagraph(); flushList(); continue; }
  const heading = line.match(/^#{1,6}\s+(.+)$/);
  if (heading) { flushParagraph(); flushList(); output.push(`<h3>${inline(heading[1])}</h3>`); continue; }
  const bullet = line.match(/^\s*(?:([-*+])|\d+[.)])\s+(.+)$/);
  if (bullet) {
   flushParagraph(); const type = bullet[1] ? 'ul' : 'ol';
   if (list?.type !== type) { flushList(); list = {type,items:[]}; }
   list.items.push(bullet[2]); continue;
  }
  flushList(); paragraph.push(line);
 }
 flushParagraph(); flushList(); if (code !== null) flushCode();
 return output.join('');
}

export function timestamp(value) {
 if (typeof value === 'number') return Number.isFinite(value) ? (value > 1e12 ? value : value * 1000) : 0;
 const result = Date.parse(value); return Number.isFinite(result) ? result : 0;
}

export function mergeItems(existing, incoming) {
 const merged = new Map(existing.map(item => [String(item.id),item]));
 for (const item of incoming) {
  if (item?.id == null) continue;
  const previous = merged.get(String(item.id));
  if (!previous || !previous.updated_at || !item.updated_at || timestamp(item.updated_at) >= timestamp(previous.updated_at)) merged.set(String(item.id),item);
 }
 const orderKey = item => item.role === 'user' ? 'U:'+String(item.details?.version ?? String(item.id).split(':').at(-1)).padStart(20,'0') :
  'E:'+String(item.details?.first_event_id ?? String(item.id).split(':').at(-1)).padStart(20,'0');
 return [...merged.values()].sort((a,b) => timestamp(a.time)-timestamp(b.time) || orderKey(a).localeCompare(orderKey(b),undefined,{numeric:true}));
}

export function draftPayload(draft) {
 const kind = draft.kind === 'reference' ? 'reference' : 'instruction';
 return {text:String(draft.text || '').trim(),kind,recheck:kind === 'instruction' && !!draft.recheck};
}

export function samePayload(left, right) { return JSON.stringify(draftPayload(left)) === JSON.stringify(draftPayload(right)); }
export function submissionAttempt(draft, createId) {
 const payload = draftPayload(draft);
 return draft.attempt && samePayload(draft.attempt,payload) ? draft.attempt : {...payload,client_message_id:createId()};
}

export function composerLimit(composer = {}, text = '') {
 const perMessage = composer.message_max_chars ?? 6000;
 const remaining = composer.chars_remaining ?? composer.max_total_chars ?? 20000;
 const size = [...String(text).trim()].length;
 return {size,maximum:Math.min(perMessage,remaining),full:composer.messages_remaining === 0 || remaining === 0,
  canSend:composer.can_send !== false && composer.messages_remaining !== 0 && size > 0 && size <= perMessage && size <= remaining};
}
