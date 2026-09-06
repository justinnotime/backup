# Generic Bash selection primitives; bindings are generated from caller configuration.
_rl_file() {
  local target=$1 candidate; shift
  if [[ -e "$target" ]]; then printf '%s\n' "$target"; return; fi
  for candidate in "$@"; do
    if [[ -e "$candidate" ]]; then _rl_note "$candidate"; printf '%s\n' "$candidate"; return; fi
  done
  printf '%s\n' "$target"
}
_rl_active_choose() {
  if _rl_active; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi
}
_rl_content() {
  if [[ -d "$1" && -n "$(ls -A "$1" 2>/dev/null)" ]]; then printf '%s\n' "$1"; return; fi
  if [[ -d "$2" && -n "$(ls -A "$2" 2>/dev/null)" ]]; then _rl_note "$2"; printf '%s\n' "$2"; return; fi
  _rl_active_choose "$1" "$2"
}
_rl_glob() {
  local pattern=$1 target=$2 candidate; shift 2
  if compgen -G "$target/$pattern" >/dev/null; then printf '%s\n' "$target"; return; fi
  for candidate in "$@"; do
    if compgen -G "$candidate/$pattern" >/dev/null; then _rl_note "$candidate"; printf '%s\n' "$candidate"; return; fi
  done
  printf '%s\n' "$target"
}
