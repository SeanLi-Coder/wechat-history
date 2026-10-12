// Keep the GUI and launchd entry point native for macOS privacy attribution.
// https://developer.apple.com/forums/thread/720057?answerId=736527022

#import <Foundation/Foundation.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

extern char **environ;

static void fail(const char *message) {
    fprintf(stderr, "Shiliao launcher: %s\n", message);
    exit(EXIT_FAILURE);
}

static void prepareGUILog(void) {
    NSString *logs = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/Shiliao/logs"];
    NSError *error = nil;
    if (![[NSFileManager defaultManager] createDirectoryAtPath:logs
                                  withIntermediateDirectories:YES
                                                   attributes:@{NSFilePosixPermissions: @0700}
                                                        error:&error]) {
        fail("Could not create the private log directory.");
    }
    NSString *path = [logs stringByAppendingPathComponent:@"app.log"];
    int descriptor = open(path.fileSystemRepresentation, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0600);
    if (descriptor < 0) fail("Could not open the private application log.");
    if (fchmod(descriptor, 0600) < 0 || dup2(descriptor, STDOUT_FILENO) < 0 || dup2(descriptor, STDERR_FILENO) < 0) {
        close(descriptor);
        fail("Could not initialize application logging.");
    }
    if (descriptor > STDERR_FILENO) close(descriptor);
}

int main(int argc, char **argv) {
    @autoreleasepool {
        umask(0077);
        NSBundle *bundle = [NSBundle mainBundle];
        NSString *runtime = [bundle objectForInfoDictionaryKey:@"ShiliaoPythonExecutable"];
        NSString *bundled = [bundle objectForInfoDictionaryKey:@"ShiliaoBundledPython"];
        BOOL portable = [bundled isKindOfClass:[NSString class]];
        if (portable) {
            if (![bundled isEqualToString:@"python/bin/python3"]) fail("Invalid bundled runtime location.");
            NSString *root = [[bundle.resourcePath stringByResolvingSymlinksInPath] stringByAppendingPathComponent:@"python"];
            runtime = [[bundle.resourcePath stringByAppendingPathComponent:bundled] stringByResolvingSymlinksInPath];
            if (![runtime hasPrefix:[root stringByAppendingString:@"/"]]) fail("The runtime must remain inside the application.");
            unsetenv("PYTHONHOME");
            unsetenv("PYTHONPATH");
            if (setenv("PYTHONNOUSERSITE", "1", 1) < 0) fail("Could not isolate the bundled runtime.");
        }
        NSString *payload = [bundle.resourcePath stringByAppendingPathComponent:@"app"];
        NSString *script = [payload stringByAppendingPathComponent:@"app.py"];
        if (![runtime isKindOfClass:[NSString class]] || !runtime.isAbsolutePath ||
            ![[NSFileManager defaultManager] isExecutableFileAtPath:runtime]) {
            fail("The configured Python runtime is unavailable. Rebuild Shiliao with an existing runtime.");
        }
        if (![[NSFileManager defaultManager] fileExistsAtPath:script]) {
            fail("The bundled application entry point is missing.");
        }
        // launchd owns stdout/stderr for scheduled runs; explicit CLI arguments
        // retain their caller's streams for diagnostics and automation.
        if (argc == 1) prepareGUILog();
        if (chdir(payload.fileSystemRepresentation) < 0) fail("Could not enter the application directory.");
        if (setenv("PYTHONDONTWRITEBYTECODE", "1", 1) < 0) fail("Could not protect the signed application resources.");
        char **arguments = calloc((size_t)argc + 2, sizeof(char *));
        if (arguments == NULL) fail("Could not allocate process arguments.");
        arguments[0] = (char *)runtime.fileSystemRepresentation;
        arguments[1] = (char *)script.fileSystemRepresentation;
        for (int index = 1; index < argc; index++) arguments[index + 1] = argv[index];
        // Do not fork, daemonize or pass through a shell. Preserve the original
        // native launch responsibility and process lifetime across execve.
        execve(arguments[0], arguments, environ);
        int error = errno;
        free(arguments);
        fail(strerror(error));
    }
}
