#!/usr/bin/env bash
# Run from your own terminal: sudo bash scripts/repair-docker-host.sh
# Switch from conflicting Snap Docker to the already-installed Ubuntu daemon.
# No packages, Docker data directories, images, or volumes are deleted.
set -Eeuo pipefail
umask 077

if [[ $EUID -ne 0 ]]; then
    echo 'Run this script with sudo in your local terminal.' >&2
    exit 1
fi
for executable in /usr/bin/docker /usr/bin/dockerd /usr/bin/snap /usr/bin/systemctl; do
    [[ -x "$executable" ]] || { echo "Missing $executable" >&2; exit 1; }
done
exec 9>/run/lock/webhook-relay-docker-switch.lock
flock -n 9 || { echo 'Another Docker switch is running.' >&2; exit 1; }

relay_docker() {
    /usr/bin/docker --host unix:///run/docker.sock "$@"
}

root_dir=$(relay_docker info --format '{{.DockerRootDir}}')
if [[ "$root_dir" == /var/lib/docker ]] && ! systemctl is-active --quiet snap.docker.dockerd.service; then
    echo 'Native Docker is already selected; verifying container startup.'
    relay_docker run --rm --user 10001:10001 --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true python:3.12.12-slim-bookworm python --version
    exit 0
fi
if [[ "$root_dir" != /var/snap/docker/common/var-lib-docker ]]; then
    echo "Unexpected Docker data root: $root_dir. No changes made." >&2
    exit 1
fi
if [[ $(systemctl show docker.service -p FragmentPath --value) != /usr/lib/systemd/system/docker.service ]]; then
    echo 'Unexpected native Docker service configuration. No changes made.' >&2
    exit 1
fi
# Do not interrupt unrelated running projects in either daemon.
while IFS= read -r name; do
    if [[ -n "$name" && "$name" != webhook-relay-test-db-1 ]]; then
        echo "Unrelated container is running: $name. Stop it deliberately before switching." >&2
        exit 1
    fi
done < <(relay_docker ps --format '{{.Names}}')
if systemctl is-active --quiet docker.service; then
    command -v ctr >/dev/null || { echo 'ctr is required to check native tasks.' >&2; exit 1; }
    native_tasks=$(ctr --address /run/containerd/containerd.sock --namespace moby tasks list --quiet)
    if [[ -n "$native_tasks" ]]; then
        echo 'The native daemon has active containers. No changes made.' >&2
        exit 1
    fi
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="/var/backups/webhook-relay-docker-switch/$stamp"
install -d -m 0700 "$backup_dir"
trap 'echo "Repair stopped at line $LINENO. Inventory/image backup: $backup_dir. No Docker data was deleted. See docs/docker-host-repair.md for rollback." >&2' ERR
relay_docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}' > "$backup_dir/containers.txt"
relay_docker image ls --digests > "$backup_dir/images.txt"
relay_docker volume ls > "$backup_dir/volumes.txt"
systemctl cat docker.service docker.socket snap.docker.dockerd.service > "$backup_dir/services.txt"

# Copy only images needed for this project. Other projects remain in Snap's store.
images=()
for image in webhook-relay:0.1.0 webhook-relay-test:0.1.0 \
    python:3.12.12-slim-bookworm postgres:16-alpine ghcr.io/astral-sh/uv:0.11.16; do
    if relay_docker image inspect "$image" >/dev/null 2>&1; then
        images+=("$image")
    fi
done
if (( ${#images[@]} == 0 )); then
    echo 'Expected project images were not found. No service changes made.' >&2
    exit 1
fi
echo "Saving ${#images[@]} project/base images to $backup_dir/project-images.tar ..."
relay_docker image save --output "$backup_dir/project-images.tar" "${images[@]}"
sha256sum "$backup_dir/project-images.tar" > "$backup_dir/project-images.sha256"

if [[ $(relay_docker inspect --format '{{.State.Running}}' webhook-relay-test-db-1 2>/dev/null || true) == true ]]; then
    relay_docker stop --time 20 webhook-relay-test-db-1
fi
# The disposable test database uses tmpfs; its test rows are intentionally transient.
snap stop --disable docker
systemctl stop docker.service docker.socket
systemctl enable --now docker.socket docker.service

for _ in {1..30}; do
    if relay_docker info >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
new_root=$(relay_docker info --format '{{.DockerRootDir}}')
[[ "$new_root" == /var/lib/docker ]] || { echo "Unexpected native root: $new_root" >&2; exit 1; }
if systemctl is-active --quiet snap.docker.dockerd.service; then
    echo 'Snap Docker is still active; stop and investigate.' >&2
    exit 1
fi
relay_docker image load --input "$backup_dir/project-images.tar"
relay_docker run --rm --user 10001:10001 --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true python:3.12.12-slim-bookworm python --version
relay_docker compose version
relay_docker info --format 'Active Docker: {{.ServerVersion}}; data root: {{.DockerRootDir}}'
echo 'Host switch and hardened-container check passed.'
echo "Image backup and inventory: $backup_dir"
echo 'Snap data remains unchanged at /var/snap/docker/common/var-lib-docker.'
echo 'Other Snap projects have not been migrated; do not remove the Docker snap.'
echo 'Next, from the project directory as your normal user: make up && make test && make demo'
