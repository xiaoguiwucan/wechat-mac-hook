#!/bin/bash

# 设置错误即停止
set -e

# --- 配置区 ---
BINARY_NAME="onebot"
JS_FILE="./onebot/script.js"
OUTPUT_TAR="./release/onebot_mac_arm64.tar.gz"
GO_SOURCE_PATH="." # 假设你的 main.go 在当前目录

echo "🚀 开始构建流程..."

# 1. 编译 Go 二进制文件
echo "📦 正在编译 Go 二进制文件 [$BINARY_NAME]..."
# 如果你需要交叉编译（例如在 Mac 上编译 Linux 版本），可以取消下面两行的注释
# export GOOS=linux
# export GOARCH=amd64
cd ./onebot
go build -o $BINARY_NAME $GO_SOURCE_PATH

# 2. 创建打包目录（为了保证 tar 包内路径整洁，建议先考入临时目录）
echo "📂 正在打包文件..."
cd ..
tar -czvf $OUTPUT_TAR ./onebot/onebot $JS_FILE ./wechat_version

echo "✅ 构建完成！生成文件: $OUTPUT_TAR"
echo "📄 包内包含: $(tar -tf $OUTPUT_TAR)"