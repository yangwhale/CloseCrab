#!/bin/bash
# 已经在 gLinux 上就直接跑，不要 ssh 自己一圈。
if [ -d /usr/local/google/home/chrisya ]; then
  exec bugged "$@"
else
  exec ssh -T glinux bugged "$@"
fi
