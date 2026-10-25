/* PulseAudio "muet" pour compiler dsd-fme sur une machine compagne sans
 * PulseAudio (macOS sans Homebrew, 2026-10-25). Seules les declarations dont
 * dsd-fme a besoin ; toutes les fonctions repondent "indisponible". La machine
 * compagne lance dsd-fme avec "-o null" et une entree fichier ou flux : rien
 * n'appelle jamais le son. simple.h, error.h et introspect.h incluent ce fichier. */
#ifndef PULSE_MUET_H
#define PULSE_MUET_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { PA_SAMPLE_S16NE = 3, PA_SAMPLE_FLOAT32NE = 5 } pa_sample_format_t;
typedef enum { PA_STREAM_NODIRECTION, PA_STREAM_PLAYBACK, PA_STREAM_RECORD } pa_stream_direction_t;
typedef enum {
    PA_CONTEXT_UNCONNECTED, PA_CONTEXT_CONNECTING, PA_CONTEXT_AUTHORIZING, PA_CONTEXT_SETTING_NAME,
    PA_CONTEXT_READY, PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED
} pa_context_state_t;
typedef enum { PA_OPERATION_RUNNING, PA_OPERATION_DONE, PA_OPERATION_CANCELLED } pa_operation_state_t;
typedef enum { PA_CONTEXT_NOFLAGS = 0 } pa_context_flags_t;

typedef struct pa_sample_spec { pa_sample_format_t format; uint32_t rate; uint8_t channels; } pa_sample_spec;
typedef struct pa_buffer_attr { uint32_t maxlength, tlength, prebuf, minreq, fragsize; } pa_buffer_attr;
typedef struct pa_channel_map { uint8_t channels; int map[32]; } pa_channel_map;
typedef struct pa_sink_info { const char *name; uint32_t index; const char *description; } pa_sink_info;
typedef struct pa_source_info { const char *name; uint32_t index; const char *description; } pa_source_info;

typedef struct pa_simple pa_simple;
typedef struct pa_context pa_context;
typedef struct pa_mainloop pa_mainloop;
typedef struct pa_mainloop_api pa_mainloop_api;
typedef struct pa_operation pa_operation;
typedef struct pa_spawn_api pa_spawn_api;
typedef uint64_t pa_usec_t;

typedef void (*pa_context_notify_cb_t)(pa_context *c, void *userdata);
typedef void (*pa_sink_info_cb_t)(pa_context *c, const pa_sink_info *i, int eol, void *userdata);
typedef void (*pa_source_info_cb_t)(pa_context *c, const pa_source_info *i, int eol, void *userdata);

pa_simple *pa_simple_new(const char *server, const char *name, pa_stream_direction_t dir, const char *dev,
                         const char *stream_name, const pa_sample_spec *ss, const pa_channel_map *map,
                         const pa_buffer_attr *attr, int *error);
void pa_simple_free(pa_simple *s);
int pa_simple_write(pa_simple *s, const void *data, size_t bytes, int *error);
int pa_simple_read(pa_simple *s, void *data, size_t bytes, int *error);
pa_usec_t pa_simple_get_latency(pa_simple *s, int *error);
const char *pa_strerror(int error);

pa_mainloop *pa_mainloop_new(void);
void pa_mainloop_free(pa_mainloop *m);
pa_mainloop_api *pa_mainloop_get_api(pa_mainloop *m);
int pa_mainloop_iterate(pa_mainloop *m, int block, int *retval);
pa_context *pa_context_new(pa_mainloop_api *api, const char *name);
void pa_context_unref(pa_context *c);
int pa_context_connect(pa_context *c, const char *server, pa_context_flags_t flags, const pa_spawn_api *api);
void pa_context_disconnect(pa_context *c);
void pa_context_set_state_callback(pa_context *c, pa_context_notify_cb_t cb, void *userdata);
pa_context_state_t pa_context_get_state(const pa_context *c);
pa_operation *pa_context_get_sink_info_list(pa_context *c, pa_sink_info_cb_t cb, void *userdata);
pa_operation *pa_context_get_source_info_list(pa_context *c, pa_source_info_cb_t cb, void *userdata);
pa_operation_state_t pa_operation_get_state(const pa_operation *o);
void pa_operation_unref(pa_operation *o);

#ifdef __cplusplus
}
#endif
#endif
