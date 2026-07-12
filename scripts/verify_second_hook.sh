#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$ROOT_DIR/build/WeChatSecondHook.dylib"
PROBE_SRC="$ROOT_DIR/build/hook_probe.m"
PROBE_BIN="$ROOT_DIR/build/hook_probe"
TEST_ROOT="$ROOT_DIR/build/test-second-home"
REAL_HOME="$HOME"

if [[ ! -f "$HOOK" ]]; then
  "$ROOT_DIR/scripts/build.sh"
fi
rm -rf "$TEST_ROOT"
mkdir -p "$TEST_ROOT"
cat > "$PROBE_SRC" <<'PROBE'
#import <Foundation/Foundation.h>
#import <fcntl.h>
#import <unistd.h>
int main(void) {
    @autoreleasepool {
        NSString *home = NSHomeDirectory();
        NSArray *appSup = NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, YES);
        printf("NSHomeDirectory=%s\n", home.UTF8String);
        printf("AppSupport=%s\n", ((NSString *)appSup.firstObject).UTF8String);
        const char *target = [[[NSProcessInfo processInfo].environment[@"WECHAT_REAL_HOME"] stringByAppendingString:@"/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/probe.txt"] UTF8String];
        int fd = open(target, O_CREAT|O_WRONLY|O_TRUNC, 0600);
        if (fd < 0) { perror("open"); return 2; }
        write(fd, "ok", 2);
        close(fd);
        printf("ProbeWriteRequested=%s\n", target);
    }
    return 0;
}
PROBE
clang -fobjc-arc -framework Foundation -o "$PROBE_BIN" "$PROBE_SRC"
WECHAT_REAL_HOME="$REAL_HOME" \
WECHAT_SECOND_INSTANCE=1 \
WECHAT_SECOND_HOME="$TEST_ROOT" \
DYLD_INSERT_LIBRARIES="$HOOK" \
"$PROBE_BIN" | tee "$ROOT_DIR/build/verify.log"

EXPECTED="$TEST_ROOT/Sandbox/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/probe.txt"
MAIN_TARGET="$REAL_HOME/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/probe.txt"
if [[ ! -f "$EXPECTED" ]]; then
  echo "验证失败：没有写入隔离目录 $EXPECTED" >&2
  exit 1
fi
if [[ -f "$MAIN_TARGET" ]]; then
  echo "验证失败：主微信目录出现测试文件 $MAIN_TARGET" >&2
  exit 1
fi
echo "验证通过：hook 仅写入隔离目录，不写主微信目录。"
echo "隔离测试目录：$TEST_ROOT"
