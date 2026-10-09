#!/bin/bash
# Release helper: scripts/release.sh X.Y.Z
#
# 1. Requires a clean working tree.
# 2. X.Y.Z must be semver and >= the latest reachable v* tag.
# 3. Updates VMVPN_VERSION (vmvpn) and VERSION (gui/vmvpn_common.py).
# 4. Runs the GUI unit tests.
# 5. Commits "release: vX.Y.Z" (skipped when there is nothing to commit)
#    and creates the annotated tag vX.Y.Z.
# 6. Never pushes — prints the push commands at the end.
set -euo pipefail

cd "$(dirname "$0")/.."

die() {
    echo "Error: $*" >&2
    exit 1
}

version="${1:-}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "usage: $0 X.Y.Z (semver, e.g. 1.1.0)"

[[ -z "$(git status --porcelain)" ]] \
    || die "the working tree is not clean — commit or stash first"

git rev-parse --verify --quiet "refs/tags/v$version" >/dev/null \
    && die "tag v$version already exists"

# version >= latest reachable v* tag (sorted with version sort).
latest_tag=$(git tag --merged HEAD --list 'v[0-9]*' \
    --sort=-version:refname | head -1)
if [[ -n "$latest_tag" ]]; then
    latest="${latest_tag#v}"
    lowest=$(printf '%s\n%s\n' "$latest" "$version" | sort -V | head -1)
    if [[ "$lowest" == "$version" && "$version" != "$latest" ]]; then
        die "version $version is older than the latest tag $latest_tag"
    fi
fi

sed -i "s/^VMVPN_VERSION=\".*\"/VMVPN_VERSION=\"$version\"/" vmvpn
sed -i "s/^VERSION = \".*\"/VERSION = \"$version\"/" gui/vmvpn_common.py
echo "Version set to $version."

(cd gui && python3 -m unittest discover -s tests)

if [[ -n "$(git status --porcelain -- vmvpn gui/vmvpn_common.py)" ]]; then
    git add vmvpn gui/vmvpn_common.py
    git commit -m "release: v$version"
else
    echo "Nothing to commit (already at $version)."
fi

git tag -a "v$version" -m "Release v$version"
echo "Tagged v$version."

echo ""
echo "Not pushed. To publish:"
echo "  git push origin main --tags"
echo "  git push gitlab main --tags"
