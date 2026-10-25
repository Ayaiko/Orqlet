/* Read-only, owned foreground helper. Public SDK layouts; no private APIs. */
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <mach/host_info.h>
#include <mach/mach_host.h>
#include <sys/resource.h>
#include <sys/sysctl.h>
#include <time.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>

static dispatch_source_t pressure_source;
static mach_port_t host;
static const char *pressure_state = "unknown";
static const char *pressure_quality = "awaiting_event";
static double pressure_time = -1;
static unsigned emitted = 0;
static unsigned requested = 0;

static double milliseconds(clockid_t clock_id) {
    struct timespec value;
    if (clock_gettime(clock_id, &value) != 0) exit(3);
    return value.tv_sec * 1000.0 + value.tv_nsec / 1000000.0;
}

static const char *mach_quality(kern_return_t result) {
    return result == KERN_PROTECTION_FAILURE ? "denied" : "unavailable";
}

static void pressure_changed(void *context) {
    (void)context;
    unsigned long data = dispatch_source_get_data(pressure_source);
    if (!data) return;
    pressure_quality = "event_observed";
    if (data == DISPATCH_MEMORYPRESSURE_CRITICAL) pressure_state = "critical";
    else if (data == DISPATCH_MEMORYPRESSURE_WARN) pressure_state = "warning";
    else if (data == DISPATCH_MEMORYPRESSURE_NORMAL) pressure_state = "normal";
    else {
        pressure_state = "unknown";
        pressure_quality = "coalesced_events";  /* flags cannot establish the last state */
    }
    pressure_time = milliseconds(CLOCK_MONOTONIC_RAW);
}

static void sample(void *context) {
    (void)context;
    double mono_start = milliseconds(CLOCK_MONOTONIC_RAW);
    double utc = milliseconds(CLOCK_REALTIME);
    host_cpu_load_info_data_t cpu = {0};
    mach_msg_type_number_t cpu_count = HOST_CPU_LOAD_INFO_COUNT;
    kern_return_t cpu_result = host_statistics(host, HOST_CPU_LOAD_INFO,
                                               (host_info_t)&cpu, &cpu_count);
    vm_statistics64_data_t vm = {0};
    mach_msg_type_number_t vm_count = HOST_VM_INFO64_COUNT;
    kern_return_t vm_result = host_statistics64(host, HOST_VM_INFO64,
                                                (host_info64_t)&vm, &vm_count);
    vm_size_t page_size = 0;
    kern_return_t page_result = host_page_size(host, &page_size);
    struct xsw_usage swap = {0};
    size_t swap_size = sizeof(swap);
    int swap_result = sysctlbyname("vm.swapusage", &swap, &swap_size, NULL, 0);
    int swap_errno = errno;
    int cpu_ok = cpu_result == KERN_SUCCESS && cpu_count == HOST_CPU_LOAD_INFO_COUNT;
    size_t needed = (offsetof(vm_statistics64_data_t, swapouts) + sizeof(vm.swapouts)) / sizeof(integer_t);
    int vm_ok = vm_result == KERN_SUCCESS && vm_count >= needed;
    int swap_ok = swap_result == 0 && swap_size == sizeof(swap);
    double mono_end = milliseconds(CLOCK_MONOTONIC_RAW);
    printf("{\"schema\":\"orqlet.native-sample.v1\",\"unix_ms\":%.3f,"
           "\"monotonic_ms\":%.3f,\"read_duration_ms\":%.3f,\"cpu_ticks\":",
           utc, mono_start, mono_end - mono_start);
    if (cpu_ok)
        printf("[%u,%u,%u,%u]", cpu.cpu_ticks[CPU_STATE_USER],
               cpu.cpu_ticks[CPU_STATE_SYSTEM], cpu.cpu_ticks[CPU_STATE_IDLE],
               cpu.cpu_ticks[CPU_STATE_NICE]);
    else printf("null");
    printf(",\"cpu_quality\":\"%s\",\"page_size_bytes\":", cpu_ok ? "available" : mach_quality(cpu_result));
    if (page_result == KERN_SUCCESS && page_size > 0) printf("%" PRIu64, (uint64_t)page_size);
    else printf("null");
    printf(",\"swap_in_pages\":");
    if (vm_ok)
        printf("%" PRIu64, vm.swapins);
    else printf("null");
    printf(",\"swap_out_pages\":");
    if (vm_ok)
        printf("%" PRIu64, vm.swapouts);
    else printf("null");
    printf(",\"vm_quality\":\"%s\",\"swap_used_bytes\":", vm_ok ? "available" : mach_quality(vm_result));
    if (swap_ok) printf("%" PRIu64, swap.xsu_used);
    else printf("null");
    printf(",\"swap_quality\":\"%s\",\"pressure\":\"%s\","
           "\"pressure_quality\":\"%s\",\"pressure_event_monotonic_ms\":",
           swap_ok ? "available" :
           swap_result != 0 && (swap_errno == EACCES || swap_errno == EPERM) ? "denied" : "unavailable",
           pressure_state, pressure_quality);
    if (pressure_time >= 0) printf("%.3f", pressure_time);
    else printf("null");
    printf("}\n");
    fflush(stdout);
    if (++emitted == requested + 1) {
        struct rusage usage;
        if (getrusage(RUSAGE_SELF, &usage) != 0) exit(3);
        printf("{\"schema\":\"orqlet.native-summary.v1\",\"cpu_user_ms\":%.3f,"
               "\"cpu_system_ms\":%.3f,\"peak_rss_bytes\":%" PRIu64 "}\n",
               usage.ru_utime.tv_sec * 1000.0 + usage.ru_utime.tv_usec / 1000.0,
               usage.ru_stime.tv_sec * 1000.0 + usage.ru_stime.tv_usec / 1000.0,
               (uint64_t)usage.ru_maxrss);
        fflush(stdout);
        exit(0);
    }
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    char *end;
    errno = 0;
    long seconds = strtol(argv[1], &end, 10);
    if (errno || *end || seconds < 1 || seconds > 300) return 2;
    requested = (unsigned)seconds;
    host = mach_host_self();
    pressure_source = dispatch_source_create(DISPATCH_SOURCE_TYPE_MEMORYPRESSURE, 0,
        DISPATCH_MEMORYPRESSURE_NORMAL | DISPATCH_MEMORYPRESSURE_WARN |
        DISPATCH_MEMORYPRESSURE_CRITICAL, dispatch_get_main_queue());
    if (pressure_source) {
        dispatch_source_set_event_handler_f(pressure_source, pressure_changed);
        dispatch_resume(pressure_source);
    } else pressure_quality = "unavailable";
    dispatch_source_t timer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0,
                                                     dispatch_get_main_queue());
    if (!timer) return 3;
    dispatch_source_set_event_handler_f(timer, sample);
    dispatch_source_set_timer(timer, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC),
                              NSEC_PER_SEC, 10 * NSEC_PER_MSEC);
    sample(NULL);
    dispatch_resume(timer);
    dispatch_main();
}
