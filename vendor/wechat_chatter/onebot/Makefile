.PHONY: build run clean pprof

# 设置 CC 为系统自带的 gcc (macOS)
export CC = /usr/bin/gcc

build:
	@echo "编译 onebot..."
	@go env -w CC=/usr/bin/gcc 2>/dev/null || true
	@go build -o onebot .

run: build
	@echo "启动 onebot..."
	@./onebot

clean:
	@echo "清理构建文件..."
	@rm -f onebot/onebot
