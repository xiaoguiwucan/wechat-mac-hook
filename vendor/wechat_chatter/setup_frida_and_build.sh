#!/bin/bash
set -e

# Frida devkit must match the frida-go API used by go.mod.
FRIDA_VERSION="${FRIDA_VERSION:-17.8.0}"
OS="macos"
ARCH="arm64"
DEVKIT_NAME="frida-core-devkit-${FRIDA_VERSION}-${OS}-${ARCH}"
DOWNLOAD_URL="https://github.com/frida/frida/releases/download/${FRIDA_VERSION}/${DEVKIT_NAME}.tar.xz"
DEVKIT_DIR="$(pwd)/frida-devkit"

echo "🔧 配置 Frida 开发环境 (Version: $FRIDA_VERSION, Arch: $ARCH)..."

# 检查是否已存在且版本/API匹配
if [ -f "$DEVKIT_DIR/libfrida-core.a" ] && [ -f "$DEVKIT_DIR/frida-core.h" ] && grep -q "FridaPackageManager" "$DEVKIT_DIR/frida-core.h"; then
    echo "✅ 发现已存在且兼容的 Frida Devkit，跳过下载。"
else
    # 如果版本不匹配，清理旧的
    if [ -d "$DEVKIT_DIR" ]; then
        echo "⚠️ 清理旧的或不兼容的 Devkit 目录..."
        rm -rf "$DEVKIT_DIR"
    fi

    mkdir -p "$DEVKIT_DIR"
    echo "⬇️ 正在下载 ${DEVKIT_NAME}..."
    curl -L -o "$DEVKIT_DIR/devkit.tar.xz" "$DOWNLOAD_URL"

    echo "📦 正在解压..."
    tar -xf "$DEVKIT_DIR/devkit.tar.xz" -C "$DEVKIT_DIR"
    rm "$DEVKIT_DIR/devkit.tar.xz"
    echo "✅ 解压完成"
fi


# 2. 编译
echo "🚀 开始编译 OneBot..."
cd onebot

# 强制设置 GOARCH 为 arm64，因为我们下载的是 arm64 的库
export GOARCH=arm64
export CGO_ENABLED=1
export CGO_CFLAGS="-I$DEVKIT_DIR"
export CGO_LDFLAGS="-L$DEVKIT_DIR -lfrida-core"

echo "⚙️ GOARCH=$GOARCH"
echo "⚙️ CGO_CFLAGS=$CGO_CFLAGS"
echo "⚙️ CGO_LDFLAGS=$CGO_LDFLAGS"

go build -v -o onebot .

echo "🎉 编译成功！二进制文件位置: $(pwd)/onebot"
