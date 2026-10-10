#!/bin/sh
# Install kdocs-cli (official Kingsoft Docs Skill CLI) for non-Docker deployments.
# Usage: bash scripts/install-kdocs-cli.sh [version]
set -eu

VERSION="${1:-${KDOCS_CLI_VERSION:-2.7.1}}"
CDN_BASE="${KDOCS_CLI_CDN:-https://wpsai.wpscdn.cn/skillhub/pro}"
BIN_NAME="kdocs-cli"
INSTALL_DIR="${KDOCS_CLI_DIR:-$HOME/.local/bin}"

os=$(uname -s)
case "$os" in
  Linux*) os="linux" ;;
  Darwin*) os="darwin" ;;
  *) echo "Unsupported OS: $os" >&2; exit 1 ;;
esac
arch=$(uname -m)
case "$arch" in
  x86_64|amd64) arch="amd64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) echo "Unsupported arch: $arch" >&2; exit 1 ;;
esac

archive="${BIN_NAME}-${VERSION}-${os}-${arch}.tar.gz"
url="${CDN_BASE}/v${VERSION}/releases/${archive}"

mkdir -p "$INSTALL_DIR"
tmp=$(mktemp -d)
echo "Downloading ${url}"
curl -fsSL "$url" -o "${tmp}/${archive}"

sums_url="${CDN_BASE}/v${VERSION}/releases/checksums.txt"
if curl -fsSL "$sums_url" -o "${tmp}/checksums.txt" 2>/dev/null; then
  (cd "$tmp" && grep " ${archive}\$" checksums.txt >/dev/null 2>&1 && (sha256sum -c checksums.txt --ignore-missing 2>/dev/null || shasum -c checksums.txt --ignore-missing 2>/dev/null) || true)
fi

tar -xzf "${tmp}/${archive}" -C "$tmp"
bin_path=$(find "$tmp" -type f -name "$BIN_NAME" | head -1)
[ -n "$bin_path" ] || { echo "archive did not contain $BIN_NAME" >&2; exit 1; }
mv "$bin_path" "${INSTALL_DIR}/${BIN_NAME}"
chmod +x "${INSTALL_DIR}/${BIN_NAME}"
rm -rf "$tmp"

echo "Installed: ${INSTALL_DIR}/${BIN_NAME} ($($INSTALL_DIR/$BIN_NAME version 2>/dev/null || echo v$VERSION))"
echo "Ensure ${INSTALL_DIR} is on PATH, then save your token:"
echo "  ${INSTALL_DIR}/${BIN_NAME} auth set-token <TOKEN>   # or set KDOCS_CLI_TOKEN env"
