#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>

int main(int argc, char **argv) {
    const char *env_root = getenv("WECHAT_MAC_HOOK_ROOT");
    char cwd[PATH_MAX];
    const char *root = env_root;
    if (root == NULL || root[0] == '\0') {
        root = getcwd(cwd, sizeof(cwd));
    }
    if (root == NULL || root[0] == '\0') {
        fprintf(stderr, "cannot resolve project root; set WECHAT_MAC_HOOK_ROOT\n");
        return 126;
    }

    char script[PATH_MAX];
    snprintf(script, sizeof(script), "%s/desktop_app/wechat_second_manager.py", root);
    chdir(root);
    setenv("PYTHONUNBUFFERED", "1", 1);
    execl("/usr/bin/python3", "python3", script, (char *)NULL);
    perror("execl /usr/bin/python3");
    return 127;
}
