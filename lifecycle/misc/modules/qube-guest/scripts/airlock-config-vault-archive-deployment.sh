set -eu

repo="${AIRLOCK_CONFIG_ARCHIVE_PATH:-@NIX_0@}"
tmpdir="$(@NIX_1@/bin/mktemp -d)"
trap '@NIX_1@/bin/rm -rf "$tmpdir"' EXIT

deployment_tar="$tmpdir/deployment.tar"
@NIX_1@/bin/cat > "$deployment_tar"
@NIX_2@/bin/tar -tf "$deployment_tar" |
  @NIX_3@/bin/awk '
    $0 != "deployment.json" &&
    $0 != "apply.json" &&
    $0 != "archive.json" &&
    $0 != "setup.json" &&
    $0 != "topology.json" &&
    $0 != "validated-topology.json" &&
    $0 != "template-builds.json" &&
    $0 != "nix-domain-builds.json" &&
    $0 != "nix-domain-materializations.json" &&
    $0 != "unsupported-domain-backends.json" &&
    $0 != "domain-references.json" &&
    $0 != "system.json" &&
    $0 != "resolved-system.json" &&
    $0 != "system-plan.json" &&
    $0 != "system-apply.json" &&
    $0 != "system-builds.json" &&
    $0 != "systemd-actions.json" &&
    $0 != "validation.json" &&
    $0 != "manifest.json" &&
    $0 != "logs/apply.commands.log" &&
    $0 != "logs/archive.stdout.log" &&
    $0 != "logs/archive.stderr.log" &&
    $0 != "logs/setup.stdout.log" &&
    $0 != "logs/setup.stderr.log" &&
    $0 != "logs/template-builds.log" &&
    $0 != "logs/nix-domain-builds.log" &&
    $0 != "logs/system-builds.log" &&
    $0 != "logs/system-apply.log" &&
    $0 != "logs/profile-activation.log" &&
    $0 != "logs/host-system-activation.log" &&
    $0 != "logs/systemd-actions.log" &&
    $0 != "logs/clone.log" &&
    $0 != "logs/checkout.log" {
      printf "unexpected deployment tar member: %s\n", $0 > "/dev/stderr"
      exit 1
    }
  '
@NIX_2@/bin/tar -C "$tmpdir" -xf "$deployment_tar"

for required in \
  deployment.json \
  apply.json \
  archive.json \
  setup.json \
  topology.json \
  validated-topology.json \
  template-builds.json \
  nix-domain-builds.json \
  nix-domain-materializations.json \
  unsupported-domain-backends.json \
  domain-references.json \
  system.json \
  resolved-system.json \
  system-plan.json \
  system-apply.json \
  system-builds.json \
  systemd-actions.json \
  validation.json \
  manifest.json; do
  [ -f "$tmpdir/$required" ] || {
    printf 'deployment tar is missing %s\n' "$required" >&2
    exit 1
  }
done

deployment_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/deployment.json")"
deployment_id="$(@NIX_4@/bin/jq -r '.deployment_id' "$tmpdir/deployment.json")"
deployment_status="$(@NIX_4@/bin/jq -r '.status' "$tmpdir/deployment.json")"
candidate_commit="$(@NIX_4@/bin/jq -r '.candidate_commit' "$tmpdir/deployment.json")"
apply_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/apply.json")"
apply_commit="$(@NIX_4@/bin/jq -r '.candidate_commit' "$tmpdir/apply.json")"
archive_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/archive.json")"
archive_status="$(@NIX_4@/bin/jq -r '.status' "$tmpdir/archive.json")"
archive_commit="$(@NIX_4@/bin/jq -r '.candidate_commit' "$tmpdir/archive.json")"
setup_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/setup.json")"
topology_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/topology.json")"
validated_topology_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/validated-topology.json")"
template_builds_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/template-builds.json")"
nix_domain_builds_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/nix-domain-builds.json")"
nix_domain_materializations_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/nix-domain-materializations.json")"
unsupported_domain_backends_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/unsupported-domain-backends.json")"
system_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/system.json")"
resolved_system_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/resolved-system.json")"
system_apply_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/system-apply.json")"
system_apply_status="$(@NIX_4@/bin/jq -r '.status' "$tmpdir/system-apply.json")"
system_builds_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/system-builds.json")"
systemd_actions_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/systemd-actions.json")"
validation_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/validation.json")"
validation_status="$(@NIX_4@/bin/jq -r '.status' "$tmpdir/validation.json")"
validation_commit="$(@NIX_4@/bin/jq -r '.commit' "$tmpdir/validation.json")"

domain_references_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/domain-references.json")"
domain_references_status="$(@NIX_4@/bin/jq -r '.status' "$tmpdir/domain-references.json")"

[ "$deployment_schema" = "airlock.config-deployment.v1" ] || {
  printf 'unsupported deployment schema: %s\n' "$deployment_schema" >&2
  exit 1
}
case "$deployment_id" in
  ""|*[!A-Za-z0-9_.:-]*)
    printf 'invalid deployment id: %s\n' "$deployment_id" >&2
    exit 1
    ;;
esac
case "$deployment_status" in
  passed|failed) ;;
  *)
    printf 'invalid deployment status: %s\n' "$deployment_status" >&2
    exit 1
    ;;
esac
[ -n "$candidate_commit" ] && [ "$candidate_commit" != null ] || {
  printf 'deployment record is missing candidate_commit\n' >&2
  exit 1
}
[ "$apply_schema" = "airlock.config-apply.v1" ] || {
  printf 'unsupported apply schema: %s\n' "$apply_schema" >&2
  exit 1
}
[ "$archive_schema" = "airlock.config-archive.v1" ] || {
  printf 'unsupported archive schema: %s\n' "$archive_schema" >&2
  exit 1
}
[ "$archive_status" = passed ] || {
  printf 'candidate archive did not pass: %s\n' "$archive_status" >&2
  exit 1
}
[ "$setup_schema" = "airlock.setup-config.v1" ] || {
  printf 'unsupported setup schema: %s\n' "$setup_schema" >&2
  exit 1
}
[ "$topology_schema" = "airlock.qubes-topology.v1" ] || {
  printf 'unsupported topology schema: %s\n' "$topology_schema" >&2
  exit 1
}
[ "$validated_topology_schema" = "airlock.qubes-topology.v1" ] || {
  printf 'unsupported validated topology schema: %s\n' "$validated_topology_schema" >&2
  exit 1
}
[ "$template_builds_schema" = "airlock.template-builds.v1" ] || {
  printf 'unsupported template builds schema: %s\n' "$template_builds_schema" >&2
  exit 1
}
[ "$nix_domain_builds_schema" = "airlock.nix-domain-builds.v1" ] || {
  printf 'unsupported Nix-native domain builds schema: %s\n' "$nix_domain_builds_schema" >&2
  exit 1
}
[ "$nix_domain_materializations_schema" = "airlock.nix-domain-materializations.v1" ] || {
  printf 'unsupported Nix-native domain materializations schema: %s\n' "$nix_domain_materializations_schema" >&2
  exit 1
}
[ "$unsupported_domain_backends_schema" = "airlock.unsupported-domain-backends.v1" ] || {
  printf 'unsupported domain backend report schema: %s\n' "$unsupported_domain_backends_schema" >&2
  exit 1
}
[ "$domain_references_schema" = "airlock.domain-references.v1" ] || {
  printf 'unsupported domain references schema: %s\n' "$domain_references_schema" >&2
  exit 1
}
[ "$domain_references_status" = passed ] || {
  printf 'domain references did not pass: %s\n' "$domain_references_status" >&2
  exit 1
}
[ "$system_schema" = "airlock.system.v1" ] || {
  printf 'unsupported system schema: %s\n' "$system_schema" >&2
  exit 1
}
[ "$resolved_system_schema" = "airlock.system.v1" ] || {
  printf 'unsupported resolved system schema: %s\n' "$resolved_system_schema" >&2
  exit 1
}
[ "$system_apply_schema" = "airlock.system-apply.v1" ] || {
  printf 'unsupported system apply schema: %s\n' "$system_apply_schema" >&2
  exit 1
}
[ "$system_apply_status" = passed ] || {
  printf 'system apply did not pass: %s\n' "$system_apply_status" >&2
  exit 1
}
[ "$system_builds_schema" = "airlock.system-builds.v1" ] || {
  printf 'unsupported system builds schema: %s\n' "$system_builds_schema" >&2
  exit 1
}
[ "$systemd_actions_schema" = "airlock.systemd-actions.v1" ] || {
  printf 'unsupported systemd actions schema: %s\n' "$systemd_actions_schema" >&2
  exit 1
}
[ "$validation_schema" = "airlock.config-validation.v1" ] || {
  printf 'unsupported validation schema: %s\n' "$validation_schema" >&2
  exit 1
}
[ "$validation_status" = passed ] || {
  printf 'candidate validation did not pass: %s\n' "$validation_status" >&2
  exit 1
}
[ "$apply_commit" = "$candidate_commit" ] || {
  printf 'apply commit %s does not match deployment commit %s\n' \
    "$apply_commit" \
    "$candidate_commit" >&2
  exit 1
}
[ "$archive_commit" = "$candidate_commit" ] || {
  printf 'archive commit %s does not match deployment commit %s\n' \
    "$archive_commit" \
    "$candidate_commit" >&2
  exit 1
}
[ "$validation_commit" = "$candidate_commit" ] || {
  printf 'validation commit %s does not match deployment commit %s\n' \
    "$validation_commit" \
    "$candidate_commit" >&2
  exit 1
}

if [ ! -d "$repo/.git" ]; then
  @NIX_5@/bin/airlock-config-vault-init >/dev/null
fi

[ -d "$repo/candidates/$candidate_commit" ] || {
  printf 'deployment references missing candidate archive: %s\n' "$candidate_commit" >&2
  exit 1
}

deployment_dir="$repo/deployments/$deployment_id"
[ ! -e "$deployment_dir" ] || {
  printf 'deployment already exists: %s\n' "$deployment_id" >&2
  exit 1
}

@NIX_1@/bin/mkdir -p "$deployment_dir/logs"
for file in \
  deployment.json \
  apply.json \
  archive.json \
  setup.json \
  topology.json \
  validated-topology.json \
  template-builds.json \
  nix-domain-builds.json \
  nix-domain-materializations.json \
  unsupported-domain-backends.json \
  domain-references.json \
  system.json \
  resolved-system.json \
  system-plan.json \
  system-apply.json \
  system-builds.json \
  systemd-actions.json \
  validation.json \
  manifest.json; do
  @NIX_1@/bin/cp "$tmpdir/$file" "$deployment_dir/$file"
done
for log in \
  apply.commands.log \
  archive.stdout.log \
  archive.stderr.log \
  setup.stdout.log \
  setup.stderr.log \
  template-builds.log \
  nix-domain-builds.log \
  system-builds.log \
  system-apply.log \
  profile-activation.log \
  host-system-activation.log \
  systemd-actions.log \
  clone.log \
  checkout.log; do
  @NIX_1@/bin/cp "$tmpdir/logs/$log" "$deployment_dir/logs/$log"
done

printf '%s\n' "$deployment_id" > "$repo/latest-deployment-attempt"
if [ "$deployment_status" = passed ]; then
  printf '%s\n' "$deployment_id" > "$repo/latest-deployment"
  printf '%s\n' "$candidate_commit" > "$repo/latest-current"
  @NIX_1@/bin/ln -sfn "candidates/$candidate_commit/snapshot" "$repo/current"
fi

cd "$repo"
@NIX_6@/bin/git add "deployments/$deployment_id" latest-deployment-attempt
if [ "$deployment_status" = passed ]; then
  @NIX_6@/bin/git add latest-deployment latest-current current
fi
@NIX_6@/bin/git \
  -c user.name='Airlock dom0 archive' \
  -c user.email='airlock-dom0@localhost' \
  commit -m "record deployment $deployment_id" >/dev/null

vault_commit="$(@NIX_6@/bin/git rev-parse HEAD)"
@NIX_4@/bin/jq -c -n \
  --arg schema "airlock.config-vault-deployment.v1" \
  --arg status "passed" \
  --arg deployment_status "$deployment_status" \
  --arg deployment_id "$deployment_id" \
  --arg candidate_commit "$candidate_commit" \
  --arg vault_commit "$vault_commit" \
  --arg archive_repo "$repo" \
  '{
    schema: $schema,
    status: $status,
    deployment_status: $deployment_status,
    deployment_id: $deployment_id,
    candidate_commit: $candidate_commit,
    vault_commit: $vault_commit,
    archive_repo: $archive_repo
  }'
