/* PulseAudio "muet" (voir pulse/pulseaudio.h) : tout repond "indisponible".
 * Compile trois fois par bridge/construire_dsd.sh (libpulse, libpulse-simple,
 * libpulse-mainloop), les noms que cherche le CMake de dsd-fme. */
#include "pulse/pulseaudio.h"

#define PA_ERR_CONNECTIONREFUSED 6

static void fail(int *error) {
    if (error)
        *error = PA_ERR_CONNECTIONREFUSED;
}

pa_simple *pa_simple_new(const char *server, const char *name, pa_stream_direction_t dir, const char *dev,
                         const char *stream_name, const pa_sample_spec *ss, const pa_channel_map *map,
                         const pa_buffer_attr *attr, int *error) {
    fail(error);
    return NULL;
}
void pa_simple_free(pa_simple *s) {}
int pa_simple_write(pa_simple *s, const void *data, size_t bytes, int *error) { fail(error); return -1; }
int pa_simple_read(pa_simple *s, void *data, size_t bytes, int *error) { fail(error); return -1; }
pa_usec_t pa_simple_get_latency(pa_simple *s, int *error) { fail(error); return 0; }
const char *pa_strerror(int error) { return "PulseAudio absent (version muette pour machine compagne)"; }

pa_mainloop *pa_mainloop_new(void) { return NULL; }
void pa_mainloop_free(pa_mainloop *m) {}
pa_mainloop_api *pa_mainloop_get_api(pa_mainloop *m) { return NULL; }
int pa_mainloop_iterate(pa_mainloop *m, int block, int *retval) { return -1; }
pa_context *pa_context_new(pa_mainloop_api *api, const char *name) { return NULL; }
void pa_context_unref(pa_context *c) {}
int pa_context_connect(pa_context *c, const char *server, pa_context_flags_t flags, const pa_spawn_api *api) { return -1; }
void pa_context_disconnect(pa_context *c) {}
void pa_context_set_state_callback(pa_context *c, pa_context_notify_cb_t cb, void *userdata) {}
pa_context_state_t pa_context_get_state(const pa_context *c) { return PA_CONTEXT_FAILED; }
pa_operation *pa_context_get_sink_info_list(pa_context *c, pa_sink_info_cb_t cb, void *userdata) { return NULL; }
pa_operation *pa_context_get_source_info_list(pa_context *c, pa_source_info_cb_t cb, void *userdata) { return NULL; }
pa_operation_state_t pa_operation_get_state(const pa_operation *o) { return PA_OPERATION_DONE; }
void pa_operation_unref(pa_operation *o) {}
