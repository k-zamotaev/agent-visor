// Russian source messages are the stable keys. Only trusted UI literals are translated.
const response = await fetch('/static/locales/en.json');
if (!response.ok) throw new Error('Could not load the language catalog');
const english = await response.json();
let language = 'ru';
try { language = localStorage.getItem('agentvisor-language') === 'en' ? 'en' : 'ru'; } catch {}
const escapeRegex = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const literalKeys = Object.keys(english).filter(key => !/\{\d+\}/.test(key)).sort((a,b) => b.length-a.length);
const literalPattern = new RegExp(literalKeys.map(escapeRegex).join('|'), 'g');
const staticNodes = [], staticAttributes = [];

export const getLanguage = () => language;
export const locale = () => language === 'en' ? 'en-US' : 'ru-RU';
export function t(source) { return language === 'en' ? english[source] ?? source : source; }
export function markup(source) {
  return language === 'en' ? source.replace(literalPattern, key => english[key]) : source;
}
// Translate literal pieces only: interpolated project names, model text and paths are untouched.
export function tr(strings, ...values) {
  const key = strings.map((part,index) => part + (index < values.length ? `{${index}}` : '')).join('');
  if (language === 'en' && Object.hasOwn(english,key)) {
    return english[key].replace(/\{(\d+)\}/g, (_,index) => String(values[Number(index)]));
  }
  return strings.map((part,index) => markup(part) + (index < values.length ? values[index] : '')).join('');
}
export function captureStatic(root=document) {
  const walker = document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (/[А-Яа-яЁё]/.test(node.data) && !node.parentElement?.closest('script,style,textarea,pre')) {
      staticNodes.push([node,node.data]);
    }
  }
  root.querySelectorAll('[aria-label],[placeholder],[title]').forEach(element => {
    for (const name of ['aria-label','placeholder','title']) {
      const value = element.getAttribute(name);
      if (value && /[А-Яа-яЁё]/.test(value)) staticAttributes.push([element,name,value]);
    }
  });
  applyStatic();
}
function applyStatic() {
  document.documentElement.lang = language;
  for (const [node,source] of staticNodes) if (node.isConnected) node.data = markup(source);
  for (const [element,name,source] of staticAttributes) if (element.isConnected) element.setAttribute(name,markup(source));
}
export function setLanguage(value) {
  if (!['ru','en'].includes(value)) throw new Error('Unsupported language');
  language = value;
  try { localStorage.setItem('agentvisor-language',language); } catch {}
  applyStatic();
}
