#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <dlfcn.h>
#import <sys/stat.h>
#import <dirent.h>
#import <fcntl.h>
#import <stdarg.h>
#import <unistd.h>
#import <pwd.h>
#import <errno.h>
#import <crt_externs.h>
#import <sys/syscall.h>
#import <mach-o/dyld.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define W2_LOG_PREFIX "[WeChatSecondHook] "

#define DYLD_INTERPOSE(_replacement, _replacee) \
__attribute__((used)) static struct { const void *replacement; const void *replacee; } \
_interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = { \
    (const void *)(unsigned long)&_replacement, \
    (const void *)(unsigned long)&_replacee \
};

static int g_enabled = -1;
static char g_realHome[PATH_MAX] = {0};
static char g_secondRoot[PATH_MAX] = {0};
static char g_secondHome[PATH_MAX] = {0};
static char g_secondContainerData[PATH_MAX] = {0};
static char g_secondGroupContainer[PATH_MAX] = {0};
static char g_secondAppSupportWechat[PATH_MAX] = {0};
static char g_secondCacheWechat[PATH_MAX] = {0};
static char g_tmpPath[PATH_MAX] = {0};
static __thread char g_redirectBuf[PATH_MAX * 2];
static __thread char g_getenvHomeBuf[PATH_MAX];

typedef char *(*getenv_fn_t)(const char *);
typedef int (*mkdir_fn_t)(const char *, mode_t);
typedef int (*stat_fn_t)(const char *, struct stat *);

typedef int (*open_fn_t)(const char *, int, ...);
typedef int (*openat_fn_t)(int, const char *, int, ...);
typedef FILE *(*fopen_fn_t)(const char *, const char *);
typedef FILE *(*freopen_fn_t)(const char *, const char *, FILE *);
typedef int (*access_fn_t)(const char *, int);
typedef int (*lstat_fn_t)(const char *, struct stat *);
typedef DIR *(*opendir_fn_t)(const char *);
typedef int (*unlink_fn_t)(const char *);
typedef int (*rmdir_fn_t)(const char *);
typedef int (*remove_fn_t)(const char *);
typedef int (*chmod_fn_t)(const char *, mode_t);
typedef int (*chown_fn_t)(const char *, uid_t, gid_t);
typedef int (*rename_fn_t)(const char *, const char *);

#define REAL_FUNC(type, name) \
static type real_##name##_func(void) { \
    static type fn = NULL; \
    if (!fn) fn = (type)dlsym(RTLD_NEXT, #name); \
    return fn; \
}

REAL_FUNC(open_fn_t, open)
REAL_FUNC(openat_fn_t, openat)
REAL_FUNC(fopen_fn_t, fopen)
REAL_FUNC(freopen_fn_t, freopen)
REAL_FUNC(access_fn_t, access)
REAL_FUNC(lstat_fn_t, lstat)
REAL_FUNC(opendir_fn_t, opendir)
REAL_FUNC(unlink_fn_t, unlink)
REAL_FUNC(rmdir_fn_t, rmdir)
REAL_FUNC(remove_fn_t, remove)
REAL_FUNC(chmod_fn_t, chmod)
REAL_FUNC(chown_fn_t, chown)
REAL_FUNC(rename_fn_t, rename)


static char *raw_getenv_direct(const char *name) {
    if (!name || !name[0]) return NULL;
    char **env = *_NSGetEnviron();
    if (!env) return NULL;
    size_t n = strlen(name);
    for (char **p = env; *p; p++) {
        if (strncmp(*p, name, n) == 0 && (*p)[n] == '=') return *p + n + 1;
    }
    return NULL;
}

static int raw_mkdir_sys(const char *path, mode_t mode) {
    return (int)syscall(SYS_mkdir, path, mode);
}

static stat_fn_t real_stat_func(void) {
    static stat_fn_t fn = NULL;
    if (!fn) fn = (stat_fn_t)dlsym(RTLD_NEXT, "stat");
    return fn;
}

static void safe_strlcpy(char *dst, const char *src, size_t size) {
    if (!dst || size == 0) return;
    if (!src) { dst[0] = '\0'; return; }
    size_t n = strlen(src);
    if (n >= size) n = size - 1;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static void join_path(char *dst, size_t dstSize, const char *a, const char *b) {
    if (!a) a = "";
    if (!b) b = "";
    size_t alen = strlen(a);
    if (alen > 0 && a[alen - 1] == '/') snprintf(dst, dstSize, "%s%s", a, b);
    else snprintf(dst, dstSize, "%s/%s", a, b);
}

static const char *real_home(void) {
    if (g_realHome[0]) return g_realHome;

    const char *home = raw_getenv_direct("WECHAT_REAL_HOME");
    if (!home || !home[0]) home = raw_getenv_direct("HOME");
    if (!home || !home[0]) {
        struct passwd *pw = getpwuid(getuid());
        if (pw && pw->pw_dir) home = pw->pw_dir;
    }
    if (!home || !home[0]) home = "/tmp";
    safe_strlcpy(g_realHome, home, sizeof(g_realHome));
    return g_realHome;
}

static bool executable_looks_like_second_app(void) {
    char exe[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) return false;
    return strstr(exe, "/WeChatSecond.app/") != NULL || strstr(exe, "/WeChatSecond.app") != NULL;
}

static bool argv_has_second_marker(void) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    if (!argv) return false;
    for (int i = 0; i < argc; i++) {
        if (argv[i] && strcmp(argv[i], "--wechat-second-instance") == 0) return true;
    }
    return false;
}

static bool is_enabled(void) {
    if (g_enabled != -1) return g_enabled == 1;
    const char *flag = raw_getenv_direct("WECHAT_SECOND_INSTANCE");
    g_enabled = ((flag && strcmp(flag, "1") == 0) || executable_looks_like_second_app() || argv_has_second_marker()) ? 1 : 0;
    return g_enabled == 1;
}

static void ensure_dir_recursive_raw(const char *path) {
    if (!path || !path[0]) return;
    char tmp[PATH_MAX];
    safe_strlcpy(tmp, path, sizeof(tmp));
    size_t len = strlen(tmp);
    if (len == 0) return;
    if (tmp[len - 1] == '/') tmp[len - 1] = '\0';

    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            raw_mkdir_sys(tmp, 0700);
            *p = '/';
        }
    }
    raw_mkdir_sys(tmp, 0700);
}

static void ensure_parent_dir_raw(const char *path) {
    if (!path || !path[0]) return;
    char tmp[PATH_MAX];
    safe_strlcpy(tmp, path, sizeof(tmp));
    char *slash = strrchr(tmp, '/');
    if (!slash || slash == tmp) return;
    *slash = '\0';
    ensure_dir_recursive_raw(tmp);
}

static void init_paths(void) {
    if (g_secondRoot[0]) return;

    const char *home = real_home();
    const char *root = raw_getenv_direct("WECHAT_SECOND_HOME");
    if (!root || !root[0]) {
        snprintf(g_secondRoot, sizeof(g_secondRoot), "%s/Library/Application Support/WeChatSecond", home);
    } else if (root[0] == '~' && root[1] == '/') {
        snprintf(g_secondRoot, sizeof(g_secondRoot), "%s/%s", home, root + 2);
    } else {
        safe_strlcpy(g_secondRoot, root, sizeof(g_secondRoot));
    }

    snprintf(g_secondHome, sizeof(g_secondHome), "%s/Sandbox/Home", g_secondRoot);
    snprintf(g_secondContainerData, sizeof(g_secondContainerData), "%s/Sandbox/Containers/com.tencent.xinWeChat/Data", g_secondRoot);
    snprintf(g_secondGroupContainer, sizeof(g_secondGroupContainer), "%s/Sandbox/Group Containers/5A4RE8SF68.com.tencent.xinWeChat", g_secondRoot);
    snprintf(g_secondAppSupportWechat, sizeof(g_secondAppSupportWechat), "%s/Library/Application Support/WeChat", g_secondHome);
    snprintf(g_secondCacheWechat, sizeof(g_secondCacheWechat), "%s/Library/Caches/com.tencent.xinWeChat", g_secondHome);
    snprintf(g_tmpPath, sizeof(g_tmpPath), "%s/tmp", g_secondRoot);

    ensure_dir_recursive_raw(g_secondRoot);
    ensure_dir_recursive_raw(g_secondHome);
    ensure_dir_recursive_raw(g_secondContainerData);
    ensure_dir_recursive_raw(g_secondGroupContainer);
    ensure_dir_recursive_raw(g_secondAppSupportWechat);
    ensure_dir_recursive_raw(g_secondCacheWechat);
    ensure_dir_recursive_raw(g_tmpPath);

    setenv("WECHAT_REAL_HOME", home, 0);
    setenv("HOME", g_secondHome, 1);
    setenv("TMPDIR", g_tmpPath, 1);

    fprintf(stderr, W2_LOG_PREFIX "enabled; realHome=%s secondRoot=%s\n", home, g_secondRoot);
}

static bool starts_with_boundary(const char *path, const char *prefix) {
    if (!path || !prefix || !prefix[0]) return false;
    size_t n = strlen(prefix);
    if (strncmp(path, prefix, n) != 0) return false;
    return path[n] == '\0' || path[n] == '/';
}

static const char *replace_prefix(const char *path, const char *from, const char *to) {
    size_t n = strlen(from);
    const char *suffix = path + n;
    if (*suffix == '/') suffix++;
    if (suffix[0]) snprintf(g_redirectBuf, sizeof(g_redirectBuf), "%s/%s", to, suffix);
    else snprintf(g_redirectBuf, sizeof(g_redirectBuf), "%s", to);
    return g_redirectBuf;
}

static const char *redirect_home_child_with_prefix(const char *p, const char *homeSubdir, const char *namePrefix) {
    char base[PATH_MAX];
    snprintf(base, sizeof(base), "%s/%s", real_home(), homeSubdir);
    if (!starts_with_boundary(p, base)) return NULL;
    const char *rel = p + strlen(base);
    if (*rel == '/') rel++;
    if (strncmp(rel, namePrefix, strlen(namePrefix)) != 0) return NULL;

    char toBase[PATH_MAX];
    snprintf(toBase, sizeof(toBase), "%s/%s", g_secondHome, homeSubdir);
    if (rel[0]) snprintf(g_redirectBuf, sizeof(g_redirectBuf), "%s/%s", toBase, rel);
    else snprintf(g_redirectBuf, sizeof(g_redirectBuf), "%s", toBase);
    return g_redirectBuf;
}

static const char *redirect_path(const char *path) {
    if (!is_enabled() || !path || !path[0]) return path;
    init_paths();

    const char *home = real_home();
    const char *p = path;
    char expanded[PATH_MAX * 2];
    if (path[0] == '~' && path[1] == '/') {
        snprintf(expanded, sizeof(expanded), "%s/%s", home, path + 2);
        p = expanded;
    }

    char from[PATH_MAX];

    snprintf(from, sizeof(from), "%s/Library/Containers/com.tencent.xinWeChat/Data", home);
    if (starts_with_boundary(p, from)) return replace_prefix(p, from, g_secondContainerData);

    snprintf(from, sizeof(from), "%s/Library/Containers/com.tencent.xinWeChat", home);
    if (starts_with_boundary(p, from)) {
        char to[PATH_MAX];
        snprintf(to, sizeof(to), "%s/Sandbox/Containers/com.tencent.xinWeChat", g_secondRoot);
        return replace_prefix(p, from, to);
    }

    snprintf(from, sizeof(from), "%s/Library/Group Containers/5A4RE8SF68.com.tencent.xinWeChat", home);
    if (starts_with_boundary(p, from)) return replace_prefix(p, from, g_secondGroupContainer);

    snprintf(from, sizeof(from), "%s/Library/Application Support/com.tencent.xinWeChat", home);
    if (starts_with_boundary(p, from)) {
        char to[PATH_MAX];
        snprintf(to, sizeof(to), "%s/Library/Application Support/com.tencent.xinWeChat", g_secondHome);
        return replace_prefix(p, from, to);
    }

    snprintf(from, sizeof(from), "%s/Library/Application Support/WeChat", home);
    if (starts_with_boundary(p, from)) return replace_prefix(p, from, g_secondAppSupportWechat);

    const char *generic = redirect_home_child_with_prefix(p, "Library/Caches", "com.tencent.xinWeChat");
    if (generic) return generic;

    generic = redirect_home_child_with_prefix(p, "Library/Preferences", "com.tencent.xinWeChat");
    if (generic) return generic;

    generic = redirect_home_child_with_prefix(p, "Library/HTTPStorages", "com.tencent.xinWeChat");
    if (generic) return generic;

    generic = redirect_home_child_with_prefix(p, "Library/Saved Application State", "com.tencent.xinWeChat");
    if (generic) return generic;

    snprintf(from, sizeof(from), "%s/Library/Preferences/com.tencent.xinWeChat.plist", home);
    if (strcmp(p, from) == 0) {
        snprintf(g_redirectBuf, sizeof(g_redirectBuf), "%s/Library/Preferences/com.tencent.xinWeChat.plist", g_secondHome);
        return g_redirectBuf;
    }

    snprintf(from, sizeof(from), "%s/Library/HTTPStorages/com.tencent.xinWeChat", home);
    if (starts_with_boundary(p, from)) {
        char to[PATH_MAX];
        snprintf(to, sizeof(to), "%s/Library/HTTPStorages/com.tencent.xinWeChat", g_secondHome);
        return replace_prefix(p, from, to);
    }

    snprintf(from, sizeof(from), "%s/Library/Saved Application State/com.tencent.xinWeChat.savedState", home);
    if (starts_with_boundary(p, from)) {
        char to[PATH_MAX];
        snprintf(to, sizeof(to), "%s/Library/Saved Application State/com.tencent.xinWeChat.savedState", g_secondHome);
        return replace_prefix(p, from, to);
    }

    // When HOME has already been changed by us, keep all writes inside the second HOME.
    if (starts_with_boundary(p, g_secondHome) || starts_with_boundary(p, g_secondRoot)) return p;

    return path;
}

static NSString *ns_second_home(void) {
    init_paths();
    return [NSString stringWithUTF8String:g_secondHome];
}

static NSString *path_for_search_directory(NSSearchPathDirectory directory) {
    init_paths();
    NSString *home = ns_second_home();
    switch (directory) {
        case NSApplicationDirectory: return [home stringByAppendingPathComponent:@"Applications"];
        case NSDemoApplicationDirectory: return [home stringByAppendingPathComponent:@"Applications/Demos"];
        case NSDeveloperApplicationDirectory: return [home stringByAppendingPathComponent:@"Developer/Applications"];
        case NSAdminApplicationDirectory: return [home stringByAppendingPathComponent:@"Applications/Utilities"];
        case NSLibraryDirectory: return [home stringByAppendingPathComponent:@"Library"];
        case NSDeveloperDirectory: return [home stringByAppendingPathComponent:@"Developer"];
        case NSUserDirectory: return [home stringByAppendingPathComponent:@"Users"];
        case NSDocumentationDirectory: return [home stringByAppendingPathComponent:@"Library/Documentation"];
        case NSDocumentDirectory: return [home stringByAppendingPathComponent:@"Documents"];
        case NSCoreServiceDirectory: return [home stringByAppendingPathComponent:@"System/Library/CoreServices"];
        case NSAutosavedInformationDirectory: return [home stringByAppendingPathComponent:@"Library/Autosave Information"];
        case NSDesktopDirectory: return [home stringByAppendingPathComponent:@"Desktop"];
        case NSCachesDirectory: return [home stringByAppendingPathComponent:@"Library/Caches"];
        case NSApplicationSupportDirectory: return [home stringByAppendingPathComponent:@"Library/Application Support"];
        case NSDownloadsDirectory: return [home stringByAppendingPathComponent:@"Downloads"];
        case NSMoviesDirectory: return [home stringByAppendingPathComponent:@"Movies"];
        case NSMusicDirectory: return [home stringByAppendingPathComponent:@"Music"];
        case NSPicturesDirectory: return [home stringByAppendingPathComponent:@"Pictures"];
        case NSSharedPublicDirectory: return [home stringByAppendingPathComponent:@"Public"];
        case NSPreferencePanesDirectory: return [home stringByAppendingPathComponent:@"Library/PreferencePanes"];
        case NSApplicationScriptsDirectory: return [home stringByAppendingPathComponent:@"Library/Application Scripts"];
        case NSItemReplacementDirectory: return [home stringByAppendingPathComponent:@".TemporaryItems"];
        default: return nil;
    }
}

NSString *wechat2_NSHomeDirectory(void) {
    if (!is_enabled()) {
        typedef NSString *(*fn_t)(void);
        fn_t orig = (fn_t)dlsym(RTLD_NEXT, "NSHomeDirectory");
        return orig ? orig() : @"";
    }
    return ns_second_home();
}
DYLD_INTERPOSE(wechat2_NSHomeDirectory, NSHomeDirectory)

NSArray<NSString *> *wechat2_NSSearchPathForDirectoriesInDomains(NSSearchPathDirectory directory, NSSearchPathDomainMask domainMask, BOOL expandTilde) {
    if (!is_enabled() || !(domainMask & NSUserDomainMask)) {
        typedef NSArray<NSString *> *(*fn_t)(NSSearchPathDirectory, NSSearchPathDomainMask, BOOL);
        fn_t orig = (fn_t)dlsym(RTLD_NEXT, "NSSearchPathForDirectoriesInDomains");
        return orig ? orig(directory, domainMask, expandTilde) : @[];
    }
    NSString *path = path_for_search_directory(directory);
    if (!path) {
        typedef NSArray<NSString *> *(*fn_t)(NSSearchPathDirectory, NSSearchPathDomainMask, BOOL);
        fn_t orig = (fn_t)dlsym(RTLD_NEXT, "NSSearchPathForDirectoriesInDomains");
        return orig ? orig(directory, domainMask, expandTilde) : @[];
    }
    ensure_dir_recursive_raw(path.UTF8String);
    if (!expandTilde) {
        NSString *home = ns_second_home();
        if ([path hasPrefix:home]) path = [@"~" stringByAppendingString:[path substringFromIndex:home.length]];
    }
    return @[path];
}
DYLD_INTERPOSE(wechat2_NSSearchPathForDirectoriesInDomains, NSSearchPathForDirectoriesInDomains)

char *wechat2_getenv(const char *name) {
    if (is_enabled() && name && strcmp(name, "HOME") == 0) {
        init_paths();
        safe_strlcpy(g_getenvHomeBuf, g_secondHome, sizeof(g_getenvHomeBuf));
        return g_getenvHomeBuf;
    }
    if (is_enabled() && name && strcmp(name, "TMPDIR") == 0) {
        init_paths();
        safe_strlcpy(g_getenvHomeBuf, g_tmpPath, sizeof(g_getenvHomeBuf));
        return g_getenvHomeBuf;
    }
    return raw_getenv_direct(name);
}
DYLD_INTERPOSE(wechat2_getenv, getenv)

int wechat2_open(const char *path, int oflag, ...) {
    mode_t mode = 0;
    if (oflag & O_CREAT) {
        va_list ap; va_start(ap, oflag); mode = (mode_t)va_arg(ap, int); va_end(ap);
    }
    const char *rp = redirect_path(path);
    if ((oflag & O_CREAT) && rp != path) ensure_parent_dir_raw(rp);
    if (oflag & O_CREAT) return (int)syscall(SYS_open, rp, oflag, mode);
    return (int)syscall(SYS_open, rp, oflag);
}
DYLD_INTERPOSE(wechat2_open, open)

int wechat2_openat(int fd, const char *path, int oflag, ...) {
    mode_t mode = 0;
    if (oflag & O_CREAT) {
        va_list ap; va_start(ap, oflag); mode = (mode_t)va_arg(ap, int); va_end(ap);
    }
    const char *rp = redirect_path(path);
    if ((oflag & O_CREAT) && rp != path) ensure_parent_dir_raw(rp);
    if (oflag & O_CREAT) return (int)syscall(SYS_openat, fd, rp, oflag, mode);
    return (int)syscall(SYS_openat, fd, rp, oflag);
}
DYLD_INTERPOSE(wechat2_openat, openat)

FILE *wechat2_fopen(const char *path, const char *mode) {
    const char *rp = redirect_path(path);
    if (mode && strpbrk(mode, "wa+")) ensure_parent_dir_raw(rp);
    fopen_fn_t orig = real_fopen_func();
    return orig ? orig(rp, mode) : NULL;
}
DYLD_INTERPOSE(wechat2_fopen, fopen)

FILE *wechat2_freopen(const char *path, const char *mode, FILE *stream) {
    const char *rp = redirect_path(path);
    if (mode && strpbrk(mode, "wa+")) ensure_parent_dir_raw(rp);
    freopen_fn_t orig = real_freopen_func();
    return orig ? orig(rp, mode, stream) : NULL;
}
DYLD_INTERPOSE(wechat2_freopen, freopen)

int wechat2_access(const char *path, int mode) { return (int)syscall(SYS_access, redirect_path(path), mode); }
DYLD_INTERPOSE(wechat2_access, access)

int wechat2_stat(const char *path, struct stat *buf) { return (int)syscall(SYS_stat64, redirect_path(path), buf); }
DYLD_INTERPOSE(wechat2_stat, stat)

int wechat2_lstat(const char *path, struct stat *buf) { return (int)syscall(SYS_lstat64, redirect_path(path), buf); }
DYLD_INTERPOSE(wechat2_lstat, lstat)

int wechat2_mkdir(const char *path, mode_t mode) {
    const char *rp = redirect_path(path);
    ensure_parent_dir_raw(rp);
    return raw_mkdir_sys(rp, mode);
}
DYLD_INTERPOSE(wechat2_mkdir, mkdir)

DIR *wechat2_opendir(const char *path) { opendir_fn_t orig = real_opendir_func(); return orig ? orig(redirect_path(path)) : NULL; }
DYLD_INTERPOSE(wechat2_opendir, opendir)

int wechat2_unlink(const char *path) { return (int)syscall(SYS_unlink, redirect_path(path)); }
DYLD_INTERPOSE(wechat2_unlink, unlink)

int wechat2_rmdir(const char *path) { return (int)syscall(SYS_rmdir, redirect_path(path)); }
DYLD_INTERPOSE(wechat2_rmdir, rmdir)

int wechat2_remove(const char *path) {
    const char *rp = redirect_path(path);
    int r = (int)syscall(SYS_unlink, rp);
    if (r != 0 && (errno == EISDIR || errno == EPERM)) r = (int)syscall(SYS_rmdir, rp);
    return r;
}
DYLD_INTERPOSE(wechat2_remove, remove)

int wechat2_chmod(const char *path, mode_t mode) { return (int)syscall(SYS_chmod, redirect_path(path), mode); }
DYLD_INTERPOSE(wechat2_chmod, chmod)

int wechat2_chown(const char *path, uid_t owner, gid_t group) { return (int)syscall(SYS_chown, redirect_path(path), owner, group); }
DYLD_INTERPOSE(wechat2_chown, chown)

int wechat2_rename(const char *oldp, const char *newp) {
    const char *ro = redirect_path(oldp);
    char oldCopy[PATH_MAX * 2];
    safe_strlcpy(oldCopy, ro, sizeof(oldCopy));
    const char *rn = redirect_path(newp);
    ensure_parent_dir_raw(rn);
    return (int)syscall(SYS_rename, oldCopy, rn);
}
DYLD_INTERPOSE(wechat2_rename, rename)

static NSURL *url_for_search_directory(NSSearchPathDirectory directory, BOOL create) {
    NSString *path = path_for_search_directory(directory);
    if (!path) return nil;
    if (create) ensure_dir_recursive_raw(path.UTF8String);
    return [NSURL fileURLWithPath:path isDirectory:YES];
}

@interface NSFileManager (WeChatSecondHook)
@end

@implementation NSFileManager (WeChatSecondHook)

+ (void)load {
    if (!is_enabled()) return;
    init_paths();

    Class cls = [NSFileManager class];
    Method m1 = class_getInstanceMethod(cls, @selector(URLsForDirectory:inDomains:));
    Method m2 = class_getInstanceMethod(cls, @selector(w2_URLsForDirectory:inDomains:));
    if (m1 && m2) method_exchangeImplementations(m1, m2);

    Method m3 = class_getInstanceMethod(cls, @selector(URLForDirectory:inDomain:appropriateForURL:create:error:));
    Method m4 = class_getInstanceMethod(cls, @selector(w2_URLForDirectory:inDomain:appropriateForURL:create:error:));
    if (m3 && m4) method_exchangeImplementations(m3, m4);

    fprintf(stderr, W2_LOG_PREFIX "NSFileManager swizzled\n");
}

- (NSArray<NSURL *> *)w2_URLsForDirectory:(NSSearchPathDirectory)directory inDomains:(NSSearchPathDomainMask)domainMask {
    if (is_enabled() && (domainMask & NSUserDomainMask)) {
        NSURL *url = url_for_search_directory(directory, YES);
        if (url) return @[url];
    }
    return [self w2_URLsForDirectory:directory inDomains:domainMask];
}

- (NSURL *)w2_URLForDirectory:(NSSearchPathDirectory)directory inDomain:(NSSearchPathDomainMask)domain appropriateForURL:(NSURL *)url create:(BOOL)shouldCreate error:(NSError **)error {
    if (is_enabled() && (domain & NSUserDomainMask)) {
        NSURL *u = url_for_search_directory(directory, shouldCreate);
        if (u) return u;
    }
    return [self w2_URLForDirectory:directory inDomain:domain appropriateForURL:url create:shouldCreate error:error];
}

@end

__attribute__((constructor))
static void wechat_second_constructor(void) {
    if (is_enabled()) init_paths();
}
