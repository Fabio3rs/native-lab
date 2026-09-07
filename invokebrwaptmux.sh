#!/usr/bin/env bash

# Usando $PWD/run como socket do tmux para teste de conceito

bwrap \
  --unshare-user \
  --unshare-pid \
  --unshare-ipc \
  --unshare-net \
  --unshare-uts \
  --new-session \
  --cap-drop ALL \
  --ro-bind / / \
  --bind "$PWD" "$PWD" \
  --tmpfs /tmp \
  --tmpfs /run \
  --proc /proc \
  --dev /dev \
  --chdir "$PWD" \
  -- tmux -D -S "$PWD/run/lab-tmux.sock"



