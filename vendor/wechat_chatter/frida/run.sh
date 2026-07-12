#!/bin/bash

SCRIPT="$1"

if [ -z "$SCRIPT" ]; then
    echo "用法: $0 <frida脚本路径>"
    echo "示例: $0 ./frida/reply.js"
    exit 1
fi

if [ ! -f "$SCRIPT" ]; then
    echo "错误: 脚本文件 $SCRIPT 不存在"
    exit 1
fi

echo "正在查找微信进程..."
PID=$(pgrep -x WeChat)
if [ -z "$PID" ]; then
    echo "未发现微信进程，正在启动微信..."
    open -a WeChat
    while true; do
        PID=$(pgrep -x WeChat)
        if [ -n "$PID" ]; then
            break
        fi
        echo "等待微信启动，5秒后重试..."
        sleep 5
    done
fi

echo "找到微信进程 PID: $PID"
echo "执行: frida -p $PID -l $SCRIPT"
frida -p "$PID" -l "$SCRIPT"
