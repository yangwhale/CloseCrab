#!/bin/bash
# bwrap-passthrough.sh — 替代真实 bwrap，直接执行命令不做沙箱隔离
# 安装方法:
#   sudo mv /usr/bin/bwrap /usr/bin/bwrap.real
#   sudo cp this-script /usr/bin/bwrap
#   sudo chmod +x /usr/bin/bwrap
# 恢复方法:
#   sudo mv /usr/bin/bwrap.real /usr/bin/bwrap

# 解析 bwrap 参数，提取 --setenv 和 -- 后的实际命令
while [[ $# -gt 0 ]]; do
    case "$1" in
        --)
            shift
            exec "$@"
            ;;
        --setenv)
            export "$2=$3"
            shift 3
            ;;
        --unsetenv)
            unset "$2"
            shift 2
            ;;
        # 跳过所有其他 bwrap 参数
        --ro-bind|--bind|--dev-bind|--tmpfs)
            shift 2  # 这些参数带两个值
            ;;
        --ro-bind-try|--bind-try|--dev-bind-try)
            shift 2
            ;;
        --symlink)
            shift 2
            ;;
        --proc|--dev)
            shift 1  # 这些参数带一个值
            ;;
        --dir|--file|--bind-data|--ro-bind-data)
            shift 2
            ;;
        --remount-ro)
            shift 1
            ;;
        --chmod)
            shift 2
            ;;
        # 无参数标志，直接跳过
        --new-session|--die-with-parent|--unshare-pid|--unshare-net|--unshare-user|--unshare-ipc|--unshare-uts|--unshare-cgroup|--share-net|--as-pid-1|--clearenv)
            shift
            ;;
        --cap-add|--cap-drop)
            shift 1
            ;;
        --seccomp|--exec-label|--file-label|--userns|--userns2|--pidns|--hostname|--uid|--gid|--lock-file|--sync-fd|--info-fd|--json-status-fd|--perms)
            shift 2
            ;;
        *)
            shift  # 未知参数，跳过
            ;;
    esac
done

echo "bwrap-passthrough: no command found after --" >&2
exit 1
