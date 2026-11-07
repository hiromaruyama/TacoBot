#!/usr/bin/env bash
# Installs a pre-push hook so EVERY agent/human (not just Claude Code) gets the overlap check.
set -euo pipefail
hooks_dir="$(git rev-parse --git-common-dir)/hooks"
mkdir -p "$hooks_dir"
cat > "$hooks_dir/pre-push" <<'EOF'
#!/usr/bin/env bash
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "main" ]; then
  echo "❌ Don't push to main directly. Open a PR from a <lane>/<task> branch." >&2
  exit 1
fi
python3 scripts/claims.py check --changed || {
  echo "❌ Your branch overlaps another branch (see above). Coordinate, or push with --no-verify ONLY if the owner agreed." >&2
  exit 1
}
EOF
chmod +x "$hooks_dir/pre-push"
echo "✅ pre-push hook installed in $hooks_dir"
