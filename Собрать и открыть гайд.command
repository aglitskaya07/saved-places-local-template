#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if ! command -v uv >/dev/null; then
  print "Нужен uv. Открой этот проект в Codex и файл START_HERE.md."
else
  uv run --frozen python -m saved_places.build --open "$@"
fi
read "?Нажми Enter, чтобы закрыть окно. "
