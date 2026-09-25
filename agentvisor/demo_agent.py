"""Explicit simulation. Writes only the task's own documents; never calls a model."""
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from .i18n import translate
except ImportError:  # The supervisor also invokes this file directly.
    from i18n import translate

STEPS = ['Составить план задачи', 'Сохранить состояние первой итерации',
         'Проверить передачу контекста', 'Записать итог демонстрации']


def main():
    language = os.environ.get('AGENTVISOR_LANGUAGE', 'ru')
    message = lambda source: translate(source, language)
    root = Path(sys.argv[1])
    goal = (root / 'GOAL.md').read_text(encoding='utf-8')
    version = re.search(r'^goal_version:\s*(\d+)', goal, re.M)[1]
    progress = (root / 'PROGRESS.md').read_text(encoding='utf-8')
    done = min(progress.count('- [x] '), len(STEPS))
    print(json.dumps({'type': 'text', 'part': {'text': message(f'ДЕМО: выполняется шаг {min(done + 1, len(STEPS))}. Модель не используется.')}}), flush=True)
    time.sleep(2)
    done = min(done + 1, len(STEPS))
    content = f'# {message("Демонстрация")}\n\ngoal_version: {version}\n\n'
    content += '\n'.join(f'- [{"x" if i < done else " "}] {message(text)}' for i, text in enumerate(STEPS))
    content += '\n\n' + message('Это симуляция работы supervisor, не выполнение пользовательской цели.') + '\n'
    (root / 'PROGRESS.md').write_text(content, encoding='utf-8')
    print(json.dumps({'type': 'step_finish', 'part': {'reason': 'stop', 'tokens': {'output': 0}}}), flush=True)
    if done == len(STEPS):
        (root / 'DONE.md').write_text(f'goal_version: {version}\n\n' + message('Демонстрация завершена. Реальный агент не запускался.') + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
