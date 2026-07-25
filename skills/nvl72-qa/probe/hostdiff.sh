#!/bin/bash
set +e
echo "==================== $(hostname) ===================="
echo
echo "--- 1. kernel + image ---"
chroot /host uname -a
chroot /host cat /etc/os-release | head -6
chroot /host cat /etc/lsb-release 2>/dev/null

echo
echo "--- 2. nvidia module info ---"
chroot /host modinfo nvidia 2>&1 | grep -E "^(filename|version|srcversion|vermagic|parm):" | head -30

echo
echo "--- 3. loaded nvidia modules ---"
chroot /host lsmod | grep -iE "nvidia|nvswitch|ipvlan|nvidia_" | head

echo
echo "--- 4. nvidia kernel parameters (module params) ---"
for f in /host/sys/module/nvidia/parameters/*; do
  n=$(basename $f)
  v=$(cat $f 2>/dev/null)
  echo "  $n = $v"
done | head -60

echo
echo "--- 5. /proc/driver/nvidia/params ---"
chroot /host cat /proc/driver/nvidia/params 2>&1 | head -30

echo
echo "--- 6. nvidia-related services ---"
chroot /host systemctl list-units --type=service --state=loaded,failed 2>&1 | grep -iE "nvidia|gpu|fabric|persist|asapd" | head

echo
echo "--- 7. nvidia-persistenced ---"
chroot /host systemctl status nvidia-persistenced 2>&1 | head -15

echo
echo "--- 8. nvidia-fabricmanager ---"
chroot /host systemctl status nvidia-fabricmanager 2>&1 | head -15
chroot /host ls /var/log/fabricmanager* 2>/dev/null | head

echo
echo "--- 9. nvidia processes ---"
chroot /host ps -ef | grep -iE "nvidia|fabric|persist|asapd" | grep -v grep | head

echo
echo "--- 10. /dev/nvidia* ---"
chroot /host ls -la /dev/nvidia* 2>&1 | head -20
chroot /host ls -la /dev/nvidia-caps/ 2>&1 | head
chroot /host ls -la /dev/nvidia-caps-imex-channels/ 2>&1 | head -5

echo
echo "--- 11. /proc/driver/nvidia/gpus/*/information ---"
for f in /host/proc/driver/nvidia/gpus/*/information; do
  echo "  === $f ==="
  cat $f 2>/dev/null | head -12
done | head -80

echo
echo "--- 12. nvidia-smi persistence + fabric ---"
chroot /host nvidia-smi -q 2>&1 | grep -E "Persistence Mode|Fabric|CliqueId|GPU Fabric|State|Status" | head -30

echo
echo "--- 13. dmesg last 40 lines nvidia/nvrm/nvswitch ---"
chroot /host dmesg -T 2>&1 | grep -iE "nvidia|nvrm|nvswitch|nvfabric" | tail -40

echo
echo "--- 14. containerd + nvidia-container-toolkit versions ---"
chroot /host containerd --version 2>&1
chroot /host nvidia-ctk --version 2>&1
chroot /host cat /etc/nvidia-container-runtime/config.toml 2>&1 | head -40

echo
echo "--- 15. cgroup / OCI hook config ---"
chroot /host ls /etc/nvidia-container-runtime/host-files-for-container.d/ 2>&1
chroot /host cat /etc/containerd/config.toml 2>&1 | grep -iE "nvidia|runtime" | head

echo
echo "--- 16. GKE nvidia-* pod / daemonset presence ---"
chroot /host ls /etc/kubernetes/manifests/ 2>&1 | head
chroot /host ls /home/kubernetes/bin/nvidia/ 2>&1 | head

echo
echo "--- 17. asapd-lite + accelerator profile status ---"
chroot /host find /home /var /opt -name '*asapd*' 2>/dev/null | head
