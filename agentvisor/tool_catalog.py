"""Expose optional MCP schemas on demand, preserving instructions and history."""
import json
import re
import threading


START = '<agentvisor-tool-catalogue>\n'
END = '\n</agentvisor-tool-catalogue>\n\n'
NATIVE = {'bash', 'edit', 'glob', 'grep', 'read', 'write', 'skill', 'task', 'todowrite',
          'todoread', 'webfetch', 'websearch', 'question', 'lsp', 'patch', 'apply_patch',
          'list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource'}
DESCRIPTIONS = {
    'blender': 'Blender scene inspection, modelling, scripting and rendering.',
    'browseros-neo': 'Browser navigation, page inspection, interaction and screenshots.',
}
SKILLS = re.compile(r'<available_skills>\s*(.*?)\s*</available_skills>', re.S)
SKILL = re.compile(r'\s*<skill>\s*<name>([^<>]+)</name>\s*'
                   r'<description>(.*?)</description>\s*'
                   r'<location>([^<>]+)</location>\s*</skill>\s*', re.S)


def compact_skills(text):
    """Abbreviate only a recognized provider catalogue, never loaded skill text.

    Unknown children, malformed entries, or extra instructions inside the block
    make it ineligible. Skill names remain verbatim and native ``skill`` resolves
    their original locations and returns the full instructions on demand.
    """
    def replace(match):
        inner = match.group(1)
        rows, cursor = [], 0
        for entry in SKILL.finditer(inner):
            if inner[cursor:entry.start()].strip():
                return match.group(0)
            name, description, _ = entry.groups()
            if '\n' in name or len(name) > 200:
                return match.group(0)
            brief = ' '.join(description.split())
            if len(brief) > 100:
                brief = brief[:97].rsplit(' ', 1)[0] + '…'
            rows.append({'name': name, 'description': brief})
            cursor = entry.end()
        if not rows or inner[cursor:].strip():
            return match.group(0)
        # Identical repeated metadata adds no capability. Different descriptions
        # of a same-name skill are retained; the native tool owns resolution.
        unique = list({(row['name'], row['description']): row for row in rows}.values())
        return ('<available_skills>\nAbbreviated discovery metadata only. Use the native skill '
                'tool with a listed name to read its complete instructions before applying it.\n' +
                json.dumps(unique, ensure_ascii=False, separators=(',', ':')) + '\n</available_skills>')
    return SKILLS.sub(replace, text)


def _name(tool):
    return (tool.get('function') or {}).get('name', '')


def _group(name):
    if not name or name in NATIVE or name.startswith('agentvisor_process_'):
        return None
    if name.startswith('mcp__'):
        parts = name.split('__', 2)
        return parts[1] if len(parts) == 3 and parts[1] and parts[1] != 'agentvisor_process' else None
    if '_' in name:
        return name.split('_', 1)[0]
    # An unprefixed or unknown native tool cannot safely be classified as MCP.
    return None


def _forced_name(body):
    choice = body.get('tool_choice')
    if isinstance(choice, dict):
        return (choice.get('function') or {}).get('name') or choice.get('name')
    return None


class ToolCatalog:
    """Keep native/supervisor tools plus at most one optional MCP server active."""

    def __init__(self):
        self._groups = {}
        self._active = None
        self._lock = threading.RLock()
        self._markers = set()

    @property
    def catalogue(self):
        with self._lock:
            return [{'name': name, 'description': DESCRIPTIONS.get(name, 'Optional tools from ' + name + '.'),
                     'tool_count': len(tools), 'active': name == self._active}
                    for name, tools in sorted(self._groups.items())]

    def select(self, name):
        with self._lock:
            if name == 'none':
                self._active = None
            elif name in self._groups:
                self._active = name
            else:
                raise ValueError('Unknown toolset. Available: ' + ', '.join(sorted(self._groups)) + ', none')
            return {'active_toolset': self._active,
                    'tools': sorted(self._groups.get(self._active, {})),
                    'available_toolsets': [item['name'] for item in self.catalogue]}

    def shape(self, body):
        """Mutate only declared schemas/catalogue metadata; return size diagnostics.

        Missing schemas are never re-added: the instruction/review gate may have
        intentionally limited this request. No-tools auxiliary requests pass
        through unchanged, and explicit named tool_choice activates its server.
        """
        if not body.get('tools'):
            return {'tools_before': 0, 'tools_after': 0, 'skills_chars_saved': 0}
        with self._lock:
            tools = body['tools']
            for tool in tools:
                name = _name(tool)
                group = _group(name)
                if group:
                    self._groups.setdefault(group, {})[name] = tool
            forced = _forced_name(body)
            forced_group = _group(forced)
            if forced_group and any(_name(tool) == forced for tool in tools):
                self._active = forced_group
            body['tools'] = [tool for tool in tools if _group(_name(tool)) in {None, self._active}]
            marker = ''
            if self._groups:
                marker = (START + 'Native and supervisor tools remain available. Optional MCP toolsets '
                          'are loaded on demand to conserve context; their schemas are not lost. '
                          'Before using a toolset call agentvisor_process_select_toolset(name); its '
                          'tools become available on the next request. Only one optional toolset is '
                          'active at a time. Select "none" to unload it.\n' +
                          json.dumps(self.catalogue, ensure_ascii=False, separators=(',', ':')) + END)
                self._markers.add(marker)
            saved = 0
            messages = body.setdefault('messages', [])
            placed = False
            for index, message in enumerate(messages):
                if message.get('role') != 'system':
                    continue
                content = message.get('content')
                if isinstance(content, str):
                    text, reduction = self._shape_text(content)
                    saved += reduction
                    messages[index] = dict(message, content=(marker if not placed else '') + text)
                    placed = True
                elif isinstance(content, list):
                    parts = []
                    for part in content:
                        if part.get('type') == 'text' and isinstance(part.get('text'), str):
                            text, reduction = self._shape_text(part['text'])
                            saved += reduction
                            parts.append(dict(part, text=text))
                        else:
                            parts.append(part)
                    if not placed and marker:
                        parts.insert(0, {'type': 'text', 'text': marker})
                    messages[index] = dict(message, content=parts)
                    placed = True
            if marker and not placed:
                messages.insert(0, {'role': 'system', 'content': marker})
            return {'tools_before': len(tools), 'tools_after': len(body['tools']),
                    'active_toolset': self._active, 'skills_chars_saved': saved,
                    'available_toolsets': [item['name'] for item in self.catalogue]}

    def _shape_text(self, text):
        # Remove only exact discovery blocks produced by this controller, not
        # arbitrary project instructions quoting similarly named XML tags.
        for marker in self._markers:
            text = text.replace(marker, '')
        compact = compact_skills(text)
        return compact, len(text) - len(compact)
