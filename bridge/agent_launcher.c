/*
 * Nearfield Bridge Agent : lanceur du service compagnon (macOS).
 *
 * Pourquoi un vrai programme compile plutot qu'un applet AppleScript
 * (2026-10-02, Apple TN3179 "Understanding local network privacy") :
 * - macOS reconnait l'application par sa signature ET par l'UUID de son
 *   executable principal ; tous les applets osacompile partagent le meme
 *   executable "applet", ce qui rend l'autorisation "Reseau local"
 *   imprevisible (refusee alors qu'elle est cochee) ;
 * - le module Python est lance comme ENFANT (pas exec) : macOS attribue alors
 *   ses connexions a cette application, qui porte l'autorisation.
 *
 * Lance par le LaunchAgent local.nearfield.bridge (AssociatedBundleIdentifiers),
 * redemarre par launchd s'il s'arrete. Journal : ~/.nearfield-bridge/service.log
 */
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(void) {
    const char *home = getenv("HOME");
    if (!home) return 1;
    char script[1024], logpath[1024];
    snprintf(script, sizeof script, "%s/.nearfield-bridge/nearfield_bridge.py", home);
    snprintf(logpath, sizeof logpath, "%s/.nearfield-bridge/service.log", home);

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, logpath, O_WRONLY | O_CREAT | O_APPEND, 0644);
    posix_spawn_file_actions_adddup2(&actions, STDOUT_FILENO, STDERR_FILENO);

    char *argv[] = {"/usr/bin/python3", "-u", script, "service", NULL};
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    if (posix_spawn(&child, argv[0], &actions, NULL, argv, environ) != 0) {
        perror("posix_spawn");
        return 1;
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        /* interrompu par un signal transmis : on attend toujours l'enfant */
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
